"""
Graph store: indice strutturale delle entità e relazioni del corpus.

Backend: Kuzu (embedded graph DB, no server, sintassi Cypher-compatible).
Persistenza: file su disco in GRAPH_DIR. Zero servizi extra, zero token a
query time (pura traversal strutturale).

Ruolo nell'Arch B:
  Le pagine wiki MD restano come artefatto leggibile per la revisione umana.
  Il grafo sostituisce il ruolo dell'indice ChromaDB wiki_pages come fonte
  di contesto strutturale: le query pescano raw chunks (ChromaDB) + sottografo
  (Kuzu) invece di raw + wiki embeds.

Schema:
  Entity  — nodo per ogni entità dell'EntityIndex (aliased o consolidated).
             wiki_snippet: primi 400 char del body della pagina più rilevante.
  Relation — arco tipato tra entità. rel_type default: CO_MENZIONATO (due
             entità estratte dallo stesso documento raw), ma può essere
             un tipo semantico più ricco se prodotto da estrazione LLM.
"""
from __future__ import annotations
from pathlib import Path
import re

import kuzu

from .config import GRAPH_DIR

_SNIPPET_MAX = 400


def _clean_body(body: str) -> str:
    """Restituisce testo pulito da un body wiki MD per l'uso come snippet.

    Rimuove:
    - blocchi YAML/codice iniziali (```yaml ... ```)
    - heading markdown (#, ##, ###)
    - righe vuote eccessive
    Restituisce i primi _SNIPPET_MAX caratteri del testo risultante.
    """
    # Rimuove blocchi codice (```...```)
    text = re.sub(r"```[a-z]*\n.*?```", "", body, flags=re.DOTALL)
    # Rimuove heading markdown
    text = re.sub(r"^#{1,6}\s+.*$", "", text, flags=re.MULTILINE)
    # Collassa spazi multipli
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text[:_SNIPPET_MAX]


