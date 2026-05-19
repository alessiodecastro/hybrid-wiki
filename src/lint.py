"""
Lint pipeline — consolidazione retroattiva di duplicati/alias (design §6.3).

Risolve il residuo §11.1: pagine entità che si riferiscono allo stesso
referente ma hanno id diversi (sinonimi, varianti, alias/persone, sotto-parti),
non eliminabili in fase di ingest perché emergono solo guardando la wiki nel
suo insieme.

Principio non negoziabile (design §6.3): **l'output del lint non è
automatico**. Due fasi nettamente separate:

  FASE DETECT  (read-only, nessuna modifica)
    similarità semantica tra pagine entità (vettori già in ChromaDB,
    zero costo embedding) → coppie candidate → l'LLM adjudica la
    relazione → clustering → report YAML con `approved: false`.
    L'umano edita il report e mette `approved: true` dove concorda.

  FASE APPLY  (esplicita, su report già approvato dall'umano)
    per ogni cluster approvato: merge dei body nel canonical, unione
    sorgenti, rewrite degli inbound [[alias]] → [[canonical]],
    eliminazione pagina+vettore alias, re-embed canonical, audit
    append-only con snapshot completo dell'alias (recuperabilità anche
    senza git), un solo Hot Layer rebuild a fine batch.

Reversibilità: la wiki è in git (design §7.5); inoltre ogni alias
eliminato è salvato integralmente in applied_merges.jsonl.
"""

from __future__ import annotations
import json
import re
from datetime import datetime, date

import yaml

from .config import (
    WIKI_COLLECTION, DEFAULT_DOMAIN, MIXED_DOMAIN,
    CONSOLIDATION_REPORT_PATH, APPLIED_MERGES_PATH,
    DUP_SIM_MIN_COSINE, DUP_NEIGHBORS_K, DUP_DIAG_TOP, DUP_MAX_ADJUDICATIONS,
    AGENTS_MD_PATH, TOKEN_LOG_PATH,
)
from .stores import WikiStore, VectorDB
from .embeddings import Embedder
from .llm_client import LLMClient
from .hot_layer import HotLayer
from .token_tracker import TokenTracker


