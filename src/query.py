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
    return AGENTS_MD_PATH.read_text(encoding="utf-8") if AGENTS_MD_PATH.exists() else ""


def _parse_response(text: str) -> dict:
    """Estrae il JSON finale prodotto dall'LLM. Tollerante a testo prima/dopo."""
    # Cerca l'ultimo blocco JSON nel testo
    matches = list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL))
    if not matches:
        return {"answer": text.strip(), "wiki_sources": [], "raw_sources": [], "confidence": "low", "raw_used": False}
    try:
        data = json.loads(matches[-1].group(0))
    except Exception:
        return {"answer": text.strip(), "wiki_sources": [], "raw_sources": [], "confidence": "low", "raw_used": False}
    return {
        "answer": data.get("answer", "").strip(),
        "wiki_sources": data.get("wiki_sources") or [],
        "raw_sources": data.get("raw_sources") or [],
        "confidence": data.get("confidence", "low"),
        "raw_used": bool(data.get("raw_sources")),
        "gaps": data.get("gaps", ""),
    }


class QueryPipeline:
    def __init__(self, llm: LLMClient | None = None, embedder: Embedder | None = None):
        self.wiki = WikiStore()
        self.vdb = VectorDB()
        self.llm = llm or LLMClient()
        self.embedder = embedder or Embedder()
        self.hot = HotLayer(self.wiki, self.llm)
        self.agents_md = _load_agents()

    def ask(self, question: str) -> dict:
        q_emb = self.embedder.embed(question)
        wiki_hits = self.vdb.query(WIKI_COLLECTION, q_emb, WIKI_TOP_K)
        raw_hits = self.vdb.query(RAW_COLLECTION, q_emb, RAW_TOP_K)

        hot_layer = self.hot.load()

        wiki_block = self._format_hits(wiki_hits, kind="WIKI")
        raw_block = self._format_hits(raw_hits, kind="RAW")

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
            f"{CONFLICT_RULES}\n\n"
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

        full = self.llm.complete(system=system, user=user, max_tokens=2000)
        parsed = _parse_response(full)
        parsed["question"] = question
        parsed["timestamp"] = datetime.now().isoformat(timespec="seconds")
        self._log(parsed)
        return parsed

    def _format_hits(self, hits: list[dict], kind: str) -> str:
        if not hits:
            return "(nessun risultato)"
        out = []
        for h in hits:
            meta = h.get("metadata") or {}
            label = meta.get("page_id") or meta.get("doc_id") or h["id"]
            extra = ""
            if kind == "RAW":
                extra = f" (chunk {meta.get('chunk_idx', '?')}, titolo: {meta.get('title','')})"
            text = h["text"]
            if len(text) > 1200:
                text = text[:1200] + "…"
            out.append(f"### [{kind}] {label}{extra}\n{text}")
        return "\n\n".join(out)

    def _log(self, entry: dict) -> None:
        QUERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with QUERY_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
