# Hybrid Wiki RAG — Walking Skeleton

Versione minima funzionante del sistema descritto in `../hybrid-wiki-rag-design.md`.
Companion wiki di lettura **multi-corpus**: il motore è dominio-agnostico, ogni
documento porta un tag `domain`. Corpora seed inclusi: `tolkien` (legendarium)
e `asimov` (Ciclo della Fondazione). Le regole operative sono in
`schema/AGENTS.md` (v0.2, dominio-agnostico).

## Cosa fa

- **Ingest** su 3 livelli (L0/L1/L2), con livello esplicito o **classificazione assistita** (l'LLM propone, l'umano conferma).
- **Doppio indice** (raw + wiki) su ChromaDB locale.
- **Query** multi-indice con orientamento dal Hot Layer e risoluzione conflitti.
- **Hot Layer** minimo (overview + index) rigenerato dopo ogni ingest L1/L2.
- **AGENTS.md v0.2** (dominio-agnostico) come contratto operativo letto in ogni chiamata LLM.
- **Multi-dominio**: tag `domain` per documento, filtro `--domain` in query, isolamento dei corpora.
- **Manutenzione**: lint health-check, consolidazione duplicati/alias a conferma umana, audit e promozione retroattiva L0.

## Cosa NON fa ancora (per design)

Access control · multimodal · synthesis pages + dependency graph · lint automatica · UI · ottimizzazione costi.

## Setup

```powershell
cd hybrid-wiki
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
# Editare .env e inserire AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT,
# AZURE_OPENAI_DEPLOYMENT (chat) e AZURE_OPENAI_EMBEDDING_DEPLOYMENT (embeddings).
# Verificare il nome della deployment di embedding nel tuo Azure resource.
```

## Uso

### Ingest

L'ingest è il processo con cui un documento entra nel sistema. Ogni documento è elaborato su uno di **tre livelli** a costo/valore crescente (design §6.1):

| Livello | Cosa produce | Quando |
|---|---|---|
| **L0** | Solo indice raw (chunk + embedding). Nessuna pagina wiki. | Alto volume, basso valore individuale (note di servizio, log). Recuperabile solo via ricerca raw. |
| **L1** | L0 + una pagina *source* (sintesi autonoma del singolo documento). | Merita una sintesi ma non va integrato col resto della wiki. |
| **L2** | L1 + estrazione entità e creazione/merge delle pagine *entity* collegate, con gestione contraddizioni. | Documenti strategici che cambiano il quadro generale. |

Il documento raw è **immutabile**: una volta ingestato non viene mai riscritto (unica eccezione: il metadato di livello in caso di promozione retroattiva — vedi *Classificazione assistita*).

**Documento singolo** — `ingest_doc.py`, livello (e per L2 il subtype) dichiarati esplicitamente:

```powershell
python scripts/ingest_doc.py --file data/raw/incoming/tolkien/frodo_intro.txt --title "Frodo Baggins" --level L2 --subtype character --domain tolkien
python scripts/ingest_doc.py --file data/raw/incoming/tolkien/lettera_routine.txt --title "Nota Mathom-house" --level L0
```

**Bulk da manifest** — `ingest_folder.py`. Un manifest YAML descrive l'intero corpus: `defaults` (dominio/livello comuni) + lista `documents` con override per documento. Il campo `level` è **opzionale**: se omesso, il documento viene classificato automaticamente (vedi *Classificazione assistita*). È il modo consigliato oltre i ~10 documenti, perché riproducibile.

```powershell
python scripts/ingest_folder.py --manifest data/raw/incoming/tolkien/manifest_tolkien.yaml --dry-run   # piano, nessuna chiamata API
python scripts/ingest_folder.py --manifest data/raw/incoming/tolkien/manifest_tolkien.yaml             # esegue
python scripts/ingest_folder.py --manifest data/raw/incoming/asimov/manifest_asimov.yaml               # secondo corpus
python scripts/ingest_folder.py --manifest data/raw/incoming/tolkien/manifest_tolkien.yaml --force     # re-ingesta anche i già presenti
```