def _extract_json(text: str) -> dict:
    """Estrae il primo oggetto JSON da un testo libero (tollerante a prosa)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"Nessun JSON nella risposta:\n{text}")
    return json.loads(m.group(0))


def _cosine(u: list[float], v: list[float]) -> float:
    """Similarità coseno calcolata esplicitamente.

    NON ci si affida alla distanza dell'indice ChromaDB: di default usa lo
    spazio L2 (euclideo al quadrato), semantica e range diversi dal coseno.
    Per vettori unitari l'ordinamento ANN coincide, quindi query() resta
    valida per TROVARE i vicini; ma il valore va ricalcolato qui per avere
    una soglia interpretabile e indipendente dalla configurazione dell'indice.
    """
    dot = sum(a * b for a, b in zip(u, v))
    nu = sum(a * a for a in u) ** 0.5
    nv = sum(b * b for b in v) ** 0.5
    if nu == 0.0 or nv == 0.0:
        return 0.0
    return dot / (nu * nv)


class _UnionFind:
    """Union-find minimale per raggruppare coppie duplicate in cluster."""

    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def clusters(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in self.parent:
            out.setdefault(self.find(node), []).append(node)
        return {r: sorted(m) for r, m in out.items() if len(m) > 1}


class LintPipeline:
    """Detection + apply della consolidazione duplicati/alias.

    Le due fasi sono metodi distinti e non si chiamano a vicenda: DETECT
    produce un artefatto su disco, APPLY lo consuma solo dopo revisione
    umana. Non esiste un percorso che applichi senza il passaggio di
    triage.
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        embedder: Embedder | None = None,
        tracker: TokenTracker | None = None,
    ):
        self.tracker = tracker or TokenTracker(log_path=TOKEN_LOG_PATH)
        self.wiki = WikiStore()
        self.vdb = VectorDB()
        self.llm = llm or LLMClient(tracker=self.tracker)
        self.embedder = embedder or Embedder(tracker=self.tracker)
        self.hot = HotLayer(self.wiki, self.llm)
        self.agents_md = AGENTS_MD_PATH.read_text(encoding="utf-8") if AGENTS_MD_PATH.exists() else ""

    # ------------------------------------------------------------------
    # Utilità comuni
    # ------------------------------------------------------------------
    def _entity_pages(self) -> dict[str, tuple[dict, str]]:
        """pid -> (frontmatter, body) delle sole pagine `type: entity`."""
        pages: dict[str, tuple[dict, str]] = {}
        for pid in self.wiki.list():
            if pid.startswith("source_"):
                continue
            fm, body = self.wiki.get(pid)
            if fm.get("type") != "entity":
                continue
            pages[pid] = (fm, body)
        return pages

    # ------------------------------------------------------------------
    # FASE 1 — DETECT (read-only)
    # ------------------------------------------------------------------
    def detect_duplicates(self) -> dict:
        """Trova cluster di pagine entità candidate-duplicato.

        Costo: solo chiamate LLM di adjudication sulle coppie sopra soglia
        (poche, perché la soglia è conservativa). La ricerca dei vicini
        riusa i vettori già in ChromaDB → zero embedding.

        Side effect: scrive CONSOLIDATION_REPORT_PATH (mai modifiche wiki).
        """
        pages = self._entity_pages()
        proposals: list[dict] = []
        if len(pages) < 2:
            self._write_report([], len(pages))
            return {"candidates": 0, "clusters": 0, "report": str(CONSOLIDATION_REPORT_PATH)}

        # 1) Coppie candidate. query() trova i vicini (ANN, ordinamento
        #    corretto), poi RI-CALCOLIAMO il coseno esplicito dai vettori
        #    salvati e filtriamo su quello (vedi _cosine: la distanza
        #    dell'indice è L2, non interpretabile come soglia coseno).
        emb_cache: dict[str, list[float]] = {}

        def _emb(pid: str):
            if pid not in emb_cache:
                emb_cache[pid] = self.vdb.get_embedding(WIKI_COLLECTION, pid)
            return emb_cache[pid]

        scored: dict[tuple[str, str], float] = {}  # coppia -> coseno
        with self.tracker.phase("lint:detect"):
            for pid, (fm, _b) in pages.items():
                emb = _emb(pid)
                if emb is None:
                    continue
                dom = fm.get("domain", DEFAULT_DOMAIN)
                where = {
                    "$and": [
                        {"domain": {"$in": [dom, MIXED_DOMAIN]}},
                        {"type": "entity"},
                    ]
                }
                hits = self.vdb.query(WIKI_COLLECTION, emb, DUP_NEIGHBORS_K + 1, where=where)
                for h in hits:
                    other = (h.get("metadata") or {}).get("page_id") or h["id"]
                    if other == pid or other not in pages:
                        continue
                    key = tuple(sorted((pid, other)))
                    if key in scored:
                        continue
                    oe = _emb(other)
                    if oe is None:
                        continue
                    scored[key] = _cosine(emb, oe)

        # Diagnostica: stampa le coppie più vicine anche SOTTO soglia, per
        # calibrare DUP_SIM_MIN_COSINE empiricamente (il lint è uno
        # strumento di triage: mostrare i dati è parte del suo lavoro).
        ranked = sorted(scored.items(), key=lambda kv: -kv[1])
        print(f"[diag] coppie valutate: {len(scored)} · "
              f"soglia coseno: {DUP_SIM_MIN_COSINE}")
        for (a, b), cos in ranked[:DUP_DIAG_TOP]:
            mark = "✓" if cos >= DUP_SIM_MIN_COSINE else " "
            print(f"[diag] {mark} cos={cos:.3f}  {a}  ~  {b}")

        # Sopra soglia, ordinate per coseno decrescente, capped: in un
        # corpus narrativo le coppie sopra soglia sono molte e per lo più
        # "distinct" — l'adjudication LLM è il vero filtro ma va limitata.
        above = [k for k, c in ranked if c >= DUP_SIM_MIN_COSINE]
        candidate_pairs = above[:DUP_MAX_ADJUDICATIONS]
        skipped = len(above) - len(candidate_pairs)
        print(f"[diag] coppie sopra soglia: {len(above)} · "
              f"adjudicate: {len(candidate_pairs)} · "
              f"non valutate (oltre cap {DUP_MAX_ADJUDICATIONS}): {skipped}")

        # 2) Adjudication LLM per coppia: relazione + canonical.
        #    PRINCIPIO: consolidamento != gerarchia. Solo `same_entity` e
        #    `alias_of` sono DUPLICATI (relazione di equivalenza → merge).
        #    `subset_of` è una relazione GERARCHICA (Mount Doom dentro
        #    Mordor, l'Anello Unico tra gli Anelli del Potere): NON è un
        #    duplicato — l'entità va conservata distinta e la relazione
        #    registrata come link nel grafo (§5.2). Unirla via union-find
        #    concatenerebbe transitivamente entità diverse in un blob
        #    (osservato: 9 entità Asimov collassate su "trantor").
        uf = _UnionFind()
        adjudications: list[dict] = []
        hierarchy: list[dict] = []  # subset_of: informativo, mai applicato
        for a, b in candidate_pairs:
            verdict = self._adjudicate(a, pages[a], b, pages[b])
            adjudications.append(verdict)
            rel = verdict["relation"]
            if rel in ("same_entity", "alias_of"):
                uf.union(a, b)
            elif rel == "subset_of":
                # canonical = il contenitore secondo l'adjudication.
                container = verdict["canonical"]
                part = b if container == a else a
                hierarchy.append({
                    "part": part,
                    "container": container,
                    "confidence": verdict["confidence"],
                    "rationale": verdict["rationale"],
                })

        # 3) Clustering + scelta canonical (voto tra le adjudication interne).
        for cluster_members in uf.clusters().values():
            # Solo relazioni di equivalenza: il cluster nasce da union su
            # same_entity/alias_of, quindi qui filtriamo coerentemente.
            internal = [
                v for v in adjudications
                if v["a"] in cluster_members and v["b"] in cluster_members
                and v["relation"] in ("same_entity", "alias_of")
            ]
            if not internal:
                continue
            votes: dict[str, int] = {}
            for v in internal:
                votes[v["canonical"]] = votes.get(v["canonical"], 0) + 1
            # canonical = più votato; tie-break id più corto poi alfabetico.
            canonical = sorted(votes, key=lambda c: (-votes[c], len(c), c))[0]
            aliases = [m for m in cluster_members if m != canonical]
            relations = sorted({v["relation"] for v in internal})
            conf_rank = {"low": 0, "medium": 1, "high": 2}
            min_conf = min(internal, key=lambda v: conf_rank.get(v["confidence"], 0))["confidence"]
            dom = pages[canonical][0].get("domain", DEFAULT_DOMAIN)
            proposals.append({
                "domain": dom,
                "canonical": canonical,
                "aliases": aliases,
                "relations": relations,
                "confidence": min_conf,
                "rationale": " | ".join(v["rationale"] for v in internal)[:600],
                "approved": False,  # l'umano lo mette a true dopo triage
            })

        self._write_report(proposals, len(pages), hierarchy)
        return {
            "candidates": len(candidate_pairs),
            "clusters": len(proposals),
            "hierarchy_suggestions": len(hierarchy),
            "report": str(CONSOLIDATION_REPORT_PATH),
        }

    def _adjudicate(self, a: str, pa: tuple[dict, str], b: str, pb: tuple[dict, str]) -> dict:
        """Chiede all'LLM la relazione tra due pagine entità."""
        fa, ba = pa
        fb, bb = pb
        system = (
            "Sei un revisore di un companion wiki. Ti vengono date DUE pagine "
            "entità dello stesso dominio. Determina la relazione tra le due. "
            "Rispondi SOLO con JSON:\n"
            '{"relation": "same_entity|alias_of|subset_of|distinct", '
            '"canonical": "<id da MANTENERE>", "confidence": "high|medium|low", '
            '"rationale": "<1 frase>"}\n'
            "- same_entity: stessa identica cosa, id varianti (es. plurale, "
            "articolo, lingua diversa). canonical = l'id più canonico.\n"
            "- alias_of: una è alias/persona/nome alternativo in-world "
            "dell'altra (es. un travestimento dello stesso personaggio). "
            "canonical = l'entità principale.\n"
            "- subset_of: una è una sotto-parte/aspetto dell'altra (es. una "
            "regione dentro un territorio). canonical = l'entità contenitore.\n"
            "- distinct: entità diverse. canonical = una qualsiasi (verrà ignorata).\n"
            "canonical DEVE essere esattamente uno dei due id forniti.\n\n"
            f"AGENTS.md:\n{self.agents_md}"
        )
        user = (
            f"## Pagina A — id: {a} (subtype: {fa.get('subtype','')})\n{ba[:1500]}\n\n"
            f"## Pagina B — id: {b} (subtype: {fb.get('subtype','')})\n{bb[:1500]}"
        )
        with self.tracker.phase("lint:adjudicate"):
            raw = self.llm.complete(system=system, user=user, max_tokens=400)
        try:
            d = _extract_json(raw)
        except Exception as e:
            print(f"[WARN] adjudication fallita per ({a},{b}): {e} → distinct")
            return {"a": a, "b": b, "relation": "distinct", "canonical": a,
                    "confidence": "low", "rationale": "parsing fallito"}
        canonical = d.get("canonical")
        if canonical not in (a, b):
            canonical = a  # robustezza: forza un id valido
        return {
            "a": a, "b": b,
            "relation": d.get("relation", "distinct"),
            "canonical": canonical,
            "confidence": d.get("confidence", "low"),
            "rationale": (d.get("rationale") or "").strip()[:200],
        }

    def _write_report(self, proposals: list[dict], n_pages: int,
                      hierarchy: list[dict] | None = None) -> None:
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "entity_pages_scanned": n_pages,
            "params": {
                "min_cosine": DUP_SIM_MIN_COSINE,
                "neighbors_k": DUP_NEIGHBORS_K,
            },
            "instructions": (
                "PROPOSALS = duplicati (same_entity/alias_of): mettere "
                "'approved: true' SOLO sui cluster corretti, poi "
                "python scripts/lint.py --apply-consolidation. "
                "HIERARCHY_SUGGESTIONS = relazioni gerarchiche (subset_of): "
                "SOLO informative, NON vengono mai applicate; sono spunti "
                "per link nel grafo, le entità restano distinte."
            ),
            "proposals": proposals,
            # Sezione informativa: l'apply legge SOLO `proposals`, quindi
            # questi non vengono mai eseguiti come merge. Esistono per non
            # perdere il segnale gerarchico emerso dalla detection.
            "hierarchy_suggestions": hierarchy or [],
        }
        CONSOLIDATION_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONSOLIDATION_REPORT_PATH.write_text(
            yaml.safe_dump(report, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # FASE 2 — APPLY (su report approvato dall'umano)
    # ------------------------------------------------------------------
    def apply_consolidation(self, report_path=None) -> dict:
        """Applica i soli cluster con `approved: true` nel report.

        Side effect: modifica/elimina pagine wiki, riscrive link, aggiorna
        i vettori, scrive l'audit, ricostruisce il Hot Layer una volta.
        """
        path = report_path or CONSOLIDATION_REPORT_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"Report non trovato: {path}. Eseguire prima --detect-duplicates."
            )
        report = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        proposals = [p for p in (report.get("proposals") or []) if p.get("approved")]
        if not proposals:
            return {"applied": 0, "note": "nessuna proposta approvata (approved: true)"}

        applied = 0
        touched_any = False
        for prop in proposals:
            canonical = prop["canonical"]
            aliases = [a for a in prop.get("aliases", []) if a != canonical]
            if not self.wiki.exists(canonical):
                print(f"[SKIP] canonical assente: {canonical}")
                continue
            for alias in aliases:
                if not self.wiki.exists(alias):
                    print(f"[SKIP] alias assente: {alias}")
                    continue
                self._merge_alias_into_canonical(canonical, alias)
                applied += 1
                touched_any = True

        if touched_any:
            with self.tracker.phase("lint:hot_layer_rebuild"):
                self.hot.rebuild()
        return {"applied": applied, "report": str(path)}

    def _merge_alias_into_canonical(self, canonical: str, alias: str) -> None:
        """Fonde una pagina alias nel canonical e rimuove l'alias.

        Ordine pensato per sicurezza: prima si produce e salva il body
        fuso (nulla di distruttivo), poi si riscrivono i link, poi si
        scrive l'audit con lo snapshot completo, e SOLO infine si elimina
        l'alias (pagina + vettore). Se qualcosa fallisce prima della
        delete, l'alias è ancora recuperabile dal filesystem.
        """
        cfm, cbody = self.wiki.get(canonical)
        afm, abody = self.wiki.get(alias)

        system = (
            f"Sei un curatore di wiki. Fondi la pagina ALIAS '{alias}' nella "
            f"pagina CANONICA '{canonical}' (stessa entità/alias/sotto-parte). "
            "Produci il body MARKDOWN finale della pagina canonica:\n"
            "- conserva la struttura: # titolo, ## Panoramica, ## Dettagli, "
            "## Relazioni, ## Domande aperte;\n"
            "- integra le informazioni dell'alias senza perdere nulla di valido;\n"
            "- se emergono contraddizioni, NON nasconderle: aggiungi/estendi "
            "'## Contraddizioni note' citando le sorgenti;\n"
            "- italiano, terza persona, enciclopedico, niente invenzioni;\n"
            "- restituisci SOLO il markdown del body, nessun preambolo.\n\n"
            f"AGENTS.md:\n{self.agents_md}"
        )
        user = (
            f"## CANONICA ({canonical})\n{cbody}\n\n"
            f"---\n\n## ALIAS da assorbire ({alias})\n{abody}"
        )
        with self.tracker.phase("lint:merge"):
            merged_body = self.llm.complete(system=system, user=user, max_tokens=2500)

        # Frontmatter unificato: unione sorgenti, dominio _mixed se divergono.
        c_sources = cfm.get("sources") or []
        a_sources = afm.get("sources") or []
        sources = list(dict.fromkeys(c_sources + a_sources))
        c_dom = cfm.get("domain", DEFAULT_DOMAIN)
        a_dom = afm.get("domain", DEFAULT_DOMAIN)
        new_dom = c_dom if c_dom == a_dom else MIXED_DOMAIN
        extra_meta = {
            "type": "entity",
            "subtype": cfm.get("subtype", "") or afm.get("subtype", "") or "",
            "domain": new_dom,
            "last_updated": date.today().isoformat(),
            "stale": False,
            "title": cfm.get("title", canonical.replace("_", " ").title()),
            "consolidated_from": list(dict.fromkeys(
                (cfm.get("consolidated_from") or []) + [alias]
            )),
        }
        self.wiki.update_with_merge(canonical, merged_body, sources, extra_meta=extra_meta)

        # Reindirizza gli inbound link e raccoglili per l'audit.
        relinked = self.wiki.rewrite_links(alias, canonical)

        # Audit append-only con snapshot integrale dell'alias (recuperabilità).
        APPLIED_MERGES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with APPLIED_MERGES_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "canonical": canonical,
                "alias_deleted": alias,
                "relinked_pages": relinked,
                "alias_snapshot": {"frontmatter": afm, "body": abody},
            }, ensure_ascii=False) + "\n")

        # Distruttivo per ultimo: elimina pagina e vettore alias.
        self.wiki.delete_page(alias)
        self.vdb.delete(WIKI_COLLECTION, [alias])

        # Re-embed del canonical aggiornato (delete+add, come l'ingest).
        _fm, body = self.wiki.get(canonical)
        with self.tracker.phase("lint:reembed"):
            emb = self.embedder.embed(f"{canonical}\n\n{body}")
        meta = {
            "page_id": canonical,
            "type": "entity",
            "subtype": extra_meta["subtype"],
            "kind": "wiki",
            "domain": new_dom,
        }
        self.vdb.delete(WIKI_COLLECTION, [canonical])
        self.vdb.add(WIKI_COLLECTION, [canonical], [emb], [body], [meta])
        print(f"  merged {alias} -> {canonical}  (relinked {len(relinked)} pagine)")