class GraphStore:
    """Wrapper su Kuzu per nodi Entity e archi Relation."""

    def __init__(self, root: Path = GRAPH_DIR):
        # Kuzu 0.11+ rifiuta path che puntano a directory vuote preesistenti.
        # Si usa un sottopercorso dedicato: data/graph/kuzu_db/.
        # root (data/graph/) può essere pre-creata da config.py senza problemi.
        root.mkdir(parents=True, exist_ok=True)
        db_path = root / "kuzu_db"
        self.db = kuzu.Database(str(db_path))
        self.conn = kuzu.Connection(self.db)
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema e lifecycle
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Crea le tabelle se non esistono. Idempotente."""
        try:
            self.conn.execute(
                "CREATE NODE TABLE Entity("
                "id STRING, label STRING, subtype STRING, "
                "domain STRING, state STRING, wiki_snippet STRING, "
                "PRIMARY KEY(id))"
            )
        except Exception:
            pass
        try:
            self.conn.execute(
                "CREATE REL TABLE Relation("
                "FROM Entity TO Entity, "
                "rel_type STRING, confidence STRING, source_doc STRING)"
            )
        except Exception:
            pass

    def reset(self) -> None:
        """Elimina e ricrea le tabelle. Usato da build_graph per rebuild completo."""
        for stmt in [
            "DROP TABLE IF EXISTS Relation",
            "DROP TABLE IF EXISTS Entity",
            "CREATE NODE TABLE Entity("
            "id STRING, label STRING, subtype STRING, "
            "domain STRING, state STRING, wiki_snippet STRING, "
            "PRIMARY KEY(id))",
            "CREATE REL TABLE Relation("
            "FROM Entity TO Entity, "
            "rel_type STRING, confidence STRING, source_doc STRING)",
        ]:
            self.conn.execute(stmt)

    def close(self) -> None:
        """Chiude connessione e rilascia il lock sul DB Kuzu."""
        self.conn.close()
        # Necessario per rilasciare il file lock: Kuzu lo mantiene finché
        # l'oggetto Database è vivo. Del + None forza il GC a rilasciarlo.
        del self.db
        self.db = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Mutazioni
    # ------------------------------------------------------------------

    def upsert_entity(
        self,
        id: str,
        label: str = "",
        subtype: str = "",
        domain: str = "",
        state: str = "",
        wiki_snippet: str = "",
    ) -> None:
        """Inserisce o aggiorna un nodo Entity (upsert manuale)."""
        res = self.conn.execute(
            "MATCH (e:Entity) WHERE e.id = $id RETURN e.id",
            {"id": id},
        )
        if res.has_next():
            self.conn.execute(
                "MATCH (e:Entity) WHERE e.id = $id "
                "SET e.label = $label, e.subtype = $subtype, "
                "e.domain = $domain, e.state = $state, e.wiki_snippet = $snip",
                {"id": id, "label": label, "subtype": subtype,
                 "domain": domain, "state": state, "snip": wiki_snippet},
            )
        else:
            self.conn.execute(
                "CREATE (:Entity {id: $id, label: $label, subtype: $subtype, "
                "domain: $domain, state: $state, wiki_snippet: $snip})",
                {"id": id, "label": label, "subtype": subtype,
                 "domain": domain, "state": state, "snip": wiki_snippet},
            )

    def add_relation(
        self,
        source_id: str,
        target_id: str,
        rel_type: str = "CO_MENZIONATO",
        confidence: str = "EXTRACTED",
        source_doc: str = "",
    ) -> None:
        """Crea un arco Relation. Salta silenziosamente se un nodo manca."""
        try:
            self.conn.execute(
                "MATCH (a:Entity), (b:Entity) "
                "WHERE a.id = $src AND b.id = $tgt "
                "CREATE (a)-[:Relation {rel_type: $rt, confidence: $conf, "
                "source_doc: $sdoc}]->(b)",
                {"src": source_id, "tgt": target_id, "rt": rel_type,
                 "conf": confidence, "sdoc": source_doc},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_subgraph(self, entity_ids: list[str], hops: int = 2) -> dict:
        """Restituisce il sottografo entro `hops` passi dagli entity_ids forniti.

        Strategia:
          1. Espande ogni entity_id via traversal multi-hop.
          2. Carica metadata di tutti i nodi trovati.
          3. Carica tutti gli archi tra i nodi del sottografo.

        Returns:
            {
              "focus": [entity_ids],
              "nodes": [{"id", "label", "subtype", "domain", "state", "snippet"}, ...],
              "relations": [{"from", "to", "type", "confidence", "source_doc"}, ...],
            }
        """
        all_ids: set[str] = set(entity_ids)

        for eid in entity_ids:
            try:
                res = self.conn.execute(
                    f"MATCH (s:Entity)-[:Relation*1..{hops}]-(n:Entity) "
                    "WHERE s.id = $id RETURN DISTINCT n.id",
                    {"id": eid},
                )
                while res.has_next():
                    all_ids.add(res.get_next()[0])
            except Exception:
                pass

        if not all_ids:
            return {"focus": entity_ids, "nodes": [], "relations": []}

        # Carica metadata nodi
        nodes = []
        for nid in sorted(all_ids):
            res = self.conn.execute(
                "MATCH (e:Entity) WHERE e.id = $id "
                "RETURN e.id, e.label, e.subtype, e.domain, e.state, e.wiki_snippet",
                {"id": nid},
            )
            if res.has_next():
                row = res.get_next()
                nodes.append({
                    "id": row[0], "label": row[1], "subtype": row[2],
                    "domain": row[3], "state": row[4], "snippet": row[5],
                })

        # Carica archi tra nodi del sottografo
        relations: list[dict] = []
        seen_rels: set[tuple] = set()
        ids_list = list(all_ids)
        for nid in ids_list:
            try:
                res = self.conn.execute(
                    "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
                    "WHERE a.id = $id AND b.id IN $others "
                    "RETURN a.id, b.id, r.rel_type, r.confidence, r.source_doc",
                    {"id": nid, "others": ids_list},
                )
                while res.has_next():
                    row = res.get_next()
                    key = (row[0], row[1], row[2])
                    if key not in seen_rels:
                        seen_rels.add(key)
                        relations.append({
                            "from": row[0], "to": row[1],
                            "type": row[2], "confidence": row[3],
                            "source_doc": row[4],
                        })
            except Exception:
                pass

        return {"focus": entity_ids, "nodes": nodes, "relations": relations}

    def entity_exists(self, entity_id: str) -> bool:
        res = self.conn.execute(
            "MATCH (e:Entity) WHERE e.id = $id RETURN e.id",
            {"id": entity_id},
        )
        return res.has_next()

    def count_entities(self) -> int:
        res = self.conn.execute("MATCH (e:Entity) RETURN count(*)")
        return res.get_next()[0] if res.has_next() else 0

    def count_relations(self) -> int:
        res = self.conn.execute("MATCH ()-[r:Relation]->() RETURN count(*)")
        return res.get_next()[0] if res.has_next() else 0

    def all_entity_ids(self) -> list[str]:
        res = self.conn.execute("MATCH (e:Entity) RETURN e.id")
        ids = []
        while res.has_next():
            ids.append(res.get_next()[0])
        return ids
