# PROGRESS — handoff per nuova sessione

Documento di passaggio di consegne. Una sessione Claude Code nuova deve
poter ripartire da qui senza il contesto della sessione precedente.
Linguaggio del progetto: italiano. Tutto su Azure OpenAI.

---

## 1. Cos'è il progetto

`hybrid-wiki/` — implementazione del sistema descritto in
`../hybrid-wiki-rag-design.md` (design doc, **v2.2**): companion wiki di
lettura ibrido **RAG + Wiki a doppio indice**, multi-corpus
dominio-agnostico. Corpora seed: `tolkien`, `asimov`.

Stack: Python, Azure OpenAI (chat `gpt-5.1`, embedding
`text-embedding-3-small` — nomi deployment da `.env`), ChromaDB locale,
Click per le CLI. Credenziali in `.env` (vedi `.env.example`).

> Nota sicurezza: in sessioni precedenti sono state incollate chiavi API
> in chat — vanno considerate compromesse e ruotate dal portale Azure.

---

## 2. Stato: COSA È FATTO E VALIDATO

Tre fasi completate e validate empiricamente su pilot a 2 corpora (~21
documenti):

1. **Walking Skeleton** — ingest L0/L1/L2, doppio indice (raw+wiki su
   ChromaDB), query con orientamento/retrieval/risoluzione conflitti,
   Hot Layer, AGENTS.md, eval set. Validato.
2. **Correzioni pilot** (design §11) — entity reuse/anti-frammentazione,
   Hot Layer batch-deferred, conflitti RAW-vs-RAW, multi-corpus/domain,
   whitelist citazioni, cap confidence, robustezza output, telemetria
   token. Validato; costo corpus −31% post-ottimizzazioni.
3. **Fase Scaling** (design §12) — completata e validata:
   - **M1** inventario gerarchico (`_identify_entities`): costo
     da O(N) a O(1), `inventory_retrieval` riusa il vettore della
     source page → zero embedding aggiuntivo.
   - **M2** lint pipeline di consolidazione duplicati/alias, due fasi
     con conferma umana.
   - **M3** classificazione L0/L1/L2 assistita + promozione retroattiva
     (CLI `--promote`).

Il design doc `../hybrid-wiki-rag-design.md` è allineato: §11 (correzioni
pilot) e §12 (correzioni Scaling) complete, footer v2.2.

---

## 3. Mappa moduli (`src/`)

- `config.py` — costanti, path, soglie. Legge `.env`. Tutte le dir create
  all'import.
- `llm_client.py` — `LLMClient` (Azure chat, `max_completion_tokens` per
  GPT-5, tracker opzionale).
- `embeddings.py` — `Embedder` (Azure embeddings, batch, tracker).
- `stores.py` — `RawStore` (raw immutabili), `WikiStore` (entity/source
  pages; `delete_page`, `rewrite_links`, `update_with_merge`), `VectorDB`
  (ChromaDB; collection `raw_chunks`/`wiki_pages`; `get_embedding`).
- `hot_layer.py` — `HotLayer`: overview (LLM) + index deterministico;
  `rebuild()`.
- `ingest.py` — `IngestPipeline`: `ingest()` (L0/L1/L2,
  `defer_hot_layer`), `promote(doc_id, new_level)` (retroattiva, NON
  duplica il raw), `rebuild_hot_layer()`, `_entity_inventory`
  (gerarchico/piatto), `_identify_entities`, `_make_source_page`,
  `_create_entity_page`, `_merge_entity_page`, `_integrate_entities`.
- `query.py` — `QueryPipeline.ask(question, domain=None)`: orientamento +
  retrieval doppio + whitelist citazioni + policy multi-corpus +
  CONFLICT_RULES (incl. RAW-vs-RAW). `_corpus_domains()`.
- `token_tracker.py` — `TokenTracker`: fasi via `contextvars`, log
  append-only `data/token_log.jsonl`, summary di sessione.
- `lint.py` — `LintPipeline`: `detect_duplicates()` (coseno esplicito +
  adjudication LLM + clustering), `apply_consolidation()`.
- `classifier.py` — `LevelClassifier.classify()` (regole → LLM few-shot →
  confidence); helper `enqueue_review/load_queue/save_queue/record_example`.

CLI (`scripts/`): `ingest_doc.py`, `ingest_folder.py`, `ask.py`,
`classify.py`, `lint.py`, `tokens.py`. Sintassi/uso in `README.md`
(sezione "Uso", recentemente riscritta per chiarezza).

---

## 4. Decisioni e lezioni architetturali (NON ri-scoprirle)

Lezioni costate iterazioni; un fresh agent le re-imparerebbe a costo. Sono
anche in design §11/§12.

1. **"Menzione ≠ trattazione sostanziale"** — pattern RICORRENTE: l'LLM
   confonde densità di nomi propri con rilevanza. Si è manifestato in
   estrazione entità (§11.1) E in classificazione (§12.5). Mitigazione
   standard ovunque si chieda all'LLM un giudizio di rilevanza: **regola
   di confine esplicita** valutata *prima* di euristiche prudenziali +
   asimmetria prudenziale ristretta al contenuto realmente sostanziale.
