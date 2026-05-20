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
from datetime import datetime
from pathlib import Path
import yaml
import chromadb
from .config import (
    RAW_DIR, WIKI_DIR, VECTORS_DIR, HOT_LAYER_PATH, ENTITY_INDEX_PATH,
    RAW_COLLECTION, WIKI_COLLECTION, ENTITY_CONSOLIDATION_THRESHOLD,
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


# Stati possibili per un'entità nell'indice (§13).
ENTITY_STATE_ALIASED = "aliased"           # n_sources < threshold, no file md, no vettore
ENTITY_STATE_CONSOLIDATED = "consolidated" # entity page materializzata + indicizzata
ENTITY_STATE_STABLE = "stable"             # consolidata + merge automatico congelato (futuro)


class EntityIndex:
    """Indice centrale delle entità del corpus (§13).

    Single source of truth per:
    - "esiste un'entità con questo id?" (anti-frammentazione, dedup)
    - "quali source contribuiscono a questa entità?"
    - "questa entità è materializzata come pagina md o solo aliased?"

    File su disco: `data/wiki/_entity_index.yaml`. Caricamento on-demand,
    save esplicito dopo ogni mutazione (no transazioni multi-step; le
    mutazioni dell'ingest sono già serializzate).

    Per ogni entry:
        id              : entity_id (snake_case inglese, stabile)
        subtype         : character|place|artifact|event|book|"" (concetto)
        domain          : dominio della maggioranza delle source; _mixed se cross
        sources         : lista doc_id che la citano (ordine = inserimento)
        n_sources       : len(sources), denormalizzato per lookup veloce
        state           : aliased | consolidated | stable
        consolidated_at : ISO timestamp del consolidamento, o None
        last_updated    : ISO date dell'ultimo update
    """

    def __init__(self, path: Path = ENTITY_INDEX_PATH,
                 threshold: int = ENTITY_CONSOLIDATION_THRESHOLD):
        self.path = path
        self.threshold = threshold
        self._data: dict | None = None  # lazy load

    # ------------------------------------------------------------------
    # I/O su disco
    # ------------------------------------------------------------------
    def _load(self) -> dict:
        """Carica l'indice se non già in memoria. Inizializza vuoto se assente."""
        if self._data is not None:
            return self._data
        if not self.path.exists():
            self._data = {"version": 1, "threshold": self.threshold, "entities": []}
            return self._data
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if "entities" not in raw or raw["entities"] is None:
            raw["entities"] = []
        raw.setdefault("version", 1)
        raw.setdefault("threshold", self.threshold)
        self._data = raw
        return self._data

    def save(self) -> None:
        """Persiste l'indice su disco. No-op se non c'è stato un load (niente da scrivere)."""
        if self._data is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump(self._data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def get(self, entity_id: str) -> dict | None:
        """Ritorna l'entry per un entity_id, o None se assente.

        L'entry restituita è il dict vivo nell'indice: modifiche su di esso
        si riflettono nella struttura interna. Per safety, il chiamante
        dovrebbe trattarla come read-only e usare update() per modifiche.
        """
        data = self._load()
        for e in data["entities"]:
            if e["id"] == entity_id:
                return e
        return None

    def exists(self, entity_id: str) -> bool:
        return self.get(entity_id) is not None

    def list_all(self) -> list[dict]:
        """Tutte le entry dell'indice (ordine di inserimento)."""
        return list(self._load()["entities"])

    def list_by_state(self, state: str) -> list[dict]:
        """Entry filtrate per stato (aliased | consolidated | stable)."""
        return [e for e in self._load()["entities"] if e.get("state") == state]

    def consolidated_ids(self) -> set[str]:
        """Insieme degli entity_id consolidated (per filtri Hot Layer/query)."""
        return {e["id"] for e in self._load()["entities"]
                if e.get("state") == ENTITY_STATE_CONSOLIDATED}

    def aliased_ids(self) -> set[str]:
        """Insieme degli entity_id aliased (per whitelist citazioni in query)."""
        return {e["id"] for e in self._load()["entities"]
                if e.get("state") == ENTITY_STATE_ALIASED}

    # ------------------------------------------------------------------
    # Mutazioni
    # ------------------------------------------------------------------
    def upsert_contribution(self, entity_id: str, doc_id: str,
                            subtype: str = "", domain: str = "") -> dict:
        """Registra un contributo di `doc_id` per `entity_id`.

        Se l'entità non esiste, viene creata in stato `aliased` con
        sources=[doc_id]. Se esiste, `doc_id` viene appeso alla lista
        (dedup) e i metadati aggiornati. NON cambia lo stato: la
        decisione di consolidare resta al chiamante (vedi
        should_consolidate()).

        Returns:
            L'entry aggiornata (dict vivo).
        """
        data = self._load()
        entry = self.get(entity_id)
        now_iso = datetime.now().isoformat(timespec="seconds")
        today = datetime.now().date().isoformat()

        if entry is None:
            entry = {
                "id": entity_id,
                "subtype": subtype or "",
                "domain": domain or "",
                "sources": [doc_id],
                "n_sources": 1,
                "state": ENTITY_STATE_ALIASED,
                "consolidated_at": None,
                "last_updated": today,
            }
            data["entities"].append(entry)
            return entry

        # Append doc_id se non già presente.
        if doc_id not in entry["sources"]:
            entry["sources"].append(doc_id)
        entry["n_sources"] = len(entry["sources"])
        entry["last_updated"] = today
        # Aggiorna subtype se ancora vuoto e ora arriva un valore.
        if subtype and not entry.get("subtype"):
            entry["subtype"] = subtype
        # Domain: se sources di domini diversi, marca _mixed.
        if domain:
            current = entry.get("domain") or domain
            entry["domain"] = current if current == domain else "_mixed"
        return entry

    def should_consolidate(self, entity_id: str) -> bool:
        """True se l'entità è `aliased` e ha raggiunto la soglia."""
        entry = self.get(entity_id)
        if entry is None:
            return False
        return (entry.get("state") == ENTITY_STATE_ALIASED
                and entry.get("n_sources", 0) >= self.threshold)

    def mark_consolidated(self, entity_id: str) -> None:
        """Promuove un'entità aliased a consolidated (timestamp registrato)."""
        entry = self.get(entity_id)
        if entry is None:
            raise KeyError(f"Entity '{entity_id}' non trovata nell'indice.")
        entry["state"] = ENTITY_STATE_CONSOLIDATED
        entry["consolidated_at"] = datetime.now().isoformat(timespec="seconds")
        entry["last_updated"] = datetime.now().date().isoformat()

    def remove(self, entity_id: str) -> bool:
        """Rimuove l'entry. Usato dal wipe e dal lint consolidation merge.

        Returns:
            True se l'entry esisteva ed è stata rimossa.
        """
        data = self._load()
        before = len(data["entities"])
        data["entities"] = [e for e in data["entities"] if e["id"] != entity_id]
        return len(data["entities"]) < before

    def remove_source_contribution(self, doc_id: str) -> list[str]:
        """Rimuove `doc_id` dalle sources di TUTTE le entità.

        Usato durante il rollback di un doc: l'entità che aveva quel doc
        come unica source diventa orfana e va rimossa; altrimenti
        decrementa n_sources e basta. NON declassifica da consolidated
        ad aliased automaticamente (richiede rebuild dedicato).

        Returns:
            Lista degli entity_id rimossi (perché senza più sources).
        """
        data = self._load()
        orphaned: list[str] = []
        for e in list(data["entities"]):
            if doc_id in e["sources"]:
                e["sources"] = [s for s in e["sources"] if s != doc_id]
                e["n_sources"] = len(e["sources"])
                if not e["sources"]:
                    data["entities"].remove(e)
                    orphaned.append(e["id"])
        return orphaned
