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
    AGENTS_MD_PATH, QUERY_LOG_PATH,
    RAW_COLLECTION, WIKI_COLLECTION,
    WIKI_TOP_K, RAW_TOP_K,
)
from .stores import WikiStore, VectorDB
from .embeddings import Embedder
from .llm_client import LLMClient
from .hot_layer import HotLayer


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
""".strip()


def _load_agents() -> str:
    """Carica il contratto operativo per iniettarlo nel system prompt."""
    return AGENTS_MD_PATH.read_text(encoding="utf-8") if AGENTS_MD_PATH.exists() else ""


def _parse_response(text: str) -> dict:
    """Estrae la struttura semantica dalla risposta dell'LLM.

    Convenzione: l'LLM termina sempre la risposta con un blocco JSON
    auto-descrittivo. Il parsing è tollerante: se l'estrazione fallisce,
    restituisce uno scheletro coerente con confidence=low, in modo che
    il chiamante non debba gestire eccezioni qui.

    Returns:
        Dict con campi: answer, wiki_sources, raw_sources, confidence,
        raw_used, gaps.
    """
    # La regex cattura un blocco JSON bilanciato a un livello di
    # annidamento. Sufficiente per il nostro schema piatto.
    # Si prende l'ULTIMO match perché l'LLM può menzionare {…} esemplificativi
    # nella spiegazione e poi emettere il vero JSON in coda.
    matches = list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL))
    if not matches:
        return {
            "answer": text.strip(),
            "wiki_sources": [],
            "raw_sources": [],
            "confidence": "low",
            "raw_used": False,
        }
    try:
        data = json.loads(matches[-1].group(0))
    except Exception:
        # JSON malformato: usiamo l'intero testo come risposta e segnaliamo
        # confidence low. Meglio degradare graceful che fallire.
        return {
            "answer": text.strip(),
            "wiki_sources": [],
            "raw_sources": [],
            "confidence": "low",
            "raw_used": False,
        }
    return {
        "answer": data.get("answer", "").strip(),
        "wiki_sources": data.get("wiki_sources") or [],
        "raw_sources": data.get("raw_sources") or [],
        "confidence": data.get("confidence", "low"),
        # raw_used è derivato (non chiesto al modello): se ha citato almeno
        # una sorgente raw, allora l'indice raw è stato effettivamente utile.
        "raw_used": bool(data.get("raw_sources")),
        "gaps": data.get("gaps", ""),
    }


class QueryPipeline:
    """Pipeline end-to-end per rispondere a una domanda dell'utente.

    Inizializza una sola volta i client e li riusa per tutte le query
    (utile in modalità batch via --eval).
    """

    def __init__(self, llm: LLMClient | None = None, embedder: Embedder | None = None):
        """Inizializza pipeline. Client opzionali per testing con mock."""
        self.wiki = WikiStore()
        self.vdb = VectorDB()
        self.llm = llm or LLMClient()
        self.embedder = embedder or Embedder()
        self.hot = HotLayer(self.wiki, self.llm)
        self.agents_md = _load_agents()

    def ask(self, question: str) -> dict:
        """Esegue la pipeline completa per una singola domanda.

        Side effect: appende un record nell'audit log.

        Returns:
            Dict con i campi parsati dalla risposta dell'LLM, arricchito
            di question e timestamp.
        """
        # Step 2-3: orientamento + retrieval multi-indice.
        q_emb = self.embedder.embed(question)
        wiki_hits = self.vdb.query(WIKI_COLLECTION, q_emb, WIKI_TOP_K)
        raw_hits = self.vdb.query(RAW_COLLECTION, q_emb, RAW_TOP_K)

        hot_layer = self.hot.load()

        # Formattazione dei hit per il prompt. Tagliamo i chunk molto lunghi
        # (raro a 200 parole, ma succede su pagine wiki dense) per contenere
        # il context.
        wiki_block = self._format_hits(wiki_hits, kind="WIKI")
        raw_block = self._format_hits(raw_hits, kind="RAW")

        # Il system prompt è la "configurazione runtime" della pipeline:
        # contiene la strategia, le regole di conflitto, il contratto
        # operativo e il Hot Layer. Volutamente lungo: questi sono i
        # vincoli che il modello deve avere sotto gli occhi prima di
        # generare la risposta.
        system = (
            "Sei l'assistente del companion wiki Tolkien. Rispondi a domande dell'utente "
            "usando il Hot Layer per orientarti e i risultati del retrieval doppio (wiki + raw).\n\n"
            "STRATEGIA:\n"
            "1. Orientati nel Hot Layer per capire quali pagine sono rilevanti.\n"
            "2. Usa le pagine wiki per sintesi e relazioni.\n"
            "3. Usa i frammenti raw per dettagli precisi, citazioni testuali, numeri/date.\n"
            "4. Applica le regole di risoluzione conflitti.\n"
            "5. Cita SEMPRE le sorgenti effettivamente usate.\n"
            "6. Se la risposta non è derivabile, dillo esplicitamente con confidence=low e segnala il gap.\n\n"
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
        full = self.llm.complete(system=system, user=user, max_tokens=2000)
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