2. **Consolidamento ≠ gerarchia** (§12.2) — nella lint: `same_entity`/
   `alias_of` → merge; `subset_of` → relazione gerarchica, NON si fonde
   (Mount Doom è *dentro* Mordor, non un duplicato). Union-find solo su
   equivalenza, altrimenti collassa interi sottografi.
3. **Similarità ≠ identità nei corpora narrativi** (§12.3) — coseno tra
   pagine co-tematiche uniformemente alto (~0.88 anche tra entità
   diverse): è solo un pre-filtro grezzo, NON un criterio di qualità; il
   vero rilevatore è adjudication LLM + triage umano. Coseno va calcolato
   **esplicitamente dai vettori** (ChromaDB usa L2 di default, non
   coseno). Cap deterministico sul numero di adjudication.
4. **Hot Layer rebuild O(N²)** — in batch va differito a fine batch (1
   rebuild), non per documento. `defer_hot_layer=True` +
   `rebuild_hot_layer()`.
5. **Gate asimmetrico** (§12.5) — decisioni economiche/sicure → auto;
   costose/incerte (L2, low conf) → conferma umana. Sbagliare verso il
   basso è il danno; verso l'alto solo spreco.
6. **Raw immutabile** — il testo non si riscrive MAI; la sola eccezione
   è il metadato `level` in promozione (`promoted_from/at`).
7. **Operazioni distruttive** (§12.4) — passo distruttivo per ULTIMO;
   audit append-only con snapshot integrale dell'entità eliminata
   (recuperabilità oltre git).
8. **Output LLM** — sempre fallback parser (troncamento a max_tokens) +
   whitelist citazioni (l'LLM fabbrica `[[id]]` plausibili).
9. Metodologia: su pipeline stocastica un delta di 1-2 pagine wiki è
   **rumore, non segnale** — non trarre conclusioni dal solo conteggio
   aggregato; guardare la composizione.

---

## 5. Loose ends / debito noto

- **Inventario gerarchico**: oltre ~500 entità l'inventario in
  `_identify_entities` andrà pre-filtrato/gerarchico per sottocategoria
  (debito dichiarato in design §11.1/§12.1; `ENTITY_INVENTORY_CAP`).
- **Frammentazione alias-persona** residua (es. un personaggio con più
  nomi in-world) → gestita dalla lint consolidazione, non in ingest.
- **English-only sugli `entity_id`** non deterministico al 100% (è una
  regola di prompt; occasionale id in italiano).
- Eval set: nessuno scoring automatico (confronto umano), per design
  dello skeleton.

---

## 6. Cosa NON è ancora implementato (prossimi passi possibili)

Dal design (§7, §10), non avviati:
- Access control multi-utente (§7.1).
- Sincronizzazione batch / near-real-time (§6.4).
- Synthesis pages + dependency graph (§7.4) — nota: i confronti
  *espliciti* funzionano già senza; servono per aggregazione *implicita*
  e persistenza (§11.9).
- Eval framework con scoring automatico (LLM-as-judge / regression) (§7.2).
- Multimodal (§7.6); ottimizzazione costi.
- Tarare `ENTITY_INVENTORY_CAP` sul crossover empirico (~60-70) con dati
  a scala maggiore (refinement minore, non bloccante).

---

## 7. Comandi rapidi di verifica (smoke test in nuova sessione)

```powershell
cd hybrid-wiki
python scripts/lint.py                                   # health check stato attuale
python scripts/ask.py "Chi è Frodo Baggins?" --domain tolkien
python scripts/ask.py --eval tests/eval_set_crossdomain.yaml   # salva in tests/results/
python scripts/tokens.py --phase ingest                  # costi per fase
```

Reset completo (mantiene i sorgenti in `data/raw/incoming/`):
`Remove-Item -Recurse -Force data\vectors, data\wiki, data\raw\*.md, data\*_log.jsonl, data\lint, data\classification -ErrorAction SilentlyContinue`
poi re-ingest dei manifest:
`python scripts/ingest_folder.py --manifest data/raw/incoming/tolkien/manifest_tolkien.yaml`
`python scripts/ingest_folder.py --manifest data/raw/incoming/asimov/manifest_asimov.yaml`

---

## 8. Fonti autoritative

- `../hybrid-wiki-rag-design.md` — design v2.2 (§11 correzioni pilot,
  §12 correzioni Scaling). **Fonte primaria** per il "perché".
- `README.md` — uso operativo, processi e comandi (sezione "Uso"
  riscritta di recente: Ingest, eval set, Classificazione, Lint).
- `schema/AGENTS.md` — contratto operativo v0.2 (dominio-agnostico),
  iniettato in ogni prompt LLM. Regole di naming, riuso entità,
  consolidamento, confine L0.
- Log: `data/token_log.jsonl`, `data/query_log.jsonl`,
  `data/lint/applied_merges.jsonl`, `data/classification/examples.jsonl`.
