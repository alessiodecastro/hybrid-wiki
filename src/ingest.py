"""
Pipeline di ingest dei documenti, con i tre livelli del design.

Il flusso comune a tutti i livelli garantisce il "principio non
negoziabile" (sezione 3.2): ogni documento viene SEMPRE salvato e
indicizzato integralmente nel raw layer, prima e indipendentemente da
qualsiasi altra elaborazione.

Differenze per livello:
- L0: stop dopo il raw layer (documenti ad alto volume, basso valore).
- L1: aggiunge una pagina wiki "source" sintetizzata dall'LLM.
- L2: L1 + identificazione entità e merge nelle pagine entity esistenti,
      con esplicitazione di eventuali contraddizioni.

Walking skeleton: la classificazione del livello è MANUALE, passata come
parametro CLI. Nelle fasi successive sarà assistita da un classificatore
automatico con review umana.
"""

from __future__ import annotations
import json
import re
from datetime import datetime, date
from pathlib import Path

from openai import BadRequestError

from .config import (
    AGENTS_MD_PATH, CHUNK_SIZE_WORDS, CHUNK_OVERLAP_WORDS, DATA_DIR,
    RAW_COLLECTION, WIKI_COLLECTION, VALID_LEVELS, VALID_SUBTYPES, TOKEN_LOG_PATH,
    DEFAULT_DOMAIN, MIXED_DOMAIN, ENTITY_INVENTORY_CAP, ENTITY_SHORTLIST_K,
)
from .stores import (
    RawStore, WikiStore, VectorDB, EntityIndex,
    ENTITY_STATE_ALIASED, ENTITY_STATE_CONSOLIDATED,
)
from .embeddings import Embedder
from .llm_client import LLMClient
from .hot_layer import HotLayer
from .token_tracker import TokenTracker


# Log degli skip causati dal content filter Azure. Append-only: serve come
# audit trail di "cosa NON è stato integrato e perchè", così le entità
# saltate sono recuperabili (es. con un retry futuro su prompt ridotti).
CONTENT_FILTER_SKIPS_PATH = DATA_DIR / "content_filter_skips.jsonl"


def _is_content_filter(exc: BadRequestError) -> bool:
    """True se l'eccezione OpenAI deriva dal content management policy
    (status 400 + code 'content_filter'). Distinto da altri 400 (es.
    payload malformato), che vanno comunque sollevati."""
    try:
        body = getattr(exc, "body", None) or {}
        return (body.get("error") or {}).get("code") == "content_filter"
    except Exception:
        return False


def _log_content_filter_skip(doc_id: str, page_id: str, title: str, domain: str, op: str) -> None:
    """Append idempotente di un record di skip nel log. Non solleva: se
    fallisce la scrittura, l'ingest deve comunque proseguire."""
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "doc_id": doc_id,
        "page_id": page_id,
        "title": title,
        "domain": domain,
        "operation": op,
        "reason": "azure_content_filter",
    }
    try:
        CONTENT_FILTER_SKIPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONTENT_FILTER_SKIPS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# Regex per generare slug "stile filesystem" da titoli liberi. Tutto
