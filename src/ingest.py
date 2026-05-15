from __future__ import annotations
import json
import re
from datetime import datetime, date
from pathlib import Path

from .config import (
    AGENTS_MD_PATH, CHUNK_SIZE_WORDS, CHUNK_OVERLAP_WORDS,
    RAW_COLLECTION, WIKI_COLLECTION, VALID_LEVELS,
)
from .stores import RawStore, WikiStore, VectorDB
from .embeddings import Embedder
from .llm_client import LLMClient
from .hot_layer import HotLayer


_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _slug_re.sub("_", text.lower()).strip("_")
    return s or "doc"


def chunk_text(text: str, size: int = CHUNK_SIZE_WORDS, overlap: int = CHUNK_OVERLAP_WORDS) -> list[str]:
    words = text.split()
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]
    chunks = []
    step = max(1, size - overlap)
    for start in range(0, len(words), step):
        end = start + size
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
    return chunks


def _load_agents() -> str:
    return AGENTS_MD_PATH.read_text(encoding="utf-8") if AGENTS_MD_PATH.exists() else ""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"Nessun JSON trovato nella risposta:\n{text}")
    return json.loads(m.group(0))


class IngestPipeline:
    def __init__(self, llm: LLMClient | None = None, embedder: Embedder | None = None):
        self.raw = RawStore()
        self.wiki = WikiStore()
        self.vdb = VectorDB()
        self.embedder = embedder or Embedder()
        self.llm = llm or LLMClient()
        self.hot = HotLayer(self.wiki, self.llm)
        self.agents_md = _load_agents()

    # ---------- pubblico ----------
    def ingest(self, file_path: str | Path, title: str, level: str, subtype: str | None = None) -> dict:
        if level not in VALID_LEVELS:
            raise ValueError(f"Livello invalido: {level}. Ammessi: {VALID_LEVELS}")
        path = Path(file_path)
        body = path.read_text(encoding="utf-8").strip()
        if not body:
            raise ValueError(f"File vuoto: {path}")

        doc_id = f"{slugify(title)}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        metadata = {
            "title": title,
            "source": str(path.name),
            "level": level,
            "domain": "tolkien",
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
        }
        if subtype:
            metadata["subtype"] = subtype

        # 1. Raw store
        self.raw.save(doc_id, body, metadata)

        # 2. Raw index (sempre)
        self._index_raw(doc_id, title, body)

        touched_pages: list[str] = []

        # 3. Livelli
        if level == "L0":
            pass
        elif level == "L1":
            source_page = self._make_source_page(doc_id, title, body)
            touched_pages.append(source_page)
        elif level == "L2":
            source_page = self._make_source_page(doc_id, title, body)
            touched_pages.append(source_page)
            entity_pages = self._integrate_entities(doc_id, title, body, hint_subtype=subtype)
            touched_pages.extend(entity_pages)

        # 4. Hot layer (solo se è cambiato qualcosa nella wiki)
        if touched_pages:
            self.hot.rebuild()

        return {"doc_id": doc_id, "level": level, "wiki_pages": touched_pages}

    # ---------- helpers ----------
    def _index_raw(self, doc_id: str, title: str, body: str) -> None:
        chunks = chunk_text(body)
        if not chunks:
            return
        ids = [f"{doc_id}__chunk_{i:03d}" for i in range(len(chunks))]
        metas = [{"doc_id": doc_id, "title": title, "chunk_idx": i, "kind": "raw"} for i in range(len(chunks))]
        embs = self.embedder.embed_batch(chunks)
        self.vdb.add(RAW_COLLECTION, ids, embs, chunks, metas)

    def _index_wiki_page(self, page_id: str) -> None:
        fm, body = self.wiki.get(page_id)
        emb = self.embedder.embed(f"{page_id}\n\n{body}")
        meta = {
            "page_id": page_id,
            "type": fm.get("type", ""),
            "subtype": fm.get("subtype", "") or "",
            "kind": "wiki",
        }
        self.vdb.delete(WIKI_COLLECTION, [page_id])
        self.vdb.add(WIKI_COLLECTION, [page_id], [emb], [body], [meta])

    def _make_source_page(self, doc_id: str, title: str, body: str) -> str:
        page_id = f"source_{doc_id}"
        system = (
            "Sei un curatore del companion wiki Tolkien. Produci una sintesi narrativa "
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
            "last_updated": date.today().isoformat(),
            "confidence": "medium",
            "stale": False,
            "title": f"Sintesi: {title}",
        }
        self.wiki.save(page_id, summary, metadata)
        self._index_wiki_page(page_id)
        return page_id

    def _identify_entities(self, doc_id: str, title: str, body: str, hint_subtype: str | None) -> list[dict]:
        hint = f"\nIl curatore suggerisce che l'entità principale è di tipo: {hint_subtype}." if hint_subtype else ""
        system = (
            "Sei l'estrattore di entità del companion wiki Tolkien. "
            "Identifica le entità rilevanti citate nel documento. "
            "Rispondi SOLO con JSON valido nel formato:\n"
            '{"entities": [{"id": "snake_case_en", "type": "entity", "subtype": "character|place|artifact|event|book", "summary": "1-2 frasi"}]}'
            "\n- id: slug inglese in snake_case (es. frodo_baggins, one_ring, council_of_elrond)."
            "\n- subtype: uno tra character, place, artifact, event, book."
            "\n- Massimo 5 entità, solo quelle veramente trattate dal documento (non semplici menzioni di passaggio)."
            f"{hint}\n\nAGENTS.md:\n{self.agents_md}"
        )
        user = f"Documento (id={doc_id}, titolo={title}):\n\n{body}"
        raw = self.llm.complete(system=system, user=user, max_tokens=1000)
        try:
            data = _extract_json(raw)
        except Exception as e:
            print(f"[WARN] parsing entità fallito ({e}); nessuna entità integrata.")
            return []
        entities = data.get("entities") or []
        clean = []
        seen = set()
        for ent in entities:
            eid = ent.get("id")
            sub = ent.get("subtype")
            if not eid or not sub or eid in seen:
                continue
            seen.add(eid)
            clean.append({"id": eid, "subtype": sub, "summary": ent.get("summary", "")})
        return clean

    def _integrate_entities(self, doc_id: str, title: str, body: str, hint_subtype: str | None) -> list[str]:
        entities = self._identify_entities(doc_id, title, body, hint_subtype)
        touched: list[str] = []
        for ent in entities:
            page_id = ent["id"]
            subtype = ent["subtype"]
            if self.wiki.exists(page_id):
                merged = self._merge_entity_page(page_id, doc_id, title, body)
            else:
                merged = self._create_entity_page(page_id, subtype, doc_id, title, body)
            extra_meta = {
                "type": "entity",
                "subtype": subtype,
                "last_updated": date.today().isoformat(),
                "stale": False,
                "title": page_id.replace("_", " ").title(),
            }
            self.wiki.update_with_merge(page_id, merged, [doc_id], extra_meta=extra_meta)
            self._index_wiki_page(page_id)
            touched.append(page_id)
        return touched

    def _create_entity_page(self, page_id: str, subtype: str, doc_id: str, title: str, body: str) -> str:
        system = (
            f"Sei un curatore del companion wiki Tolkien. Crea una NUOVA pagina enciclopedica "
            f"per l'entità '{page_id}' (tipo: {subtype}) basandoti sul documento fornito. "
            "Struttura in markdown:\n"
            "# <nome leggibile>\n\n## Panoramica\n\n## Dettagli\n\n## Relazioni\n\n## Domande aperte\n\n"
            "Italiano, tono enciclopedico, terza persona. "
            f"Cita la sorgente come [[{doc_id}]]. Solo contenuti deducibili dal documento, niente invenzioni.\n\n"
            f"AGENTS.md:\n{self.agents_md}"
        )
        user = f"Documento sorgente (id={doc_id}, titolo={title}):\n\n{body}"
        return self.llm.complete(system=system, user=user, max_tokens=1500)

    def _merge_entity_page(self, page_id: str, doc_id: str, title: str, body: str) -> str:
        existing_fm, existing_body = self.wiki.get(page_id)
        existing_sources = existing_fm.get("sources") or []
        system = (
            f"Sei un curatore del companion wiki Tolkien. Aggiorna la pagina esistente '{page_id}' "
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