Proprietà del bulk ingest:
- **Idempotente**: salta i documenti già ingestati riconoscendoli da `(source_filename, domain)`; `--force` li re-ingesta.
- **Tollerante agli errori**: un documento che fallisce non blocca il batch (conteggiato in `FAILED`).
- **Hot Layer differito**: il rebuild dell'index è O(pagine_totali); il bulk lo esegue **una sola volta a fine batch** invece che per documento (altrimenti O(N²) sul corpus). `ingest_doc.py` su singolo documento ricostruisce subito.

### Domini multipli

Il motore è dominio-agnostico: ogni documento porta un tag `domain` (da `--domain` in `ingest_doc.py` o dal campo `domain` nel manifest). I corpora restano isolati; una pagina wiki che aggrega sorgenti di domini diversi assume `domain=_mixed` ed è inclusa in tutti i filtri. In query si filtra per dominio:

```powershell
python scripts/ask.py "Chi è il protagonista?" --domain tolkien
python scripts/ask.py "Chi è il protagonista?" --domain asimov
```

### Interrogare la knowledge base

Una domanda esegue la pipeline di query (design §6.2): orientamento dal Hot Layer → retrieval doppio (wiki + raw, filtrabile per `--domain`) → risoluzione conflitti → risposta con citazioni, livello di confidence e gap dichiarati. Ogni query è loggata in `data/query_log.jsonl`.

```powershell
python scripts/ask.py "Chi è il portatore dell'Anello?"
python scripts/ask.py "Quanti abitanti ha Trantor?" --domain asimov
```

### eval set

L'eval set è un file YAML di domande con **risposta e sorgenti attese**, organizzate per categoria (sintesi, dettaglio raw, conflitto, relazione, gap, cross-dominio). Eseguirlo lancia la pipeline di query su ogni domanda e mostra **affiancati** la risposta del sistema e l'atteso dichiarato: è lo strumento di valutazione qualitativa (design §7.2). Nel walking skeleton non c'è scoring automatico — il confronto è umano.

Due eval set inclusi:
- `tests/eval_set.yaml` — dominio singolo (tolkien).
- `tests/eval_set_crossdomain.yaml` — cross-dominio; ogni domanda può dichiarare il proprio `domain`, per testare isolamento e contaminazione tra corpora.

```powershell
python scripts/ask.py --eval tests/eval_set.yaml
python scripts/ask.py --eval tests/eval_set_crossdomain.yaml
python scripts/ask.py --eval tests/eval_set.yaml --domain tolkien   # forza un dominio su tutte le domande
```

Ogni esecuzione, oltre alla console, salva un report in `tests/results/evalset_results_YYYYMMDD_HHMMSS.txt` (un file per run, contenuto identico alla console: header con eval set/timestamp/filtro dominio, ogni domanda con risposta–sorgenti–confidence–atteso, riepilogo token finale). Un file per esecuzione permette di confrontare run diversi nel tempo e individuare regressioni.

### Classificazione L0/L1/L2 assistita

Assegnare il livello a mano non scala. La classificazione assistita (design §6.1) segue il principio *l'LLM propone, l'umano conferma*, con una decisione a tre stadi:

1. **Regole deterministiche** (`data/classification/rules.yaml`, opzionale): se un documento combacia per sorgente/titolo/dominio, livello fissato a regola — niente LLM.
2. **Proposta LLM**: criteri da `AGENTS.md` + esempi few-shot dalle conferme umane precedenti. È *active learning*: ogni conferma affina le proposte successive.
3. **Gate di confidenza asimmetrico**: regola, oppure L0/L1 ad **alta** confidence → ingest automatico; **L2 o confidence non alta → coda di review umana**. Mai auto-ingest di un L2: sbagliare verso il basso perde il documento per le query concettuali ed è il rischio grave; sbagliare verso l'alto è solo spreco.

Workflow proposta → conferma:

```powershell
# 1. proposta read-only su un singolo documento
python scripts/classify.py --file data/raw/incoming/tolkien/frodo_intro.txt --title "Frodo Baggins" --domain tolkien

# 2. proposta + accodamento per review
python scripts/classify.py --file <doc> --title "<t>" --domain <d> --enqueue

# 3. esamina la coda delle proposte in attesa
python scripts/classify.py --review

# 4. l'umano edita data/classification/review_queue.yaml impostando, per ogni entry,
#    approved_level: L0|L1|L2   (oppure 'reject' per scartare, null = lascia in attesa)

# 5. esegue gli approvati: ingest al livello scelto + registra l'esempio (active learning)
python scripts/classify.py --confirm
```