# minuscolo, solo alfanumerici e underscore, senza accenti né punteggiatura.
_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Converte un testo libero in uno slug filesystem-safe.

    Esempio: "Frodo Baggins — introduzione" -> "frodo_baggins_introduzione"
    """
    s = _slug_re.sub("_", text.lower()).strip("_")
    return s or "doc"  # fallback per stringhe vuote o composte solo da punteggiatura


def chunk_text(text: str, size: int = CHUNK_SIZE_WORDS, overlap: int = CHUNK_OVERLAP_WORDS) -> list[str]:
    """Spezza un testo in chunk sovrapposti, granularità a parola.

    Lo sliding window con overlap garantisce che frasi a cavallo tra due
    chunk siano comunque presenti integralmente in almeno uno: questo
    riduce la perdita di contesto su tagli infelici.

    Args:
        size: numero di parole per chunk.
        overlap: numero di parole sovrapposte tra chunk consecutivi.

    Returns:
        Lista di chunk. Documento più corto di `size` -> singolo chunk.
    """
    words = text.split()
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]
    chunks = []
    # step rappresenta l'avanzamento "netto" tra inizio di un chunk e il
    # successivo. Vincolo step >= 1 evita loop infiniti se overlap >= size.
    step = max(1, size - overlap)
    for start in range(0, len(words), step):
        end = start + size
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            # Ultimo chunk processato: il prossimo sarebbe oltre la fine.
            break
    return chunks


def _load_agents() -> str:
    """Carica il contratto operativo (AGENTS.md), iniettato in ogni prompt LLM."""
    return AGENTS_MD_PATH.read_text(encoding="utf-8") if AGENTS_MD_PATH.exists() else ""


def _extract_json(text: str) -> dict:
    """Estrae il primo blocco JSON da un testo libero.

    Necessario perché l'LLM a volte aggiunge prosa intorno al JSON nonostante
    le istruzioni. La regex è "greedy multiline": cattura dal primo `{` fino
    all'ultimo `}`.
    """
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"Nessun JSON trovato nella risposta:\n{text}")
    return json.loads(m.group(0))


class IngestPipeline:
    """Orchestratore dell'ingest dei documenti nei tre livelli L0/L1/L2.

    Mantiene riferimenti agli store e ai client esterni (LLM, embedder)
    e li riusa per tutte le elaborazioni successive — utile per non
    pagare l'overhead di inizializzazione su ingest batch.
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        embedder: Embedder | None = None,
        tracker: TokenTracker | None = None,
    ):
        """Inizializza la pipeline. I client sono opzionali per facilitare
        i test con mock; default = client reali su Azure OpenAI.

        Il TokenTracker è creato qui se non fornito esternamente, in modo
        che venga condiviso da LLMClient e Embedder e raccolga le metriche
        di tutte le sotto-fasi dell'ingest.
        """
        # Tracker condiviso tra LLM ed Embedder: vincolo perché la fase
        # corrente (contextvar) sia letta in modo consistente da entrambi.
        self.tracker = tracker or TokenTracker(log_path=TOKEN_LOG_PATH)
        self.raw = RawStore()
        self.wiki = WikiStore()
        self.vdb = VectorDB()
        self.embedder = embedder or Embedder(tracker=self.tracker)
        self.llm = llm or LLMClient(tracker=self.tracker)
        self.hot = HotLayer(self.wiki, self.llm)
        # Indice centrale entità (§13). Lazy load: il file viene letto al
        # primo accesso. Shared tra tutti i passaggi del batch — quindi è
        # consistente attraverso più ingest consecutivi nella stessa
        # invocazione di pipeline.
        self.entity_index = EntityIndex()
        # AGENTS.md viene caricato UNA VOLTA all'init: nel ciclo di vita
        # tipico di un ingest batch non cambia. In caso di hot-reload del
        # contratto operativo, ricreare la pipeline.
        self.agents_md = _load_agents()

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------
    def ingest(
        self,
        file_path: str | Path,
        title: str,
        level: str,
        subtype: str | None = None,
        domain: str = DEFAULT_DOMAIN,
        defer_hot_layer: bool = False,
    ) -> dict:
        """Ingesta un documento. Punto di ingresso unico per tutti i livelli.

        Args:
            file_path: path del file di testo sorgente.
            title: titolo leggibile (usato anche per generare il doc_id).
            level: "L0" | "L1" | "L2".
            subtype: opzionale, suggerisce all'estrazione entità il tipo
                principale (character/place/...). Usato solo in L2.
            domain: etichetta di dominio per il documento. Propaga ai
                chunk raw, alla source page e alle entity pages. Permette
                filtri query-time multi-dominio.
            defer_hot_layer: se True, NON ricostruisce il Hot Layer al
                termine di questo ingest. Usato dal bulk ingest: il rebuild
                è O(pagine_totali) e va eseguito UNA volta a fine batch
                (vedi rebuild_hot_layer), non per ogni documento — altrimenti
                il costo cresce quadraticamente col corpus. Il chiamante è
                responsabile di invocare rebuild_hot_layer() alla fine.

        Returns:
            Dict con doc_id, livello, dominio, pagine wiki toccate e flag
            hot_layer_deferred (utile al chiamante per sapere se deve
            ancora ricostruire).

        Raises:
            ValueError: se il livello è invalido o il file è vuoto.
        """
        if level not in VALID_LEVELS:
            raise ValueError(f"Livello invalido: {level}. Ammessi: {VALID_LEVELS}")
        path = Path(file_path)
        body = path.read_text(encoding="utf-8").strip()
        if not body:
            raise ValueError(f"File vuoto: {path}")

        # doc_id = slug del titolo + timestamp. Il timestamp evita collisioni
        # quando lo stesso documento viene re-ingestato (versionamento di fatto).
        doc_id = f"{slugify(title)}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        metadata = {
            "title": title,
            "source": str(path.name),
            "level": level,
            "domain": domain,
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
        }
        if subtype:
            metadata["subtype"] = subtype

        # 1. Raw store: scrive il documento immutabile prima di tutto.
        #    Nessuna chiamata API qui — solo filesystem.
        self.raw.save(doc_id, body, metadata)

        # 2. Raw index: SEMPRE indicizzato, indipendentemente dal livello.
        #    Questo è il punto chiave del "principio non negoziabile".
        #    Solo embedding API; fase taggata per il tracker.
        with self.tracker.phase(f"ingest:{level.lower()}:raw_index"):
            self._index_raw(doc_id, title, body, domain)

        touched_pages: list[str] = []

        # 3. Path divergenti per livello. Ogni step LLM è in una fase
        #    distinta per poter analizzare a posteriori dove va il budget.
        if level == "L0":
            # Stop qui: il documento è recuperabile solo via ricerca raw.
            pass
        elif level == "L1":
            with self.tracker.phase("ingest:l1:source_page"):
                source_page = self._make_source_page(doc_id, title, body, domain)
            touched_pages.append(source_page)
        elif level == "L2":
            with self.tracker.phase("ingest:l2:source_page"):
                source_page = self._make_source_page(doc_id, title, body, domain)
            touched_pages.append(source_page)
            # _integrate_entities internamente apre fasi annidate per
            # identify / create / merge.
            entity_pages = self._integrate_entities(doc_id, title, body, hint_subtype=subtype, domain=domain)
            touched_pages.extend(entity_pages)

        # 4. Hot Layer: rebuild solo se la wiki è cambiata. Per L0
        #    non c'è nulla da riflettere nell'index.
        #    defer_hot_layer=True: il bulk ingest salta il rebuild qui e lo
        #    fa una sola volta a fine batch (rebuild O(pagine) × N doc
        #    diventa O(N^2) se eseguito per ogni documento).
        hot_layer_deferred = False
        if touched_pages:
            if defer_hot_layer:
                hot_layer_deferred = True
            else:
                with self.tracker.phase("ingest:hot_layer_rebuild"):
                    self.hot.rebuild()

        return {
            "doc_id": doc_id,
            "level": level,
            "domain": domain,
            "wiki_pages": touched_pages,
            "hot_layer_deferred": hot_layer_deferred,
        }

    def promote(self, doc_id: str, new_level: str, defer_hot_layer: bool = False) -> dict:
        """Promozione/riclassificazione retroattiva di un documento esistente.

        Caso d'uso (§6.1 "promozione retroattiva"): un documento ingestato
        come L0 si rivela strategico → va portato a L1/L2 senza re-ingestarlo
        da zero. Il raw è immutabile e GIÀ presente: NON si ricrea il doc_id,
        NON si ri-salva il body, NON si ri-embeddano i chunk raw (esistono).
        Si eseguono solo gli step wiki del nuovo livello.

        L'unico aggiornamento al raw store è il metadato `level` (è
        annotazione di classificazione/provenienza, non contenuto): si
        riscrive lo stesso body con `level` aggiornato + `promoted_from`/
        `promoted_at` per audit. L'immutabilità riguarda il TESTO, non la
        classificazione, che il design prevede esplicitamente evolva.

        Args:
            doc_id: documento già nel RawStore.
            new_level: livello target (L0/L1/L2).
            defer_hot_layer: come in ingest(); per uso batch.

        Returns:
            {doc_id, level, domain, wiki_pages, hot_layer_deferred}.
        """
        if new_level not in VALID_LEVELS:
            raise ValueError(f"Livello invalido: {new_level}. Ammessi: {VALID_LEVELS}")
        if not self.raw.exists(doc_id):
            raise ValueError(f"doc_id non presente nel raw store: {doc_id}")

        fm, body = self.raw.get(doc_id)
        title = fm.get("title", doc_id)
        domain = fm.get("domain", DEFAULT_DOMAIN)
        subtype = fm.get("subtype")
        old_level = fm.get("level", "?")

        # Aggiorna SOLO il metadato di classificazione (body invariato).
        new_meta = {k: v for k, v in fm.items() if k != "id"}
        new_meta["level"] = new_level
        new_meta["promoted_from"] = old_level
        new_meta["promoted_at"] = datetime.now().isoformat(timespec="seconds")
        self.raw.save(doc_id, body, new_meta)

        # Step wiki del nuovo livello (raw GIÀ indicizzato: si salta).
        touched_pages: list[str] = []
        if new_level == "L0":
            pass
        elif new_level == "L1":
            with self.tracker.phase("promote:l1:source_page"):
                touched_pages.append(self._make_source_page(doc_id, title, body, domain))
        elif new_level == "L2":
            with self.tracker.phase("promote:l2:source_page"):
                touched_pages.append(self._make_source_page(doc_id, title, body, domain))
            touched_pages.extend(
                self._integrate_entities(doc_id, title, body, hint_subtype=subtype, domain=domain)
            )

        hot_layer_deferred = False
        if touched_pages:
            if defer_hot_layer:
                hot_layer_deferred = True
            else:
                with self.tracker.phase("promote:hot_layer_rebuild"):
                    self.hot.rebuild()

        return {
            "doc_id": doc_id,
            "level": new_level,
            "promoted_from": old_level,
            "domain": domain,
            "wiki_pages": touched_pages,
            "hot_layer_deferred": hot_layer_deferred,
        }

    def rebuild_hot_layer(self) -> None:
        """Ricostruisce il Hot Layer una volta sola.

        Pensato per essere chiamato dal bulk ingest al termine del batch,
        quando tutti i documenti sono stati processati con
        defer_hot_layer=True. Idempotente: ricostruisce sempre da zero
        leggendo lo stato corrente della wiki.
        """
        with self.tracker.phase("ingest:hot_layer_rebuild"):
            self.hot.rebuild()

    # ------------------------------------------------------------------
    # Helper privati
    # ------------------------------------------------------------------
    def _index_raw(self, doc_id: str, title: str, body: str, domain: str) -> None:
        """Chunka il body e popola la collection raw_chunks.

        Ogni chunk ha id deterministico `{doc_id}__chunk_{NNN}` così che
        un re-ingest dello stesso doc_id (caso raro ma possibile in dev)
        produca upsert in-place invece di duplicati.

        Il `domain` viene propagato nei metadati di ogni chunk: abilita
        il filtro `where={"domain": ...}` lato query.
        """
        chunks = chunk_text(body)
        if not chunks:
            return
        ids = [f"{doc_id}__chunk_{i:03d}" for i in range(len(chunks))]
        metas = [
            {"doc_id": doc_id, "title": title, "chunk_idx": i, "kind": "raw", "domain": domain}
            for i in range(len(chunks))
        ]
        # Singola chiamata batch all'API embeddings: massimizza throughput
        # e minimizza il numero di richieste fatturabili.
        embs = self.embedder.embed_batch(chunks)
        self.vdb.add(RAW_COLLECTION, ids, embs, chunks, metas)

    def _index_wiki_page(self, page_id: str) -> None:
        """(Re-)indicizza una pagina wiki nella collection wiki_pages.

        Strategia: delete + upsert. Pulisce eventuali vettori obsoleti
        legati a una versione precedente della stessa pagina (succede
        nel ciclo di merge L2).

        Il `domain` viene letto dal frontmatter (popolato/aggiornato dai
        metodi di creazione/merge): le pagine miste hanno domain=MIXED_DOMAIN.
        """
        fm, body = self.wiki.get(page_id)
        # L'embedding è calcolato sul body completo prefisso dal page_id:
        # il nome della pagina è un segnale semantico forte (es. "frodo_baggins").
        emb = self.embedder.embed(f"{page_id}\n\n{body}")
        meta = {
            "page_id": page_id,
            "type": fm.get("type", ""),
            "subtype": fm.get("subtype", "") or "",
            "kind": "wiki",
            "domain": fm.get("domain", DEFAULT_DOMAIN),
        }
        self.vdb.delete(WIKI_COLLECTION, [page_id])
        self.vdb.add(WIKI_COLLECTION, [page_id], [emb], [body], [meta])

    def _make_source_page(self, doc_id: str, title: str, body: str, domain: str) -> str:
        """Genera la pagina source per un documento (L1 e L2).

        La pagina source è la sintesi 1:1 di un singolo documento raw.
        Funge da "ponte" tra raw e wiki: l'audit trail della sintesi.
        Eredita il `domain` dal documento sorgente.

        Returns:
            page_id della pagina creata.
        """
        page_id = f"source_{doc_id}"
        # Il system prompt include AGENTS.md per garantire coerenza tra
        # tutte le sintesi (tono, citazioni, struttura).
        system = (
            "Sei un curatore di un companion wiki di lettura. Produci una sintesi narrativa "
            "in italiano del documento sotto, strutturata in tre sezioni Markdown: "
            "## Overview, ## Dettagli, ## Citazioni notevoli. "
            "Mantieni un tono enciclopedico in terza persona. "
            "Non inventare contenuti non presenti nel documento. "
            "Cita sempre il documento sorgente come [[" + doc_id + "]] almeno nelle Overview.\n\n"
            "Regole operative del sistema (AGENTS.md):\n" + self.agents_md
        )
        user = f"# {title}\n\n{body}"
        summary = self.llm.complete(system=system, user=user, max_tokens=1500)

        metadata = {
            "type": "source",
            "subtype": "",
            "tags": ["source"],
            "sources": [doc_id],
            "domain": domain,
            "last_updated": date.today().isoformat(),
            "confidence": "medium",  # default per le sintesi: l'LLM può aver omesso dettagli
            "stale": False,
            "title": f"Sintesi: {title}",
        }
        self.wiki.save(page_id, summary, metadata)
        self._index_wiki_page(page_id)
        return page_id

    @staticmethod
    def _entity_line(pid: str, fm: dict, body: str) -> str:
        """Riga di inventario: id + subtype + descrittore breve dal body."""
        sub = fm.get("subtype") or "-"
        first = next(
            (l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")),
            "",
        )
        descr = (first[:90] + "…") if len(first) > 90 else first
        return f"- {pid} [{sub}] {descr}"

    def _collect_entity_pages(self, domain: str) -> dict[str, tuple[dict, str]]:
        """Mappa pid -> (frontmatter, body) delle pagine entity del dominio.

        Filtra: solo `type: entity` (no source_*), dominio corrente o
        `_mixed` (entità condivise tra corpora).
        """
        pages: dict[str, tuple[dict, str]] = {}
        for pid in self.wiki.list():
            if pid.startswith("source_"):
                continue
            fm, pbody = self.wiki.get(pid)
            if fm.get("type") != "entity":
                continue
            pdom = fm.get("domain", DEFAULT_DOMAIN)
            if pdom != domain and pdom != MIXED_DOMAIN:
                continue
            pages[pid] = (fm, pbody)
        return pages

    def _aliased_entries_for_domain(self, domain: str) -> list[dict]:
        """Entry aliased dall'indice che riguardano il dominio target.

        Include entità con domain corrispondente o `_mixed`. Output
        ordinato per id. Usato per arricchire l'inventario con le
        entità NOTE ma non ancora materializzate (§13): l'LLM deve
        comunque non coniarne varianti.
        """
        out = []
        for e in self.entity_index.list_by_state(ENTITY_STATE_ALIASED):
            edom = e.get("domain") or ""
            if edom == domain or edom == MIXED_DOMAIN or edom == "":
                out.append(e)
        out.sort(key=lambda x: x["id"])
        return out

    def _entity_inventory(self, domain: str, query_embedding: list[float] | None = None) -> str:
        """Inventario delle entità esistenti, da iniettare in _identify_entities.

        Serve a far RIUSARE al modello gli id esistenti invece di crearne
        varianti (the_shire vs shire, orodruin vs mount_doom, ...).

        Con lazy materialization (§13) l'inventario include DUE classi:
        - CONSOLIDATED: entità con pagina md materializzata (dal filesystem,
          come storicamente). Hanno descrittore breve dal body.
        - ALIASED: entità note solo nell'indice (n_sources < THRESHOLD).
          Niente body, solo id + subtype + n_sources — comunque sufficienti
          per impedire la creazione di varianti del medesimo id.

        Due modalità (debito noto §11.1: oltre ~ENTITY_INVENTORY_CAP entità
        consolidated l'inventario piatto satura il contesto):
        - PIATTA (n consolidated <= cap, oppure nessun query_embedding):
          lista completa consolidated + lista aliased compatta.
        - GERARCHICA (n consolidated > cap): SCHELETRO per subtype +
          SHORTLIST semantica (solo consolidated, le aliased non hanno
          vettore). Le aliased restano come elenco aggregato compatto.
        """
        pages = self._collect_entity_pages(domain)
        aliased = self._aliased_entries_for_domain(domain)

        if not pages and not aliased:
            return "(nessuna entità esistente in questo dominio: è il primo documento)"

        # Sezione ALIASED (riga compatta, ridotta a id + subtype + n_sources).
        # Mostrata anche in modalità gerarchica perché tipicamente sono
        # poche righe ognuna: utili per anti-frammentazione, costano poco.
        aliased_block = ""
        if aliased:
            lines = []
            for e in aliased:
                sub = e.get("subtype") or "-"
                lines.append(f"- {e['id']} [{sub}] (aliased, {e['n_sources']} sources)")
            aliased_block = (
                "\n\nALIASED (entità note ma non ancora consolidate — riusa l'id se trattate nel doc, "
                "altrimenti ignora):\n"
                + "\n".join(lines)
            )

        # Modalità piatta: sotto soglia o senza query vector disponibile.
        if len(pages) <= ENTITY_INVENTORY_CAP or query_embedding is None:
            if not pages:
                return "(nessuna entità materializzata in questo dominio)" + aliased_block
            lines = [self._entity_line(pid, fm, body) for pid, (fm, body) in pages.items()]
            return "\n".join(sorted(lines)) + aliased_block

        # --- Modalità gerarchica ---
        # 1. Scheletro: per subtype, conteggio + fino a 3 esemplari.
        by_sub: dict[str, list[str]] = {}
        for pid, (fm, _b) in pages.items():
            by_sub.setdefault(fm.get("subtype") or "-", []).append(pid)
        skeleton_lines = ["SCHELETRO (panoramica aggregata — lo spazio entità consolidated è più ampio della shortlist):"]
        for sub in sorted(by_sub):
            ids = sorted(by_sub[sub])
            esempi = ", ".join(ids[:3])
            piu = f" … (+{len(ids) - 3})" if len(ids) > 3 else ""
            skeleton_lines.append(f"- [{sub}] {len(ids)} entità: {esempi}{piu}")

        # 2. Shortlist semantica: K entità più vicine al documento.
        #    Filtro ChromaDB: solo entity, dominio o _mixed.
        #    Le aliased non hanno vettore e non compaiono nella shortlist —
        #    ma compaiono nella sezione ALIASED già aggiunta.
        where = {
            "$and": [
                {"domain": {"$in": [domain, MIXED_DOMAIN]}},
                {"type": "entity"},
            ]
        }
        hits = self.vdb.query(WIKI_COLLECTION, query_embedding, ENTITY_SHORTLIST_K, where=where)
        short_lines = []
        for h in hits:
            pid = (h.get("metadata") or {}).get("page_id") or h["id"]
            if pid in pages:
                fm, body = pages[pid]
                short_lines.append(self._entity_line(pid, fm, body))
        shortlist = "\n".join(short_lines) if short_lines else "(nessuna entità affine)"

        return (
            "\n".join(skeleton_lines)
            + f"\n\nSHORTLIST (le {len(short_lines)} entità consolidated più affini a questo documento — "
            "controlla QUI prima di coniare un id nuovo):\n"
            + shortlist
            + aliased_block
        )

    def _identify_entities(
        self,
        doc_id: str,
        title: str,
        body: str,
        hint_subtype: str | None,
        domain: str,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        """Chiede all'LLM di estrarre le entità rilevanti dal documento.

        Output forzato a JSON per parsing affidabile. Il prompt:
        1. Mostra l'inventario delle entità già esistenti (stesso dominio)
           e impone il RIUSO dell'id esatto se l'entità esiste già — anche
           con nome alternativo, sinonimo, singolare/plurale, articolo.
           Questo previene la frammentazione (osservata: 21 doc -> 72
           pagine wiki, con duplicati tipo mount_doom/orodruin).
        2. Alza la soglia di rilevanza: solo entità trattate in modo
           sostanziale, mai menzioni di passaggio.

        query_embedding: usato dall'inventario in modalità gerarchica per
        recuperare la shortlist semantica (vedi _entity_inventory). Se None
        l'inventario resta in modalità piatta.
        """
        hint = (
            f"\nIl curatore suggerisce che l'entità principale è di tipo: {hint_subtype}."
            if hint_subtype else ""
        )
        inventory = self._entity_inventory(domain, query_embedding=query_embedding)
        system = (
            f"Sei l'estrattore di entità di un companion wiki di lettura. "
            f"Il documento appartiene al dominio/corpus '{domain}': non confondere "
            f"le sue entità con quelle di altri corpus.\n\n"
            f"=== ENTITÀ GIÀ ESISTENTI NEL DOMINIO '{domain}' ===\n"
            f"{inventory}\n"
            f"=== FINE INVENTARIO ===\n\n"
            "REGOLA DI RIUSO (tassativa): se un'entità del documento corrisponde "
            "a una già presente nell'inventario, DEVI riusare il suo id ESATTO. "
            "Vale anche se nel documento compare con nome diverso: sinonimo, "
            "nome in altra lingua, variante con/senza articolo, singolare/plurale. "
            "Esempi di errori DA NON fare: creare 'shire' se esiste 'the_shire'; "
            "creare 'orodruin' se esiste 'mount_doom' (stessa entità, nome Sindarin); "
            "creare 'seldon_crises' se esiste 'seldon_crisis'. Nel dubbio, riusa.\n\n"
            "INVENTARIO POSSIBILMENTE PARZIALE: su corpus ampi l'inventario può "
            "mostrare solo uno SCHELETRO aggregato + una SHORTLIST delle entità più "
            "affini, non l'elenco completo. Quindi: (a) controlla SEMPRE la shortlist "
            "prima di coniare un id; (b) se un'entità non è in shortlist ma è di un "
            "tipo presente nello scheletro, è plausibile che esista già: usa un id "
            "CANONICO e prevedibile (nome proprio inglese, snake_case, senza articolo "
            "né plurale), NON una variante stilistica. Questo minimizza i duplicati "
            "che la pipeline di consolidazione dovrà poi unire.\n\n"
            "SOGLIA DI RILEVANZA: crea/segnala un'entità SOLO se è trattata in modo "
            "sostanziale (almeno 2-3 frasi di contenuto specifico su di essa). "
            "Oggetti, luoghi o personaggi nominati una sola volta di sfuggita NON "
            "sono entità: ignorali. Meglio poche entità solide che molte marginali.\n\n"
            "UNIFICAZIONE INTRA-DOCUMENTO: se nello STESSO documento la stessa "
            "entità compare con nomi diversi (sinonimo, nome in altra lingua, "
            "sigla, con/senza articolo, singolare/plurale), emetti UN SOLO id. "
            "CATEGORIA vs ISTANZE: se il documento descrive una categoria con più "
            "istanze (es. 'le Crisi Seldon' con la crisi degli Enciclopedisti, dei "
            "Mercanti, ecc.), crea UNA SOLA pagina per la categoria — NON una pagina "
            "per istanza — salvo che una singola istanza abbia ≥3 frasi di "
            "trattazione autonoma e indipendente.\n\n"
            "Rispondi SOLO con JSON valido nel formato:\n"
            '{"entities": [{"id": "snake_case_en", "type": "entity", "subtype": "character|place|artifact|event|book", "summary": "1-2 frasi"}]}'
            "\n- id: slug SEMPRE in INGLESE, snake_case (anche se il contenuto è "
            "in italiano: 'primary_radiant' non 'radiante_primario'). Se l'entità "
            "è nell'inventario, USA QUELL'id esatto; altrimenti coniane uno nuovo."
            "\n- subtype: uno tra character, place, artifact, event, book. Se l'entità è "
            "un concetto/disciplina/organizzazione che non rientra in questi tipi, usa "
            'subtype "" (stringa vuota) anziché forzare un tipo errato.'
            "\n- Massimo 4 entità: solo quelle centrali per il documento."
            f"{hint}\n\nAGENTS.md:\n{self.agents_md}"
        )
        user = f"Documento (id={doc_id}, titolo={title}):\n\n{body}"
        raw = self.llm.complete(system=system, user=user, max_tokens=1000)
        try:
            data = _extract_json(raw)
        except Exception as e:
            # Non bloccante: in caso di output malformato, l'ingest L2
            # degrada gracefully a L1 (pagina source presente, nessuna
            # integrazione entità).
            print(f"[WARN] parsing entità fallito ({e}); nessuna entità integrata.")
            return []

        # Dedup e validazione: il modello a volte ripete la stessa entità
        # o omette campi richiesti.
        entities = data.get("entities") or []
        clean = []
        seen = set()
        for ent in entities:
            eid = ent.get("id")
            # subtype può essere "" (concetti/organizzazioni fuori tassonomia):
            # NON è più un campo obbligatorio. Solo eid è discriminante.
            sub = ent.get("subtype") or ""
            if not eid or eid in seen:
                continue
            # Se il modello inventa un subtype non in whitelist, lo
            # normalizziamo a "" anziché propagare un valore non valido.
            if sub and sub not in VALID_SUBTYPES:
                sub = ""
            seen.add(eid)
            clean.append({"id": eid, "subtype": sub, "summary": ent.get("summary", "")})
        return clean

    def _integrate_entities(self, doc_id: str, title: str, body: str, hint_subtype: str | None, domain: str) -> list[str]:
        """Per ogni entità identificata nel doc, decide il branch (§13):
        aliased (solo update indice, no LLM), consolidamento (LLM su N source),
        merge incrementale (LLM su entity esistente + nuovo doc).

        Returns:
            Lista dei page_id materializzati o aggiornati. Le entità
            rimaste `aliased` NON compaiono qui (nessun file md creato).
        """
        # Query vector per la shortlist semantica dell'inventario gerarchico.
        # Riuso del vettore della source page (creata e embeddata appena prima
        # in _make_source_page): zero embedding aggiuntivo nel caso normale.
        # Fallback: embed di titolo + incipit se il vettore non è recuperabile.
        with self.tracker.phase("ingest:l2:inventory_retrieval"):
            q_emb = self.vdb.get_embedding(WIKI_COLLECTION, f"source_{doc_id}")
            if q_emb is None:
                head = " ".join(body.split()[:300])
                q_emb = self.embedder.embed(f"{title}\n\n{head}")

        with self.tracker.phase("ingest:l2:identify_entities"):
            entities = self._identify_entities(
                doc_id, title, body, hint_subtype, domain, query_embedding=q_emb
            )
        touched: list[str] = []

        for ent in entities:
            entity_id = ent["id"]
            subtype = ent["subtype"]

            # Step 1 (sempre): registra il contributo nell'indice.
            # Zero LLM. L'entità è già nota o appena registrata in stato `aliased`.
            self.entity_index.upsert_contribution(entity_id, doc_id, subtype, domain)
            entry = self.entity_index.get(entity_id)

            # Step 2: decide il branch in base allo stato + soglia.
            should_consolidate = self.entity_index.should_consolidate(entity_id)
            is_consolidated = entry["state"] == ENTITY_STATE_CONSOLIDATED

            if not should_consolidate and not is_consolidated:
                # CASO "aliased sotto soglia": niente file, niente LLM, niente vettore.
                # Solo update indice (già fatto sopra). L'entità è citabile via
                # [[entity_id]] e lato query verrà sintetizzata dalle source.
                continue

            # I due rami che richiedono chiamata LLM. Graceful skip su
            # content filter Azure (corpora narrativi): salta la singola
            # entità con audit log, non blocca l'ingest.
            try:
                if should_consolidate:
                    # CONSOLIDAMENTO: aliased → consolidated. Una sola chiamata
                    # LLM, input = N source page elencate nell'indice.
                    with self.tracker.phase("ingest:l2:entity_consolidate"):
                        new_body = self._consolidate_entity(
                            entity_id, subtype, entry["sources"], entry["domain"]
                        )
                    page_domain = entry["domain"]
                    op = "consolidate"
                else:
                    # MERGE INCREMENTALE: entità già consolidated, integra il nuovo doc.
                    with self.tracker.phase("ingest:l2:entity_merge"):
                        new_body = self._merge_entity_page(entity_id, doc_id, title, body, domain)
                    existing_fm, _ = self.wiki.get(entity_id)
                    current_domain = existing_fm.get("domain", DEFAULT_DOMAIN)
                    page_domain = current_domain if current_domain == domain else MIXED_DOMAIN
                    op = "merge"
            except BadRequestError as e:
                if _is_content_filter(e):
                    skip_op = "consolidate" if should_consolidate else "merge"
                    print(f"  [content_filter] skip entity '{entity_id}' ({skip_op}) "
                          f"per doc '{doc_id}' — log in {CONTENT_FILTER_SKIPS_PATH.name}")
                    _log_content_filter_skip(doc_id, entity_id, title, domain, op=skip_op)
                    # Importante: NON marchiamo consolidated se è fallito.
                    # L'entità resta aliased nell'indice e ritenterà al prossimo doc.
                    continue
                raise

            # Persistenza: salva file md, indicizza vettore, aggiorna indice.
            extra_meta = {
                "type": "entity",
                "subtype": subtype,
                "domain": page_domain,
                "sources": entry["sources"],
                "last_updated": date.today().isoformat(),
                "stale": False,
                "title": entity_id.replace("_", " ").title(),
            }
            # update_with_merge gestisce sia primo salvataggio (consolidate)
            # sia update di pagina esistente (merge incrementale).
            # Per il consolidate, le sources sono già nell'extra_meta e
            # vengono usate dal WikiStore.update_with_merge.
            self.wiki.update_with_merge(
                entity_id, new_body,
                [doc_id] if op == "merge" else [],  # consolidate: sources in extra_meta
                extra_meta=extra_meta,
            )
            with self.tracker.phase("ingest:l2:wiki_index"):
                self._index_wiki_page(entity_id)

            if op == "consolidate":
                self.entity_index.mark_consolidated(entity_id)
            touched.append(entity_id)

        # Persisti l'indice una sola volta a fine integrazione (batch save).
        self.entity_index.save()
        return touched

    def _consolidate_entity(self, entity_id: str, subtype: str,
                            source_ids: list[str], domain: str) -> str:
        """Genera una entity page consolidando N source page in una passata.

        È il sostituto del vecchio `_create_entity_page` per il caso "ho
        accumulato N source per questa entità e ora vale la pena
        materializzarla". Differenza chiave: invece di leggere il body del
        nuovo doc, legge i body delle N source page già nel WikiStore
        (sintesi già strutturate, contesto migliore di N raw grezzi).

        Args:
            entity_id: id dell'entità da consolidare.
            subtype: character|place|... o "" (concetto).
            source_ids: lista doc_id dei doc che hanno contribuito.
            domain: dominio dell'entità (o _mixed).

        Returns:
            Body markdown della entity page consolidata (frontmatter
            aggiunto dal chiamante).
        """
        subtype_label = subtype or "concetto/organizzazione (fuori tassonomia standard)"

        # Legge i body delle source page già nel WikiStore. Saltiamo le
        # mancanti (non dovrebbe succedere, ma safety): la generazione
        # avviene comunque sulle disponibili.
        source_blocks: list[str] = []
        for src_doc_id in source_ids:
            spid = f"source_{src_doc_id}"
            if not self.wiki.exists(spid):
                continue
            _fm, sbody = self.wiki.get(spid)
            source_blocks.append(f"### Source [[{src_doc_id}]]\n\n{sbody}")

        if not source_blocks:
            # Caso di edge: tutte le source page mancano. Fallback a
            # stringa minima invece di crashare.
            return (
                f"# {entity_id.replace('_', ' ').title()}\n\n"
                f"## Panoramica\n\nEntità identificata ma senza source disponibili "
                f"al momento del consolidamento.\n\n"
                f"## Dettagli\n\n## Relazioni\n\n## Domande aperte\n"
            )

        sources_csv = ", ".join(f"[[{sid}]]" for sid in source_ids)
        system = (
            f"Sei un curatore di un companion wiki di lettura, corpus '{domain}'. "
            f"Genera la pagina ENCICLOPEDICA per l'entità '{entity_id}' "
            f"(tipo: {subtype_label}) CONSOLIDANDO le {len(source_blocks)} "
            "source page elencate. Le source sono già sintesi strutturate dei "
            "documenti originali: combina le informazioni che convergono, "
            "menziona esplicitamente le contraddizioni se presenti.\n\n"
            "Struttura in markdown:\n"
            "# <Nome leggibile>\n\n"
            "## Panoramica\n\n## Dettagli\n\n## Relazioni\n\n"
            "## Domande aperte\n\n"
            "## Contraddizioni note   (opzionale, solo se le source divergono "
            "su un fatto specifico)\n\n"
            "Italiano, tono enciclopedico, terza persona. "
            f"Cita le sorgenti con [[doc_id]] usando esclusivamente gli id "
            f"forniti ({sources_csv}). Niente invenzioni: solo contenuti "
            "deducibili dalle source.\n\n"
            f"AGENTS.md:\n{self.agents_md}"
        )
        user = (
            f"Entity da consolidare: '{entity_id}' (subtype: {subtype or 'concetto'}, "
            f"domain: {domain}).\n\n"
            f"Source disponibili ({len(source_blocks)}):\n\n"
            + "\n\n---\n\n".join(source_blocks)
        )
        return self.llm.complete(system=system, user=user, max_tokens=2500)

    def _create_entity_page(self, page_id: str, subtype: str, doc_id: str, title: str, body: str, domain: str) -> str:
        """Genera ex-novo una pagina entity dal documento sorgente."""
        subtype_label = subtype or "concetto/organizzazione (fuori tassonomia standard)"
        system = (
            f"Sei un curatore di un companion wiki di lettura, corpus '{domain}'. "
            f"Crea una NUOVA pagina enciclopedica "
            f"per l'entità '{page_id}' (tipo: {subtype_label}) basandoti sul documento fornito. "
            "Struttura in markdown:\n"
            "# <nome leggibile>\n\n## Panoramica\n\n## Dettagli\n\n## Relazioni\n\n## Domande aperte\n\n"
            "Italiano, tono enciclopedico, terza persona. "
            f"Cita la sorgente come [[{doc_id}]]. Solo contenuti deducibili dal documento, niente invenzioni.\n\n"
            f"AGENTS.md:\n{self.agents_md}"
        )
        user = f"Documento sorgente (id={doc_id}, titolo={title}):\n\n{body}"
        return self.llm.complete(system=system, user=user, max_tokens=1500)

    def _merge_entity_page(self, page_id: str, doc_id: str, title: str, body: str, domain: str) -> str:
        """Fonde un nuovo documento in una pagina entity esistente.

        Operazione delicata: deve preservare le informazioni preesistenti
        ancora valide, aggiungere quelle nuove, e ESPLICITARE eventuali
        contraddizioni in una sezione "## Contraddizioni note". Questo è
        il meccanismo principale con cui il sistema gestisce le tensioni
        tra fonti diverse.
        """
        existing_fm, existing_body = self.wiki.get(page_id)
        existing_sources = existing_fm.get("sources") or []
        system = (
            f"Sei un curatore di un companion wiki di lettura, corpus '{domain}'. "
            f"Aggiorna la pagina esistente '{page_id}' "
            "integrando le informazioni del nuovo documento. Regole:\n"
            "- Mantieni la struttura: # titolo, ## Panoramica, ## Dettagli, ## Relazioni, ## Domande aperte.\n"
            "- Non rimuovere informazioni già presenti se restano valide.\n"
            "- Aggiungi nuove informazioni con la citazione della sorgente come [[doc_id]].\n"
            "- Se il nuovo documento CONTRADDICE qualcosa di esistente, aggiungi (o estendi) una sezione "
            "  '## Contraddizioni note' alla fine, citando entrambe le sorgenti in conflitto.\n"
            "- Italiano, terza persona, enciclopedico. Niente invenzioni.\n\n"
            f"AGENTS.md:\n{self.agents_md}"
        )
        user = (
            f"## Pagina esistente (sorgenti: {existing_sources})\n\n{existing_body}\n\n"
            f"---\n\n## Nuovo documento (id={doc_id}, titolo={title})\n\n{body}"
        )
        return self.llm.complete(system=system, user=user, max_tokens=2000)
