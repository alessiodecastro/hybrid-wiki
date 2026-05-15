from __future__ import annotations
from pathlib import Path
import yaml
import chromadb
from .config import (
    RAW_DIR, WIKI_DIR, VECTORS_DIR, HOT_LAYER_PATH,
    RAW_COLLECTION, WIKI_COLLECTION,
)


def _write_md(path: Path, frontmatter: dict, body: str) -> None:
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{fm}\n---\n\n{body.strip()}\n", encoding="utf-8")


def _read_md(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        _, fm_block, body = text.split("---", 2)
        return yaml.safe_load(fm_block) or {}, body.strip()
    return {}, text.strip()


class RawStore:
    def __init__(self, root: Path = RAW_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, doc_id: str) -> Path:
        return self.root / f"{doc_id}.md"

    def exists(self, doc_id: str) -> bool:
        return self._path(doc_id).exists()

    def save(self, doc_id: str, content: str, metadata: dict) -> Path:
        path = self._path(doc_id)
        _write_md(path, {"id": doc_id, **metadata}, content)
        return path

    def get(self, doc_id: str) -> tuple[dict, str]:
        return _read_md(self._path(doc_id))

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.md"))


class WikiStore:
    RESERVED = {"HOT_LAYER"}

    def __init__(self, root: Path = WIKI_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, page_id: str) -> Path:
        return self.root / f"{page_id}.md"

    def exists(self, page_id: str) -> bool:
        return self._path(page_id).exists()

    def save(self, page_id: str, content: str, metadata: dict) -> Path:
        path = self._path(page_id)
        _write_md(path, {"id": page_id, **metadata}, content)
        return path

    def get(self, page_id: str) -> tuple[dict, str]:
        return _read_md(self._path(page_id))

    def list(self) -> list[str]:
        out = []
        for p in self.root.glob("*.md"):
            if p.stem in self.RESERVED:
                continue
            out.append(p.stem)
        return sorted(out)

    def update_with_merge(self, page_id: str, new_content: str, new_sources: list[str], extra_meta: dict | None = None) -> Path:
        fm, _ = self.get(page_id) if self.exists(page_id) else ({}, "")
        sources = list(dict.fromkeys((fm.get("sources") or []) + new_sources))
        fm["sources"] = sources
        if extra_meta:
            fm.update(extra_meta)
        return self.save(page_id, new_content, {k: v for k, v in fm.items() if k != "id"})


class VectorDB:
    def __init__(self, root: Path = VECTORS_DIR):
        self.client = chromadb.PersistentClient(path=str(root))
        self.collections = {
            RAW_COLLECTION: self.client.get_or_create_collection(RAW_COLLECTION),
            WIKI_COLLECTION: self.client.get_or_create_collection(WIKI_COLLECTION),
        }

    def _coll(self, name: str):
        if name not in self.collections:
            raise ValueError(f"Collection sconosciuta: {name}")
        return self.collections[name]

    def add(self, collection: str, ids: list[str], embeddings: list[list[float]], documents: list[str], metadatas: list[dict]) -> None:
        if not ids:
            return
        self._coll(collection).upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)

    def query(self, collection: str, query_embedding: list[float], top_k: int, where: dict | None = None) -> list[dict]:
        res = self._coll(collection).query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
        )
        out = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for i, _id in enumerate(ids):
            out.append({
                "id": _id,
                "text": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "distance": dists[i] if i < len(dists) else None,
            })
        return out

    def delete(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        self._coll(collection).delete(ids=ids)

    def delete_where(self, collection: str, where: dict) -> None:
        self._coll(collection).delete(where=where)

    def count(self, collection: str) -> int:
        return self._coll(collection).count()
