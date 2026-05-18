"""
Pipeline di query multi-indice (sezione 6.2 del design).

Flusso a 7 step:
1. Identità e permessi  -> non implementato nello skeleton (single user).
2. Orientamento         -> caricamento del Hot Layer come parte del system prompt.
3. Retrieval multi-indice -> top-k da wiki_pages e raw_chunks in parallelo.
4. Filtro permessi      -> non implementato nello skeleton.
5. Risoluzione conflitti -> istruzioni esplicite nel prompt (CONFLICT_RULES).
6. Sintesi risposta     -> singola chiamata LLM con tutto il contesto.
7. Log + feedback       -> append a data/query_log.jsonl (audit trail).

La risoluzione conflitti è "in-context" (regole nel prompt) e non
"deterministica" (codice Python): per il walking skeleton è sufficiente.
Nelle fasi successive si valuterà un secondo passaggio dedicato per
conflitti numerici critici (compliance, audit).
"""

from __future__ import annotations
import json
import re
from datetime import datetime

from .config import (
    AGENTS_MD_PATH, QUERY_LOG_PATH, TOKEN_LOG_PATH,
    RAW_COLLECTION, WIKI_COLLECTION,
    WIKI_TOP_K, RAW_TOP_K, MIXED_DOMAIN,
)
from .stores import WikiStore, VectorDB
from .embeddings import Embedder
from .llm_client import LLMClient
from .hot_layer import HotLayer
from .token_tracker import TokenTracker


# Tabella delle regole di risoluzione conflitti (sezione 6.2 del design).
# Iniettata nel system prompt di OGNI query. Volutamente sintetica per
# restare leggibile dal modello senza saturare il contesto.
CONFLICT_RULES = """
REGOLE DI RISOLUZIONE CONFLITTI (sezione 6.2 del design):
- Numeri specifici (date, cifre, codici)   -> RAW autoritativo
- Citazioni testuali                       -> RAW autoritativo
- Stati attuali (cosa è vero ora)          -> RAW se più recente, WIKI se aggrega più fonti
- Sintesi e interpretazioni                -> WIKI autoritativa
- Relazioni e collegamenti                 -> WIKI autoritativa

Se WIKI e RAW divergono su un fatto, NON nascondere il conflitto: esplicita
"secondo [[wiki_page]]… ; secondo il documento [[doc_id]]…" e indica quale
prevale secondo le regole.

CONFLITTO RAW-vs-RAW (due documenti grezzi che divergono sullo stesso fatto):
le regole sopra disambiguano solo WIKI-vs-RAW. Se a divergere sono DUE fonti
RAW (es. due documenti che danno un numero diverso) e nessuna è chiaramente
più recente o più autorevole per provenienza, NON sceglierne arbitrariamente
una: riporta ENTRAMBI i valori con le rispettive citazioni, dichiara il
conflitto irrisolto, e abbassa la confidence ad al massimo "medium"
(o "low" se il fatto è centrale per la domanda).
""".strip()


def _load_agents() -> str:
    """Carica il contratto operativo per iniettarlo nel system prompt."""
    return AGENTS_MD_PATH.read_text(encoding="utf-8") if AGENTS_MD_PATH.exists() else ""


# Regex per i wikilink `[[id]]`. Usata sia per il fallback parser (quando il
# JSON finale è assente perché la risposta è stata troncata) sia per il check
# di validità delle citazioni a valle.
_WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9_]+)\]\]")

# Euristica per distinguere doc_id raw da page_id wiki guardando solo l'id:
# i doc_id terminano sempre con un suffisso di timestamp `_AAAAMMGGHHMMSS`
# (14 cifre) generato da IngestPipeline.ingest(). Le page_id wiki no.
_DOC_ID_SUFFIX_RE = re.compile(r"_\d{14}$")


