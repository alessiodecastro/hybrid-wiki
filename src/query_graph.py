"""
Pipeline di query Arch B: RAW ChromaDB + Graph Kuzu (no wiki embeddings).

Differenze rispetto a QueryPipeline (query.py):
  - NON interroga la collection wiki_pages di ChromaDB.
  - Rileva entità rilevanti dai doc_id recuperati + testo della domanda.
  - Chiama GraphStore.get_subgraph() per ottenere il sottografo strutturale.
  - Inietta il sottografo come JSON compatto nel prompt invece dei wiki hits.
  - Le pagine wiki MD restano su disco per la revisione umana ma non
    vengono embeddate né recuperate a query time.

Tutto il resto (Hot Layer, conflict rules, whitelist citazioni, multi-corpus
policy, token tracking, audit log) è identico a QueryPipeline.
"""
from __future__ import annotations
import json
import re
from datetime import datetime

from .config import (
    AGENTS_MD_PATH, QUERY_LOG_PATH, TOKEN_LOG_PATH,
    RAW_COLLECTION, RAW_TOP_K, MIXED_DOMAIN,
)
from .stores import VectorDB, EntityIndex, WikiStore
from .graph_store import GraphStore
from .embeddings import Embedder
from .llm_client import LLMClient
from .hot_layer import HotLayer
from .token_tracker import TokenTracker
from .query import CONFLICT_RULES, _load_agents, _parse_response


# Limite massimo di nodi nel subgrafo iniettato nel prompt.
# Sopra questo limite si troncano i nodi meno connessi (i primi nella lista
# ordinata per id sono quelli più "centrali" nel subgrafo espanso).
_MAX_SUBGRAPH_NODES = 12
_SUBGRAPH_HOPS = 1        # hops=1: solo vicini diretti, evita espansione eccessiva
_SNIPPET_IN_PROMPT = 100  # char snippet per nodo nel prompt (vs 300 precedente)


def _entity_label(entity_id: str) -> str:
    return entity_id.replace("_", " ").title()


