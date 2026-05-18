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

from .config import (
    AGENTS_MD_PATH, CHUNK_SIZE_WORDS, CHUNK_OVERLAP_WORDS,
    RAW_COLLECTION, WIKI_COLLECTION, VALID_LEVELS, TOKEN_LOG_PATH,
    DEFAULT_DOMAIN, MIXED_DOMAIN,
)
from .stores import RawStore, WikiStore, VectorDB
from .embeddings import Embedder
from .llm_client import LLMClient
from .hot_layer import HotLayer
from .token_tracker import TokenTracker


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

        Returns:
            Dict con doc_id assegnato, livello, dominio, e pagine wiki toccate.

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
        if touched_pages:
            with self.tracker.phase("ingest:hot_layer_rebuild"):
                self.hot.rebuild()

        return {"doc_id": doc_id, "level": level, "domain": domain, "wiki_pages": touched_pages}

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

    def _identify_entities(self, doc_id: str, title: str, body: str, hint_subtype: str | None) -> list[dict]:
        """Chiede all'LLM di estrarre le entità rilevanti dal documento.

        Output forzato a JSON per parsing affidabile. Il prompt è esplicito
        sul fatto di filtrare le menzioni di passaggio: vogliamo le entità
        "trattate", non semplicemente citate. Questo è un trade-off:
        riduce il rumore ma può perdere dettagli rilevanti per il merge
        (vedi caso della contraddizione 1601 vs 1604 sulla fondazione
        della Contea nei documenti seed).
        """
        hint = (
            f"\nIl curatore suggerisce che l'entità principale è di tipo: {hint_subtype}."
            if hint_subtype else ""
        )
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
            sub = ent.get("subtype")
            if not eid or not sub or eid in seen:
                continue
            seen.add(eid)
            clean.append({"id": eid, "subtype": sub, "summary": ent.get("summary", "")})
        return clean

    def _integrate_entities(self, doc_id: str, title: str, body: str, hint_subtype: str | None, domain: str) -> list[str]:
        """Per ogni entità identificata: crea o aggiorna la pagina entity.

        Returns:
            Lista dei page_id toccati (creati o aggiornati).
        """
        with self.tracker.phase("ingest:l2:identify_entities"):
            entities = self._identify_entities(doc_id, title, body, hint_subtype)
        touched: list[str] = []
        for ent in entities:
            page_id = ent["id"]
            subtype = ent["subtype"]
            # Branch principale: creazione vs merge. Cambia il prompt
            # e quindi la natura (e il costo) della chiamata LLM:
            # il merge include la pagina esistente in input, è più caro.
            if self.wiki.exists(page_id):
                with self.tracker.phase("ingest:l2:entity_merge"):
                    merged = self._merge_entity_page(page_id, doc_id, title, body)
                # Dominio della pagina aggiornato: se il nuovo dominio
                # differisce dal corrente, segna la pagina come "_mixed"
                # (sorgenti multi-dominio). Permette al filtro query-time
                # di scegliere se includerla.
                existing_fm, _ = self.wiki.get(page_id)
                current_domain = existing_fm.get("domain", DEFAULT_DOMAIN)
                page_domain = current_domain if current_domain == domain else MIXED_DOMAIN
            else:
                with self.tracker.phase("ingest:l2:entity_create"):
                    merged = self._create_entity_page(page_id, subtype, doc_id, title, body)
                page_domain = domain
            # Metadati aggiornati a ogni passaggio: importa che sources sia
            # cumulativo (vedi WikiStore.update_with_merge).
            extra_meta = {
                "type": "entity",
                "subtype": subtype,
                "domain": page_domain,
                "last_updated": date.today().isoformat(),
                "stale": False,
                "title": page_id.replace("_", " ").title(),
            }
            self.wiki.update_with_merge(page_id, merged, [doc_id], extra_meta=extra_meta)
            # Re-embed della pagina aggiornata. Fase dedicata per separare
            # il costo dell'embedding wiki da quello della generazione testo.
            with self.tracker.phase("ingest:l2:wiki_index"):
                self._index_wiki_page(page_id)
            touched.append(page_id)
        return touched

    def _create_entity_page(self, page_id: str, subtype: str, doc_id: str, title: str, body: str) -> str:
        """Genera ex-novo una pagina entity dal documento sorgente."""
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