def _split_links_by_kind(ids: list[str]) -> tuple[list[str], list[str]]:
    """Divide una lista di id in (wiki_page_ids, raw_doc_ids) con euristica.

    Usato come fallback quando il modello non ha emesso il blocco JSON
    finale (es. risposta troncata a max_tokens).
    """
    wiki, raw = [], []
    seen = set()
    for _id in ids:
        if _id in seen:
            continue
        seen.add(_id)
        if _DOC_ID_SUFFIX_RE.search(_id):
            raw.append(_id)
        else:
            wiki.append(_id)
    return wiki, raw


def _parse_response(text: str) -> dict:
    """Estrae la struttura semantica dalla risposta dell'LLM.

    Convenzione: l'LLM termina sempre la risposta con un blocco JSON
    auto-descrittivo. Il parsing è tollerante e applica un fallback a
    cascata:
    1. Cerca un blocco JSON valido in coda al testo.
    2. Se manca o è malformato, estrae gli id dai wikilink `[[...]]`
       presenti nel body e li classifica con euristica wiki/raw.
       In questo caso la confidence viene forzata a "low" perché il
       contratto di output non è stato rispettato (anche se i contenuti
       sono presenti).

    Returns:
        Dict con campi: answer, wiki_sources, raw_sources, confidence,
        raw_used, gaps.
    """
    # 1) Path nominale: blocco JSON in coda.
    matches = list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL))
    if matches:
        try:
            data = json.loads(matches[-1].group(0))
            return {
                "answer": data.get("answer", "").strip() or text.strip(),
                "wiki_sources": data.get("wiki_sources") or [],
                "raw_sources": data.get("raw_sources") or [],
                "confidence": data.get("confidence", "low"),
                "raw_used": bool(data.get("raw_sources")),
                "gaps": data.get("gaps", ""),
            }
        except Exception:
            # Cade nel fallback sottostante.
            pass

    # 2) Fallback: estrai i wikilink inline dal corpo della risposta.
    # Tipico caso: risposta troncata a max_tokens prima del blocco JSON.
    # Meglio mostrare le citazioni che il modello ha effettivamente fatto
    # piuttosto che dichiarare "nessuna sorgente" su una risposta ricca.
    inline_ids = _WIKILINK_RE.findall(text)
    wiki, raw = _split_links_by_kind(inline_ids)
    return {
        "answer": text.strip(),
        "wiki_sources": wiki,
        "raw_sources": raw,
        # Confidence forzata a low: senza JSON non sappiamo cosa il modello
        # giudichi affidabile. Segnaliamo onestamente il degrado.
        "confidence": "low",
        "raw_used": bool(raw),
        "gaps": "(JSON finale mancante: sorgenti ricostruite da wikilink inline)",
    }