class QueryPipelineGraph:
    """Pipeline end-to-end Arch B: raw retrieval + graph traversal."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        embedder: Embedder | None = None,
        tracker: TokenTracker | None = None,
    ):
        self.tracker = tracker or TokenTracker(log_path=TOKEN_LOG_PATH)
        self.vdb = VectorDB()
        self.llm = llm or LLMClient(tracker=self.tracker)
        self.embedder = embedder or Embedder(tracker=self.tracker)
        self.entity_index = EntityIndex()
        # WikiStore necessario per il Hot Layer (non per l'embedding wiki)
        self.hot = HotLayer(WikiStore(), self.llm, entity_index=self.entity_index)
        self.graph = GraphStore()
        self.agents_md = _load_agents()

    def _corpus_domains(self) -> set[str]:
        """Domini presenti nel corpus (scan frontmatter wiki, filesystem only)."""
        wiki = WikiStore()
        domains: set[str] = set()
        for pid in wiki.list():
            fm, _ = wiki.get(pid)
            d = fm.get("domain")
            if d and d != MIXED_DOMAIN:
                domains.add(d)
        return domains

    def _entities_from_hits(self, raw_hits: list[dict]) -> list[str]:
        """Entità nel grafo associate ai doc_id dei raw hits.

        Per ogni doc_id recuperato cerca nell'EntityIndex quali entità
        hanno quel doc nelle loro sources → nessun costo LLM aggiuntivo.
        """
        retrieved_doc_ids = {
            (h.get("metadata") or {}).get("doc_id") or h["id"]
            for h in raw_hits
        }
        return [
            e["id"]
            for e in self.entity_index.list_all()
            if any(s in retrieved_doc_ids for s in e.get("sources", []))
        ]

    def _entities_from_question(self, question: str) -> list[str]:
        """Match approssimativo: entity_id menzionati nel testo della domanda.

        Converte entity_id in forma leggibile (snake_case → parole) e cerca
        corrispondenze nel testo normalizzato. Supplementa _entities_from_hits
        per domande esplicite su entità non presenti nei raw hits.
        """
        q_lower = question.lower()
        matches = []
        for e in self.entity_index.list_all():
            eid_readable = e["id"].replace("_", " ").lower()
            if eid_readable in q_lower and e["id"] not in matches:
                matches.append(e["id"])
        return matches

    def _format_subgraph(self, subgraph: dict) -> str:
        """Serializza il subgrafo in formato compatto per il prompt.

        Formato: blocco JSON leggibile con nodi (id, label, subtype, snippet)
        e relazioni (from → to, tipo). Lo snippet per nodo è limitato a
        evitare di saturare il contesto.
        """
        nodes = subgraph.get("nodes", [])[:_MAX_SUBGRAPH_NODES]
        relations = subgraph.get("relations", [])
        # Filtra relazioni ai soli nodi inclusi
        included_ids = {n["id"] for n in nodes}
        relations = [
            r for r in relations
            if r["from"] in included_ids and r["to"] in included_ids
        ]
        # Dedup relazioni bidirezionali CO_MENZIONATO: basta una direzione
        seen: set[frozenset] = set()
        deduped_rels = []
        for r in relations:
            key = frozenset([r["from"], r["to"]])
            if key not in seen:
                seen.add(key)
                deduped_rels.append(r)

        payload = {
            "focus": subgraph.get("focus", []),
            "nodes": [
                {
                    "id": n["id"],
                    "label": n.get("label") or _entity_label(n["id"]),
                    "subtype": n.get("subtype", ""),
                    "domain": n.get("domain", ""),
                    "snippet": (n.get("snippet") or "")[:_SNIPPET_IN_PROMPT],
                }
                for n in nodes
            ],
            "relations": [
                {
                    "from": r["from"],
                    "to": r["to"],
                    "type": r["type"],
                    "source_doc": r.get("source_doc", ""),
                }
                for r in deduped_rels
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def ask(self, question: str, domain: str | None = None) -> dict:
        """Esegue la pipeline Arch B per una singola domanda.

        Returns:
            Stesso schema di QueryPipeline.ask(): dict con answer, sources,
            confidence, gaps, question, timestamp.
            Campo aggiuntivo: "arch" = "graph" per distinguere nei log.
        """
        # Embedding della domanda (uguale ad Arch A)
        with self.tracker.phase("query:embedding"):
            q_emb = self.embedder.embed(question)

        # Filtro dominio per raw chunks
        raw_where: dict | None = {"domain": domain} if domain else None

        # Retrieval raw (uguale ad Arch A, WIKI_COLLECTION non toccata)
        raw_hits = self.vdb.query(RAW_COLLECTION, q_emb, RAW_TOP_K, where=raw_where)

        # Hot Layer (stesso orientamento di Arch A)
        hot_layer = self.hot.load()

        # --- Differenza chiave vs Arch A ---
        # Rileva entità dai doc recuperati + dalla domanda, poi traversal grafo
        entity_ids = list(dict.fromkeys(
            self._entities_from_hits(raw_hits) +
            self._entities_from_question(question)
        ))

        subgraph = self.graph.get_subgraph(entity_ids, hops=_SUBGRAPH_HOPS) if entity_ids else {
            "focus": [], "nodes": [], "relations": []
        }

        graph_block = self._format_subgraph(subgraph)
        raw_block = self._format_raw_hits(raw_hits)

        # Whitelist citazioni: entità dal grafo + doc_id raw recuperati
        allowed_entities = sorted({n["id"] for n in subgraph.get("nodes", [])})
        allowed_raw = sorted({
            (h.get("metadata") or {}).get("doc_id") or h["id"]
            for h in raw_hits
            if (h.get("metadata") or {}).get("doc_id") or h["id"]
        })

        # Policy multi-corpus (identica ad Arch A)
        domain_policy = ""
        if not domain:
            corpus_domains = self._corpus_domains()
            if len(corpus_domains) > 1:
                retrieved_domains = sorted({
                    (h.get("metadata") or {}).get("domain")
                    for h in raw_hits
                    if (h.get("metadata") or {}).get("domain")
                })
                others = sorted(corpus_domains - set(retrieved_domains))
                domain_policy = (
                    "POLICY MULTI-CORPUS (nessun filtro dominio applicato):\n"
                    f"- Il corpus contiene più corpora distinti: {sorted(corpus_domains)}.\n"
                    f"- Il contesto retrieved copre i domini: {retrieved_domains or '(nessuno)'}.\n"
                    "- Struttura la risposta per dominio se copre più corpora; "
                    "cap confidence a 'medium'.\n\n"
                )

        system = (
            "Sei l'assistente di un companion wiki di lettura multi-corpus. "
            "Rispondi a domande dell'utente usando il Hot Layer per orientarti, "
            "il sottografo strutturale delle entità e i frammenti raw.\n\n"
            "STRATEGIA:\n"
            "1. Orientati nel Hot Layer.\n"
            "2. Usa il SOTTOGRAFO per capire chi sono le entità rilevanti e come "
            "   sono connesse (relazioni, dominio, snippet descrittivo).\n"
            "3. Usa i frammenti RAW per dettagli precisi, citazioni testuali, numeri/date.\n"
            "4. Applica le regole di risoluzione conflitti.\n"
            "5. Cita SEMPRE le sorgenti effettivamente usate.\n"
            "6. Se la risposta non è derivabile, dillo esplicitamente.\n\n"
            + domain_policy +
            "SCAN OBBLIGATORIO DEI CONFLITTI:\n"
            "Prima di formulare la risposta, scorri tutti i frammenti RAW e verifica "
            "eventuali discrepanze su numeri, date o fatti rispetto al sottografo. "
            "Se trovi divergenze, esplicita entrambe le versioni.\n\n"
            "REGOLE DI CITAZIONE (VINCOLANTI — il rispetto è obbligatorio):\n"
            "- OGNI volta che nomini un'entità presente nel sottografo, DEVI "
            "  marcarla con `[[entity_id]]` alla prima menzione nel paragrafo. "
            "  Non è opzionale: una risposta che descrive entità del grafo SENZA "
            "  alcun `[[entity_id]]` viola il contratto di output.\n"
            "- Le entità di FOCUS del sottografo (campo \"focus\" del JSON) sono "
            "  il cuore della domanda: se le usi nella risposta, le loro citazioni "
            "  `[[entity_id]]` DEVONO comparire nel corpo e nel campo wiki_sources.\n"
            "- Usa `[[doc_id_raw]]` (quelli con suffisso _AAAAMMGGHHMMSS) per dettagli "
            "  puntuali, date, citazioni testuali tratte dai frammenti RAW.\n"
            "- Usa l'entity_id ESATTO della whitelist (snake_case), non il label "
            "  leggibile: scrivi `[[lord_voldemort]]`, non `[[Lord Voldemort]]`.\n"
            "- Puoi citare con `[[id]]` SOLO id presenti nelle whitelist sottostanti. "
            "  Per entità non in whitelist usa testo piano SENZA wikilink.\n"
            "- Prima di emettere il blocco JSON finale, rileggi la tua risposta: "
            "  ogni entità del grafo che hai citato deve essere elencata in "
            "  wiki_sources, ogni doc_id raw in raw_sources. Liste vuote sono "
            "  ammesse solo se davvero non hai usato nessuna sorgente.\n"
            "ENTITÀ ammesse (dal grafo): "
            + (", ".join(allowed_entities) if allowed_entities else "(nessuna)") + "\n"
            "RAW doc_id ammessi: "
            + (", ".join(allowed_raw) if allowed_raw else "(nessuno)") + "\n\n"
            "COMPATTEZZA: risposta entro ~600 parole incluso blocco JSON finale.\n\n"
            f"{CONFLICT_RULES}\n\n"
            "FORMATO DI OUTPUT: dopo la risposta in markdown, includi UN UNICO blocco JSON:\n"
            '{"answer": "<risposta>", '
            '"wiki_sources": ["entity_id1", ...], '
            '"raw_sources": ["doc_id1", ...], '
            '"confidence": "high|medium|low", '
            '"gaps": "<lacune>"}\n\n'
            f"AGENTS.md:\n{self.agents_md}\n\n"
            f"=== HOT LAYER ===\n{hot_layer}"
        )

        user = (
            f"## Domanda\n{question}\n\n"
            f"## Sottografo strutturale entità (Arch B)\n```json\n{graph_block}\n```\n\n"
            f"## Risultati retrieval — RAW (top {len(raw_hits)})\n{raw_block}\n"
        )

        with self.tracker.phase("query:llm_synthesis"):
            full = self.llm.complete(system=system, user=user, max_tokens=3500)

        parsed = _parse_response(full)
        parsed["question"] = question
        parsed["timestamp"] = datetime.now().isoformat(timespec="seconds")
        parsed["arch"] = "graph"
        parsed["graph_entities"] = len(subgraph.get("nodes", []))
        parsed["graph_relations"] = len(subgraph.get("relations", []))
        self._log(parsed)
        return parsed

    def _format_raw_hits(self, hits: list[dict]) -> str:
        """Serializza i raw hit (identico a QueryPipeline._format_hits per kind=RAW)."""
        if not hits:
            return "(nessun risultato)"
        out = []
        for h in hits:
            meta = h.get("metadata") or {}
            label = meta.get("doc_id") or h["id"]
            extra = f" (chunk {meta.get('chunk_idx', '?')}, titolo: {meta.get('title', '')})"
            text = h["text"]
            if len(text) > 1200:
                text = text[:1200] + "…"
            out.append(f"### [RAW] {label}{extra}\n{text}")
        return "\n\n".join(out)

    def close(self) -> None:
        """Rilascia il file lock Kuzu. Da chiamare al termine dell'uso."""
        self.graph.close()

    def _log(self, entry: dict) -> None:
        QUERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with QUERY_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
