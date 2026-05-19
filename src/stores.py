"""
Persistenza del sistema: il "doppio indice" del design realizzato come
filesystem + ChromaDB.

Tre classi indipendenti, ognuna con una responsabilità precisa:

- RawStore  : documenti raw immutabili (.md con frontmatter YAML).
              Strato 1 dell'architettura, fedeltà totale.
- WikiStore : pagine wiki sintetizzate (.md con frontmatter YAML).
              Strato 3 dell'architettura, sintesi e relazioni.
- VectorDB  : ChromaDB persistente con due collection separate
              (raw_chunks, wiki_pages). Strato 2 dell'architettura.

I tre store sono completamente disaccoppiati: l'ingest pipeline li orchestra
in sequenza ma ognuno è autonomo e testabile in isolamento.
"""

from __future__ import annotations
from pathlib import Path
import yaml
import chromadb
from .config import (
    RAW_DIR, WIKI_DIR, VECTORS_DIR, HOT_LAYER_PATH,
    RAW_COLLECTION, WIKI_COLLECTION,
)


def _write_md(path: Path, frontmatter: dict, body: str) -> None:
    """Serializza un file markdown con frontmatter YAML.

    Formato compatibile con tutti i parser markdown standard (Jekyll, Hugo,
    Obsidian) e leggibile a occhio nudo nel terminale.
    """
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{fm}\n---\n\n{body.strip()}\n", encoding="utf-8")


def _read_md(path: Path) -> tuple[dict, str]:
    """Parsa un file markdown con frontmatter YAML.

    Returns:
        (frontmatter_dict, body_text). Frontmatter vuoto se il file non
        inizia con '---'.
    """
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        # split(maxsplit=2) divide in: pre-frontmatter (vuoto), frontmatter, body.
        _, fm_block, body = text.split("---", 2)
        return yaml.safe_load(fm_block) or {}, body.strip()
    return {}, text.strip()


class RawStore:
    """Store dei documenti raw, immutabili per design.

    Una volta scritto un documento, non viene MAI modificato. Eventuali
    aggiornamenti producono un nuovo doc_id. Questa proprietà è il
    fondamento del "principio non negoziabile" del design: il raw layer
    garantisce fedeltà totale alle sorgenti originali.
    """

    def __init__(self, root: Path = RAW_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, doc_id: str) -> Path:
        """Risolve il path su disco per un doc_id."""
        return self.root / f"{doc_id}.md"

    def exists(self, doc_id: str) -> bool:
        """Verifica se un documento è già presente nello store."""
        return self._path(doc_id).exists()

    def save(self, doc_id: str, content: str, metadata: dict) -> Path:
        """Salva un nuovo documento. Sovrascrive se esiste già (caso atipico:
        un doc_id collide solo se due ingest avvengono nello stesso secondo
        con lo stesso titolo).

        Returns:
            Path del file scritto.
        """
        path = self._path(doc_id)
        _write_md(path, {"id": doc_id, **metadata}, content)
        return path

    def get(self, doc_id: str) -> tuple[dict, str]:
        """Carica (frontmatter, body) di un documento."""
        return _read_md(self._path(doc_id))

    def list(self) -> list[str]:
        """Elenca tutti i doc_id presenti, ordinati alfabeticamente."""
        return sorted(p.stem for p in self.root.glob("*.md"))