Nel **bulk ingest** la classificazione è automatica per i documenti senza `level` nel manifest: stesso gate (auto-ingest dei casi sicuri, accodamento del resto in `review_queue.yaml`).

**Promozione retroattiva** — un documento ingestato come L0 può rivelarsi strategico (design §6.1). Un audit ri-classifica i documenti L0 e segnala i candidati; la promozione è poi eseguita esplicitamente dall'umano e **non duplica il raw immutabile**: riesegue solo gli step wiki del nuovo livello e aggiorna il solo metadato `level` (`promoted_from`/`promoted_at`).

```powershell
python scripts/lint.py --audit-l0                          # ri-classifica i doc L0, marca [PROMOTE] i candidati
python scripts/classify.py --promote <doc_id> --level L2   # esegue la promozione (human-gated)
```

### Lint: health check e consolidazione duplicati

`lint.py` raccoglie le operazioni di manutenzione della knowledge base (design §6.3), in tre modalità.

**1. Health check** (default, read-only) — fotografia di integrità: conteggi (documenti raw, pagine wiki, vettori), dimensione del Hot Layer, pagine wiki senza sorgenti, pagine non presenti nell'index, riferimenti a sorgenti inesistenti.

```powershell
python scripts/lint.py
```

**2. Consolidazione duplicati/alias** — crescendo, la wiki genera pagine ridondanti per la stessa entità (sinonimi, varianti, alias/persona). È **a due fasi con conferma umana** (l'output del lint non è mai automatico, design §6.3):

- **DETECT** (read-only): similarità coseno tra pagine entità calcolata sui vettori già in ChromaDB (costo embedding nullo) → l'LLM giudica ogni coppia candidata distinguendo **duplicati** (`same_entity`/`alias_of` → da fondere) da **relazioni gerarchiche** (`subset_of` → NON si fondono, restano entità distinte: es. *Monte Fato* è *dentro* Mordor, non un suo duplicato). Scrive `data/lint/consolidation_report.yaml` coi cluster proposti, tutti `approved: false`. Filtro same-domain: mai merge cross-corpus.
- **Triage umano**: si rivede il report e si mette `approved: true` solo sui cluster corretti (`canonical`/`aliases` editabili a mano).
- **APPLY**: fonde i soli cluster approvati nel canonical, riscrive i link `[[alias]]→[[canonical]]` in tutte le pagine, elimina pagina e vettore dell'alias, ricostruisce il Hot Layer una volta. Reversibile: la wiki è in git e `data/lint/applied_merges.jsonl` conserva lo snapshot integrale (frontmatter + body) di ogni alias eliminato.

```powershell
python scripts/lint.py --detect-duplicates      # FASE 1 (read-only) → report YAML
# rivedere data/lint/consolidation_report.yaml, approved: true sui cluster giusti
python scripts/lint.py --apply-consolidation    # FASE 2 (distruttivo, solo cluster approvati)
```

**3. Audit L0** — ri-classifica i documenti L0 per individuare candidati alla promozione (vedi *Classificazione assistita*). Read-only: segnala, non promuove.

```powershell
python scripts/lint.py --audit-l0
python scripts/lint.py --audit-l0 --sample 20   # campiona 20 doc L0 invece di tutti
```

### Report consumo token

Ogni invocazione di `ingest_doc.py` e `ask.py` stampa un riepilogo dei token spesi nella sessione (breakdown per fase). Il dettaglio completo viene loggato in append a `data/token_log.jsonl`. Per analisi cumulative:

```powershell
python scripts/tokens.py                                # report totale
python scripts/tokens.py --phase ingest                 # solo ingest (tutti i sotto-step)
python scripts/tokens.py --phase query:llm              # solo la sintesi finale
python scripts/tokens.py --since 24h                    # ultime 24 ore
python scripts/tokens.py --csv tokens.csv               # esporta record raw in CSV (1 riga per chiamata)
python scripts/tokens.py --csv-summary by_phase.csv     # esporta aggregato per fase (per grafici scaling)
```

Le fasi tracciate:
- `ingest:l{0,1,2}:raw_index`     — embedding dei chunk raw
- `ingest:l1:source_page`         — generazione pagina source L1
- `ingest:l2:source_page`         — generazione pagina source L2
- `ingest:l2:identify_entities`   — estrazione JSON entità
- `ingest:l2:entity_create`       — creazione nuova pagina entity
- `ingest:l2:entity_merge`        — merge in pagina entity esistente
- `ingest:l2:wiki_index`          — embedding pagina wiki aggiornata
- `ingest:hot_layer_rebuild`      — rigenerazione overview Hot Layer
- `query:embedding`               — embedding della domanda
- `query:llm_synthesis`           — chiamata finale di risposta

## Struttura

```
hybrid-wiki/
├── src/                       # moduli core (ingest, query, lint, classifier, stores, ...)
├── data/
│   ├── raw/incoming/          # documenti sorgente per dominio (tolkien/, asimov/) + manifest
│   ├── raw/                   # documenti originali ingestati (immutabili)
│   ├── wiki/                  # pagine sintetizzate + HOT_LAYER.md
│   ├── vectors/               # ChromaDB (creato a runtime)
│   ├── lint/                  # consolidation_report.yaml + applied_merges.jsonl (audit)
│   ├── classification/        # review_queue.yaml + examples.jsonl (active learning) + rules.yaml
│   └── *_log.jsonl            # query_log, token_log (audit append-only)
├── schema/AGENTS.md           # contratto operativo dominio-agnostico (v0.2)
├── scripts/                   # CLI: ingest_doc, ingest_folder, ask, classify, lint, tokens
└── tests/
    ├── eval_set.yaml          # eval dominio singolo (tolkien)
    ├── eval_set_crossdomain.yaml
    └── results/               # report per-run: evalset_results_YYYYMMDD_HHMMSS.txt
```

## Note di funzionamento

- **Modelli**: tutto su Azure OpenAI. Generazione tramite deployment chat (`gpt-5.1` di default), embedding tramite deployment dedicato (`text-embedding-3-small` di default). Entrambi i nomi sono configurabili via `.env`. Il client usa `max_completion_tokens` (richiesto dalla serie GPT-5).
- **Persistenza**: ChromaDB persistente in `data/vectors/`. Per resettare il sistema basta cancellare la cartella `data/` (escluso `data/raw/incoming/`).
- **Audit trail minimo**: ogni query viene loggata in append a `data/query_log.jsonl`.
- **Contraddizioni volute** nei dataset seed (test della conflict resolution):
  - `tolkien`: fondazione della Contea, 1601 in `contea.txt` vs 1604 in `consiglio_elrond.txt` (`eval_set.yaml#q09`).
  - `asimov`: popolazione di Trantor, ~40 mld in `trantor.txt` vs ~45 mld in `impero_galattico.txt` (`eval_set_crossdomain.yaml#x05`).
- **Stress tassonomia**: `psicostoria` e `fondazione` (corpus asimov) non rientrano nei `subtype` standard (character/place/artifact/event/book). La pipeline crea comunque la pagina entity con `subtype: ""` invece di forzare un tipo errato (vedi AGENTS.md, "Limite noto della tassonomia").
- **Isolamento domini**: una pagina wiki che aggrega sorgenti di domini diversi assume `domain: _mixed`; il filtro `--domain X` include `X` + `_mixed`.

## Stato e roadmap

**Completati**: Walking Skeleton · correzioni dal pilot (design §11) · fase **Scaling** (design §12: inventario gerarchico, lint di consolidazione, classificazione assistita, promozione retroattiva).

**Non ancora implementati** (design §7 e §10): access control multi-utente, sincronizzazione batch/near-real-time, synthesis pages + dependency graph, eval framework con scoring automatico, multimodal, ottimizzazione costi.

Riferimento completo: `../hybrid-wiki-rag-design.md` (v2.2, §§11–12 per le correzioni validate sul pilot).