class QueryPipeline:
    """Pipeline end-to-end per rispondere a una domanda dell'utente.

    Inizializza una sola volta i client e li riusa per tutte le query
    (utile in modalità batch via --eval).
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        embedder: Embedder | None = None,
        tracker: TokenTracker | None = None,
    ):
        """Inizializza pipeline. Client opzionali per testing con mock.

        Il TokenTracker è condiviso tra LLM ed Embedder così che le
        chiamate dentro un `with self.tracker.phase(...)` vengano tutte
        etichettate con la stessa fase.
        """
        self.tracker = tracker or TokenTracker(log_path=TOKEN_LOG_PATH)
        self.wiki = WikiStore()
        self.vdb = VectorDB()
        self.llm = llm or LLMClient(tracker=self.tracker)
        self.embedder = embedder or Embedder(tracker=self.tracker)
        self.hot = HotLayer(self.wiki, self.llm)
        self.agents_md = _load_agents()

    def _corpus_domains(self) -> set[str]:
        """Insieme dei domini presenti nel corpus (scan frontmatter wiki).

        Solo filesystem, nessun costo token. Usato per rilevare se il
        corpus è multi-dominio e quindi una query senza filtro rischia la
        "dominanza silenziosa" (caso x08: una domanda generica risponde da
        un solo corpus senza segnalare che ne esistono altri).
        """
        domains: set[str] = set()
        for pid in self.wiki.list():
            fm, _ = self.wiki.get(pid)
            d = fm.get("domain")
            if d and d != MIXED_DOMAIN:
                domains.add(d)
        return domains

    def ask(self, question: str, domain: str | None = None) -> dict:
        """Esegue la pipeline completa per una singola domanda.

        Args:
            question: la domanda in linguaggio naturale.
            domain: opzionale. Se presente, filtra il retrieval ai
                soli chunk/pagine di quel dominio (più "_mixed" per il
                wiki, che può contenere informazioni rilevanti anche se
                aggregate da più domini). Se None, nessun filtro: il
                retrieval considera tutto il corpus.

        Side effect: appende un record nell'audit log.

        Returns:
            Dict con i campi parsati dalla risposta dell'LLM, arricchito
            di question e timestamp.
        """
        # Step 2-3: orientamento + retrieval multi-indice.
        # Fasi distinte per separare il costo di embedding della domanda
        # (sempre 1 chiamata) dalla generazione finale.
        with self.tracker.phase("query:embedding"):
            q_emb = self.embedder.embed(question)
        # Filtri ChromaDB: applicati lato vector DB così che top-k operi
        # già sul sottoinsieme rilevante (evita post-filter che ridurrebbe
        # arbitrariamente i risultati utili).
        raw_where: dict | None = None
        wiki_where: dict | None = None
        if domain:
            # Raw: filtro stretto sul dominio richiesto.
            raw_where = {"domain": domain}
            # Wiki: include anche le pagine "_mixed" perché un'entità
            # condivisa tra domini può comunque essere rilevante (es. un
            # personaggio nominato in più corpus).
            wiki_where = {"domain": {"$in": [domain, MIXED_DOMAIN]}}
        # Il retrieval su ChromaDB non consuma token (è solo similarity
        # search locale), quindi qui non c'è fase token-tracking. Resta
        # la fase logica "retrieval" per coerenza concettuale.
        wiki_hits = self.vdb.query(WIKI_COLLECTION, q_emb, WIKI_TOP_K, where=wiki_where)
        raw_hits = self.vdb.query(RAW_COLLECTION, q_emb, RAW_TOP_K, where=raw_where)

        hot_layer = self.hot.load()

        # Formattazione dei hit per il prompt. Tagliamo i chunk molto lunghi
        # (raro a 200 parole, ma succede su pagine wiki dense) per contenere
        # il context.
        wiki_block = self._format_hits(wiki_hits, kind="WIKI")
        raw_block = self._format_hits(raw_hits, kind="RAW")

        # Whitelist delle citazioni ammesse: solo gli id effettivamente
        # recuperati. Senza questa lista il modello inventa wikilink
        # plausibili ma inesistenti (osservato: morgoth, rings_of_power,
        # war_of_the_ring...). Con la lista esplicita la hallucination
        # cala drasticamente.
        allowed_wiki = sorted({(h.get("metadata") or {}).get("page_id") or h["id"] for h in wiki_hits})
        allowed_raw = sorted({(h.get("metadata") or {}).get("doc_id") or h["id"] for h in raw_hits})
        allowed_wiki = [w for w in allowed_wiki if w]
        allowed_raw = [r for r in allowed_raw if r]

        # Policy multi-corpus (fix caso x08: dominanza silenziosa).
        # Si attiva SOLO quando non è stato applicato un filtro --domain
        # e il corpus contiene più di un dominio. Costa solo uno scan
        # filesystem, nessun token aggiuntivo se non il blocco di prompt.
        domain_policy = ""
        if not domain:
            corpus_domains = self._corpus_domains()
            if len(corpus_domains) > 1:
                retrieved_domains = sorted({
                    (h.get("metadata") or {}).get("domain")
                    for h in (wiki_hits + raw_hits)
                    if (h.get("metadata") or {}).get("domain")
                })
                others = sorted(corpus_domains - set(retrieved_domains))
                domain_policy = (
                    "POLICY MULTI-CORPUS (nessun filtro dominio applicato):\n"
                    f"- Il corpus contiene più corpora distinti: {sorted(corpus_domains)}.\n"
                    f"- Il contesto retrieved copre i domini: {retrieved_domains or '(nessuno)'}.\n"
                    "- Se la domanda è generica e il contesto copre PIÙ domini: struttura "
                    "la risposta per dominio (sezioni separate, una per corpus) e NON "
                    "fonderli; cap confidence a 'medium'.\n"
                    "- Se il contesto copre UN SOLO dominio ma il corpus ne contiene altri "
                    f"({others or 'nessun altro'}): rispondi per quel dominio MA dichiara "
                    "esplicitamente che la risposta copre solo quel corpus e che gli altri "
                    "corpora potrebbero contenere una risposta diversa; cap confidence a "
                    "'medium', salvo che la domanda nomini esplicitamente entità/termini "
                    "univoci di quel dominio.\n\n"
                )

        # Il system prompt è la "configurazione runtime" della pipeline:
        # contiene la strategia, le regole di conflitto, il contratto
        # operativo e il Hot Layer. Volutamente lungo: questi sono i
        # vincoli che il modello deve avere sotto gli occhi prima di
        # generare la risposta.
        system = (
            "Sei l'assistente di un companion wiki di lettura multi-corpus. Rispondi a domande dell'utente "
            "usando il Hot Layer per orientarti e i risultati del retrieval doppio (wiki + raw).\n\n"
            "STRATEGIA:\n"
            "1. Orientati nel Hot Layer per capire quali pagine sono rilevanti.\n"
            "2. Usa le pagine wiki per sintesi e relazioni.\n"
            "3. Usa i frammenti raw per dettagli precisi, citazioni testuali, numeri/date.\n"
            "4. Applica le regole di risoluzione conflitti.\n"
            "5. Cita SEMPRE le sorgenti effettivamente usate.\n"
            "6. Se la risposta non è derivabile, dillo esplicitamente con confidence=low e segnala il gap.\n\n"
            + domain_policy +
            # Lo "scan obbligatorio" forza il modello a fare una passata
            # ATTIVA sui frammenti per cercare discrepanze. Senza questo
            # vincolo, il modello tende a fidarsi del primo risultato
            # ad alto ranking e a ignorare contraddizioni in chunk marginali
            # (osservato in test: caso 1601 vs 1604 sulla Contea).
            "SCAN OBBLIGATORIO DEI CONFLITTI:\n"
            "PRIMA di formulare la risposta, scorri TUTTI i frammenti retrieved (wiki E raw) e verifica se "
            "su numeri, date, cifre o nomi compaiono valori diversi per lo stesso fatto. Includi anche "
            "i frammenti che sembrano marginali rispetto alla domanda principale: una menzione di passaggio "
            "che contraddice una sintesi è un segnale, non rumore. Se trovi una discrepanza, NON sceglierne "
            "una sola e nasconderla: esplicita entrambe le versioni nella risposta, cita le rispettive "
            "sorgenti, e indica quale prevale secondo le regole di risoluzione conflitti.\n\n"
            # Whitelist + regole di formato citazione. Critico per evitare
            # le hallucination di wikilink osservate sulle risposte lunghe.
            "REGOLE DI CITAZIONE (vincolanti):\n"
            "- Puoi citare con `[[id]]` SOLO id presenti nella whitelist sottostante. "
            "  Se vuoi riferirti a un'entità non in whitelist, scrivila in testo piano "
            "  SENZA wikilink (es. 'Morgoth', non '[[morgoth]]').\n"
            "- Usa `[[page_id_wiki]]` per concetti, sintesi e relazioni.\n"
            "- Usa `[[doc_id_raw]]` (quelli con suffisso _AAAAMMGGHHMMSS) per dettagli "
            "  puntuali, date, citazioni testuali.\n"
            "- Non duplicare wiki+raw nello stesso punto se rimandano allo stesso fatto: "
            "  scegline UNO secondo le regole di risoluzione conflitti.\n"
            "WIKI page_id ammessi: " + (", ".join(allowed_wiki) if allowed_wiki else "(nessuno)") + "\n"
            "RAW  doc_id  ammessi: " + (", ".join(allowed_raw) if allowed_raw else "(nessuno)") + "\n\n"
            "COMPATTEZZA:\n"
            "La risposta finale (incluso il blocco JSON) deve stare entro ~600 parole. "
            "Se l'argomento è vasto, dai priorità ai fatti essenziali e cita le pagine wiki "
            "per gli approfondimenti, senza riprodurle integralmente. L'output deve SEMPRE "
            "terminare con il blocco JSON: meglio una risposta più breve ma completa di JSON "
            "che una lunga ma troncata.\n\n"
            f"{CONFLICT_RULES}\n\n"
            # Il blocco JSON finale è il "contratto di output" che permette
            # al parser di estrarre dati strutturati senza euristiche fragili.
            "FORMATO DI OUTPUT: dopo la risposta in markdown, includi UN UNICO blocco JSON finale:\n"
            '{"answer": "<la stessa risposta in markdown>", '
            '"wiki_sources": ["page_id1", ...], '
            '"raw_sources": ["doc_id1", ...], '
            '"confidence": "high|medium|low", '
            '"gaps": "<eventuali lacune notate>"}\n\n'
            f"AGENTS.md:\n{self.agents_md}\n\n"
            f"=== HOT LAYER ===\n{hot_layer}"
        )

        user = (
            f"## Domanda\n{question}\n\n"
            f"## Risultati retrieval — WIKI (top {len(wiki_hits)})\n{wiki_block}\n\n"
            f"## Risultati retrieval — RAW (top {len(raw_hits)})\n{raw_block}\n"
        )

        # Singola chiamata LLM: tutto il contesto è già preparato.
        # È la fase più costosa di una query: l'intero context (Hot Layer
        # + AGENTS.md + wiki hits + raw hits) viene passato al modello.
        # max_tokens=3500 lascia margine al modello per chiudere il blocco
        # JSON anche su risposte ricche. Sotto i 3000 si è osservato
        # truncation su domande aperte (es. "cosa sappiamo su X in dettaglio?").
        with self.tracker.phase("query:llm_synthesis"):
            full = self.llm.complete(system=system, user=user, max_tokens=3500)
        parsed = _parse_response(full)
        parsed["question"] = question
        parsed["timestamp"] = datetime.now().isoformat(timespec="seconds")
        self._log(parsed)
        return parsed

    def _format_hits(self, hits: list[dict], kind: str) -> str:
        """Serializza i hit del vector store in markdown leggibile dall'LLM.

        Args:
            kind: "WIKI" o "RAW", influenza l'etichetta e il livello di
                dettaglio dei metadati esposti.
        """
        if not hits:
            return "(nessun risultato)"
        out = []
        for h in hits:
            meta = h.get("metadata") or {}
            # Cerca prima il page_id (wiki), poi il doc_id (raw), poi
            # fallback sull'id ChromaDB.
            label = meta.get("page_id") or meta.get("doc_id") or h["id"]
            extra = ""
            if kind == "RAW":
                # Per i raw esponiamo chunk_idx e titolo originale: aiutano
                # il modello a capire la posizione e il contesto del chunk.
                extra = f" (chunk {meta.get('chunk_idx', '?')}, titolo: {meta.get('title','')})"
            text = h["text"]
            # Cap a 1200 caratteri per pagina wiki dense: evita di saturare
            # il context su un singolo hit.
            if len(text) > 1200:
                text = text[:1200] + "…"
            out.append(f"### [{kind}] {label}{extra}\n{text}")
        return "\n\n".join(out)

    def _log(self, entry: dict) -> None:
        """Appende un record al log delle query (audit trail minimo).

        Formato JSONL (una riga per evento): facile da grep/jq, append-only,
        ricostruibile in dataset di valutazione. Seme dell'audit trail
        previsto dalla sezione 7.5 del design.
        """
        QUERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with QUERY_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