class WikiStore:
    """Store delle pagine wiki sintetizzate.

    Le pagine sono di due tipi (vedi schema/AGENTS.md):
    - type=entity: pagine dedicate a character/place/artifact/event/book.
                   Mergeable: nuovi documenti raffinano pagine esistenti.
    - type=source: sintesi 1:1 di un singolo documento raw. Append-only.

    HOT_LAYER.md vive nella stessa directory ma è gestito da hot_layer.py e
    deve essere escluso dalle operazioni di listing (vedi RESERVED).
    """

    # Filename (senza estensione) da escludere dal listing perché non sono
    # pagine wiki "vere" ma artefatti di sistema.
    RESERVED = {"HOT_LAYER"}

    def __init__(self, root: Path = WIKI_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, page_id: str) -> Path:
        return self.root / f"{page_id}.md"

    def exists(self, page_id: str) -> bool:
        return self._path(page_id).exists()

    def save(self, page_id: str, content: str, metadata: dict) -> Path:
        """Scrive (o sovrascrive) una pagina wiki con il suo frontmatter."""
        path = self._path(page_id)
        _write_md(path, {"id": page_id, **metadata}, content)
        return path

    def get(self, page_id: str) -> tuple[dict, str]:
        return _read_md(self._path(page_id))

    def list(self) -> list[str]:
        """Elenca le pagine wiki, escludendo i file RESERVED (es. HOT_LAYER)."""
        out = []
        for p in self.root.glob("*.md"):
            if p.stem in self.RESERVED:
                continue
            out.append(p.stem)
        return sorted(out)

    def delete_page(self, page_id: str) -> bool:
        """Elimina il file di una pagina wiki. Usato dalla consolidazione
        lint (§6.3) per rimuovere una pagina alias dopo il merge nel
        canonical. Rifiuta esplicitamente i file RESERVED.

        Returns:
            True se il file esisteva ed è stato rimosso.
        """
        if page_id in self.RESERVED:
            raise ValueError(f"Rifiuto di eliminare una pagina riservata: {page_id}")
        path = self._path(page_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def rewrite_links(self, old_id: str, new_id: str) -> list[str]:
        """Riscrive i wikilink [[old_id]] -> [[new_id]] in TUTTE le pagine.

        Necessario dopo un merge di consolidazione: gli inbound link verso
        la pagina alias eliminata vanno reindirizzati al canonical, altrimenti
        restano riferimenti rotti.

        Returns:
            Lista dei page_id modificati (per audit/report).
        """
        if old_id == new_id:
            return []
        old_tok = f"[[{old_id}]]"
        new_tok = f"[[{new_id}]]"
        changed = []
        for pid in self.list():
            fm, body = self.get(pid)
            if old_tok in body:
                self.save(pid, body.replace(old_tok, new_tok),
                          {k: v for k, v in fm.items() if k != "id"})
                changed.append(pid)
        return changed

    def update_with_merge(
        self,
        page_id: str,
        new_content: str,
        new_sources: list[str],
        extra_meta: dict | None = None,
    ) -> Path:
        """Aggiorna una pagina entity preservando l'unione delle sorgenti.

        Usato dall'ingest L2: l'LLM produce il body fuso (vecchio + nuovo
        documento) e questo metodo si occupa di:
        - estendere la lista sources con il nuovo doc_id (deduplicando,
          mantenendo l'ordine di inserimento via dict.fromkeys);
        - applicare i metadati aggiornati (last_updated, stale, ecc.);
        - salvare il file.

        Se la pagina non esiste ancora, viene creata da zero.
        """
        fm, _ = self.get(page_id) if self.exists(page_id) else ({}, "")
        # Unione ordinata e deduplicata. dict.fromkeys è una tecnica idiomatica
        # in Python 3.7+ che sfrutta il fatto che i dict mantengono l'ordine.
        sources = list(dict.fromkeys((fm.get("sources") or []) + new_sources))
        fm["sources"] = sources
        if extra_meta:
            fm.update(extra_meta)
        # Rimuove "id" per non duplicarlo: save() lo reinserisce dal page_id.
        return self.save(page_id, new_content, {k: v for k, v in fm.items() if k != "id"})


class VectorDB:
    """Wrapper su ChromaDB persistente per le due collection del doppio indice.

    Non espone direttamente le collection: tutti i metodi richiedono il
    nome della collection come parametro. Questo evita di proliferare
    metodi specializzati (`add_raw`, `add_wiki`, ecc.) e mantiene la API
    omogenea per future estensioni (es. una terza collection per le
    synthesis pages in fase di Scaling).
    """

    def __init__(self, root: Path = VECTORS_DIR):
        # PersistentClient: salva su disco in `root` e ricarica al riavvio.
        # Per il pilot è sufficiente; per la produzione si valuterà un server
        # ChromaDB esterno o un altro vector DB (Qdrant, Weaviate...).
        self.client = chromadb.PersistentClient(path=str(root))
        # Le collection vengono create on-demand al primo upsert. Si usa
        # get_or_create per essere idempotenti rispetto a esecuzioni multiple.
        self.collections = {
            RAW_COLLECTION: self.client.get_or_create_collection(RAW_COLLECTION),
            WIKI_COLLECTION: self.client.get_or_create_collection(WIKI_COLLECTION),
        }

    def _coll(self, name: str):
        """Risolve il nome della collection, fallendo presto se errato."""
        if name not in self.collections:
            raise ValueError(f"Collection sconosciuta: {name}")
        return self.collections[name]

    def add(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        """Aggiunge o aggiorna vettori (upsert su id).

        L'upsert (non insert) è intenzionale: rende l'ingest idempotente
        rispetto a re-elaborazioni della stessa pagina wiki (caso comune
        nella pipeline L2 quando una entità viene aggiornata da un nuovo
        documento).
        """
        if not ids:
            return
        self._coll(collection).upsert(
            ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas
        )

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int,
        where: dict | None = None,
    ) -> list[dict]:
        """Esegue una ricerca per similarità.

        Args:
            collection: nome della collection (raw_chunks o wiki_pages).
            query_embedding: vettore della domanda dell'utente.
            top_k: numero di risultati richiesti.
            where: filtro opzionale sui metadati (es. {"doc_id": "..."}).
                Utile in futuro per filtri di access control.

        Returns:
            Lista di dict normalizzati: {id, text, metadata, distance}.
            ChromaDB restituisce le strutture annidate (liste di liste);
            qui si denormalizza per comodità dei consumer.
        """
        res = self._coll(collection).query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
        )
        out = []
        # ChromaDB indicizza per batch di query: [0] estrae l'unica batch
        # corrispondente alla nostra singola query_embedding.
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
        """Cancella vettori per id. Usato prima del re-embed di una pagina
        wiki aggiornata, per evitare duplicati con metadati stantii."""
        if not ids:
            return
        self._coll(collection).delete(ids=ids)

    def delete_where(self, collection: str, where: dict) -> None:
        """Cancella vettori per filtro metadati. Riservato a operazioni
        amministrative (es. wipe di tutti i chunk di un doc_id)."""
        self._coll(collection).delete(where=where)

    def count(self, collection: str) -> int:
        """Numero di vettori nella collection. Usato dal lint per diagnostica."""
        return self._coll(collection).count()

    def get_embedding(self, collection: str, id_: str) -> list[float] | None:
        """Recupera il vettore già memorizzato per un id.

        Usato dall'inventario gerarchico (§11.1): permette di riutilizzare
        l'embedding della source page appena creata come query vector per
        la shortlist semantica, evitando una chiamata embedding aggiuntiva.

        Returns:
            Il vettore, o None se l'id non esiste o non ha embedding.
        """
        res = self._coll(collection).get(ids=[id_], include=["embeddings"])
        embs = res.get("embeddings")
        if embs is not None and len(embs) and embs[0] is not None:
            return list(embs[0])
        return None
