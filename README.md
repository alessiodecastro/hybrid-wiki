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
- **Graph layer (Arch B, sperimentale)**: indice strutturale a grafo (Kuzu) come **alternativa all'embedding wiki**. Le pagine MD restano su disco per la revisione umana ma non vengono embeddate; il retrieval pesca da raw + sottografo entità invece che da raw + wiki vettoriale. Vedi sezione *[Graph layer — indice strutturale sperimentale (Arch B)](#graph-layer--indice-strutturale-sperimentale-arch-b)*.

## Cosa NON fa ancora (per design)

Access control · multimodal · synthesis pages · lint automatica · UI · ottimizzazione costi.

> Il **grafo strutturale delle entità** (chiamato *grafo dei collegamenti* nel design §4.1/§5.2, finora mai materializzato) esiste ora come **prototipo sperimentale "Arch B"**: vedi sezione dedicata e design §14.

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

L'ingest è il processo con cui un documento entra nel sistema. Ogni documento è elaborato su uno di **tre livelli** a costo/valore crescente (design §6.1). I tre livelli sono **incrementali**: L1 include tutto ciò che fa L0, L2 include tutto ciò che fa L1.

| Livello | File su disco prodotti | Vettori ChromaDB | Hot Layer | Quando usarlo |
|---|---|---|---|---|
| **L0** | `data/raw/<doc_id>.md` (copia immutabile con frontmatter). Nessun file in `data/wiki/`. | `raw_chunks`: N chunk da ~200 parole con overlap 40, embeddati uno per uno (`ingest:l0:raw_index`). | Non aggiornato (il doc non compare nell'index). | Alto volume, basso valore individuale (avvisi, log, note di servizio, comunicazioni operative). Recuperabile **solo** via ricerca raw — non emerge nelle query orientative basate sul Hot Layer. |
| **L1** | L0 + `data/wiki/source_<doc_id>.md` (pagina *source*: sintesi autonoma del singolo documento, sezioni `## Overview / ## Dettagli / ## Citazioni notevoli`). | L0 + `wiki_pages`: 1 vettore per la pagina source (`ingest:l1:source_page` + `ingest:l2:wiki_index` per l'embedding). | Aggiornato a fine batch: la source page compare nell'index del Hot Layer. | Documento che merita una sintesi propria ma **non** va integrato col resto della wiki (es. atto puntuale, articolo singolo, scheda non riusabile). |
| **L2** | L1 + aggiornamento di `data/wiki/_entity_index.yaml` (sempre) + 0 o più file `data/wiki/<entity_id>.md` materializzati **solo quando un'entità raggiunge la soglia di consolidamento** (default 3 sources). Le entità sotto soglia restano `aliased`: registrate nell'indice ma senza file. | L1 + 1 vettore per ogni entity page **consolidated o aggiornata** in questo ingest. Niente vettori per le aliased. | Le entity consolidated compaiono nell'index del Hot Layer; le aliased sono solo contate aggregatamente. | Documento strategico (biografie complete, eventi cardine, concetti centrali): cambia il quadro generale del corpus e va integrato. |

**Lettura della tabella**: un ingest L2 in genere produce 1 raw + 1 source page + alcune righe nell'`_entity_index.yaml`. Le entity page md vengono materializzate **solo** quando un'entità accumula la N-esima source (default N=3). Vedi sezione *Pagina source vs pagina entity* sotto e §13 del design doc per il razionale architetturale (lazy materialization).

Il documento raw è **immutabile**: una volta ingestato non viene mai riscritto. Unica eccezione: il metadato `level` in caso di **promozione retroattiva** (un doc L0 diventato strategico viene rieseguito agli step L1/L2 senza duplicare il raw — vedi *Classificazione assistita*).

#### Pagina *source* vs pagina *entity* (lazy materialization)

I file in `data/wiki/` sono di **due tipi** (campo `type` nel frontmatter), con cicli di vita radicalmente diversi. **Le entity sono materializzate lazy**: una citazione `[[entity_id]]` può esistere anche se la pagina md ancora non c'è (design §13).

| | **Source page** (`type: source`) | **Entity page** (`type: entity`) |
|---|---|---|
| **Identità** | "vista sul documento": fissa la prospettiva di **un singolo raw** | "vista sull'entità del mondo": rappresenta una cosa (personaggio, luogo, evento) **trasversale ai documenti** |
| **Cardinalità sorgenti** | 1:1 — sempre **una sola** source (il doc da cui è stata generata) | 1:N — lista cumulativa, deduplicata, di tutti i raw che hanno contribuito |
| **Mutabilità** | **Append-only / immutabile**. Una volta scritta non viene mai più modificata. | **Mergeable**. Dopo il consolidamento, ogni nuovo doc che la menziona la raffina o contraddice (sezione `## Contraddizioni note`). |
| **Fedeltà** | Fedele al singolo doc anche se in conflitto con altri (se il raw dice "1604", la source dice "1604") | Aggrega le tensioni: se due raw divergono, l'entity **mantiene entrambe le versioni esplicitamente** |
| **id** | `source_<doc_id>` (con timestamp del doc) | `<entity_id>` (slug inglese snake_case stabile, es. `frodo_baggins`, `one_ring`) |
| **Domain** | Sempre del singolo doc | Diventa `_mixed` se le sources sono di domini diversi |
| **Struttura** | `## Overview / ## Dettagli / ## Citazioni notevoli` | `## Panoramica / ## Dettagli / ## Relazioni / ## Domande aperte` (+ `## Contraddizioni note` opzionale) |
| **Esiste dal livello** | L1 in su | L2, ma **solo se l'entità ha ≥ THRESHOLD sources** (default 3) |
| **Stati** | n/a (sempre presente) | `aliased` (registrata nell'indice, NO file) → `consolidated` (raggiunta soglia, file + vettore creati) |

#### L'indice centrale `_entity_index.yaml`

Tutte le entità del corpus — siano materializzate o meno — vivono in `data/wiki/_entity_index.yaml`. È il **single source of truth** per:
- "esiste un'entità con questo id?" → dedup e anti-frammentazione
- "quali source contribuiscono a quest'entità?" → recupera le source pertinenti senza scan
- "quest'entità è consolidata o aliased?" → decide il branch in ingest e in retrieval

Esempio:

```yaml
version: 1
threshold: 3
entities:
  - id: hari_seldon
    subtype: character
    domain: asimov
    sources: [hari_seldon_20260518190004, la_fondazione_20260518190604, le_crisi_seldon_20260518190350]
    n_sources: 3
    state: consolidated
    consolidated_at: '2026-05-20T15:00:00'
  - id: tom_riddle_sr
    subtype: character
    domain: rowling
    sources: [voldemort_20260520113156]
    n_sources: 1
    state: aliased
    consolidated_at: null
```

#### Come si declina nell'ingest L2

```
L0 → indicizzazione raw (chunk + embeddings)
L1 → L0 + _make_source_page()             # sempre CREATE, mai merge (doc_id unico)
L2 → L1 + _integrate_entities():
       per ogni entity_id estratta:
         indice.upsert_contribution(entity_id, doc_id)
         ├─ ALIASED sotto soglia      → solo update indice, ZERO LLM, nessun file
         ├─ raggiunge la soglia       → _consolidate_entity()  # 1 chiamata LLM su N source page
         └─ già CONSOLIDATED          → _merge_entity_page()   # merge incrementale, 1 chiamata LLM
```

La source ha **un solo flusso** (`create`). Lo stesso file ingestato due volte produce **due** source page distinte, mai una fusione.

L'entity ha **tre flussi**, scelti runtime in base allo stato nell'indice + n_sources. Le entità sotto soglia non costano nulla in LLM: solo una riga aggiunta all'YAML. Le entità che raggiungono la soglia pagano una **singola** chiamata LLM "consolidamento" che fonde le N source in una entity page nuova. Da consolidate in poi, comportamento merge incrementale classico.

Il **consolidamento** legge le source page già nel WikiStore (non i raw grezzi): le source sono sintesi già strutturate, contesto migliore di N raw per il prompt LLM. Anche per questo lavora bene anche su corpora narrativi con contenuti densi.

#### Conseguenze operative

| Aspetto | Source | Entity |
|---|---|---|
| Conflict resolution | Non opera (1 sola fonte per definizione) | Qui scattano `## Contraddizioni note` (post-consolidamento) e le `CONFLICT_RULES` lato query (sempre) |
| Lint consolidation (`--detect-duplicates`) | Non opera sulle source | Opera **solo** sulle entity consolidated |
| Promozione retroattiva | Crea una nuova source (con `promoted_from`) | Può aggiornare entity esistenti tramite re-integration |
| Citazioni `[[id]]` in risposta | Per dettagli puntuali, numeri, citazioni testuali | Per concetti, sintesi, relazioni — **anche se l'entità è aliased**: il retrieval prende le source e l'LLM sintetizza runtime |

In una battuta: **la source è la memoria di "cosa ha detto questo documento", l'entity è la memoria di "cosa sappiamo su questa cosa"** — ma materializzata solo quando ne vale la pena. La prima fissa una prospettiva e non la rinnega mai; la seconda costruisce un consensus cumulativo gestendo esplicitamente le tensioni tra fonti.

#### Osservabilità (`lint --entity-stats`)

```powershell
python scripts/lint.py --entity-stats
```

Report read-only dello stato del wiki layer: distribuzione `aliased / consolidated`, istogramma `n_sources` (1, 2, 3-5, 6-10, >10), top entità per `n_sources` (le "regine" del corpus), aliased candidate a consolidamento manuale, costo cumulato `entity_consolidate` + `entity_merge` dal token log, verdetto sulla soglia. È lo strumento per tarare `ENTITY_CONSOLIDATION_THRESHOLD` (env var) sui dati reali del tuo corpus.

**Documento singolo** — `ingest_doc.py`, livello (e per L2 il subtype) dichiarati esplicitamente:

```powershell
python scripts/ingest_doc.py --file data/raw/incoming/tolkien/frodo_intro.txt --title "Frodo Baggins" --level L2 --subtype character --domain tolkien
python scripts/ingest_doc.py --file data/raw/incoming/tolkien/lettera_routine.txt --title "Nota Mathom-house" --level L0
```

**Bulk da manifest** — `ingest_folder.py`. Un manifest YAML descrive l'intero corpus: `defaults` (campi ereditati da tutti i documenti) + lista `documents` con override per documento. È il modo consigliato oltre i ~10 documenti, perché riproducibile e idempotente.

Struttura attesa del manifest:

```yaml
# base_dir: opzionale; default = cartella del manifest. Path dei file relativi a questa.
defaults:
  domain: <stringa libera>   # es. tolkien, asimov, rowling, work_notes
  # level: <opzionale qui — vedi sotto>
documents:
  - { file: <nome_file.txt>, title: "<titolo leggibile>", level: L2, subtype: character }
  - { file: <nome_file.txt>, title: "<titolo leggibile>", level: L1 }
  - { file: <nome_file.txt>, title: "<titolo leggibile>", level: L0 }
  - { file: <nome_file.txt>, title: "<titolo leggibile>" }   # senza level → classificazione assistita
```

Campi obbligatori per documento: `file`, `title`. Campi opzionali: `level` (L0/L1/L2), `subtype` (solo per L2: `character|place|artifact|event|book`), `domain` (override del default). Ogni campo presente nell'entry **vince** sul default omonimo.

**Comportamento del campo `level` — il punto importante.** Cosa succede a un documento dipende dalla **combinazione** tra `defaults.level` e il `level` nell'entry:

| `defaults.level` | `level` nell'entry | Risultato per il documento |
|---|---|---|
| `L1` (o qualunque livello) | `L2` (esplicito) | Ingest a L2 (l'entry vince sul default). |
| `L1` (o qualunque livello) | assente | Ingest a L1 (eredita dal default). **Nessuna classificazione**. |
| **assente** | `L2` (esplicito) | Ingest a L2. |
| **assente** | assente | **Classificazione assistita**: l'LLM propone il livello, il gate asimmetrico decide se auto-ingestare o accodare per review umana (vedi *Classificazione L0/L1/L2 assistita*). |

In pratica: per innescare la classificazione assistita su una parte dei documenti del manifest, occorre **omettere `level` dai defaults** e ometterlo nelle entry che si vogliono classificare. Mettere un default ovunque significa rinunciare alla classificazione assistita per tutto il batch.

Nel dry-run, le entry che andranno a classificazione compaiono come `PLAN [CLASSIFY]` invece che `PLAN [L1]`.

```powershell
python scripts/ingest_folder.py --manifest data/raw/incoming/tolkien/manifest_tolkien.yaml --dry-run   # piano, nessuna chiamata API
python scripts/ingest_folder.py --manifest data/raw/incoming/tolkien/manifest_tolkien.yaml             # esegue
python scripts/ingest_folder.py --manifest data/raw/incoming/asimov/manifest_asimov.yaml               # secondo corpus
python scripts/ingest_folder.py --manifest data/raw/incoming/tolkien/manifest_tolkien.yaml --force     # re-ingesta anche i già presenti
```

Marker stampati in output (uno per documento):

| Marker | Significato |
|---|---|
| `OK    [Lx] domain file  doc_id=... wiki=[...]` | Ingestato con successo al livello indicato; elenca i file wiki prodotti. |
| `SKIP  [Lx] domain file  (già ingestato)` | Idempotenza: stesso `(source_filename, domain)` già nel raw store. `--force` ignora il check. |
| `CLASS [Lx] domain file  -> Lx/conf (LLM\|REGOLA) auto-ingest` | Classificato e ingestato automaticamente (gate: L0/L1 high-confidence, oppure regola deterministica). |
| `QUEUE [??] domain file  proposto Lx/conf → review umana` | Classificato ma accodato per conferma umana (gate: L2 o confidence non alta). Vai a `classify.py --review`. |
| `FAIL  [Lx] domain file  -> <errore>` | Errore (encoding, prompt rejection, ecc.). Il batch prosegue. |
| `MISS  [Lx] domain file  (file non trovato)` | Il file dichiarato nel manifest è assente dal disco. |

Proprietà del bulk ingest:
- **Idempotente**: salta i documenti già ingestati riconoscendoli da `(source_filename, domain)`; `--force` li re-ingesta. Lo stesso file in un dominio diverso non viene considerato duplicato (caso lecito).
- **Tollerante agli errori**: un documento che fallisce non blocca il batch (conteggiato in `FAILED`).
- **Hot Layer differito**: il rebuild dell'index è O(pagine_totali); il bulk lo esegue **una sola volta a fine batch** invece che per documento (altrimenti O(N²) sul corpus). `ingest_doc.py` su singolo documento ricostruisce subito.

### Domini multipli

Il motore è dominio-agnostico: ogni documento porta un tag `domain` (da `--domain` in `ingest_doc.py` o dal campo `domain` nel manifest). I corpora restano isolati; una pagina wiki che aggrega sorgenti di domini diversi assume `domain=_mixed` ed è inclusa in tutti i filtri. In query si filtra per dominio:

```powershell
python scripts/ask.py "Chi è il protagonista?" --domain tolkien
python scripts/ask.py "Chi è il protagonista?" --domain asimov
```

### Interrogare la knowledge base (`ask.py`)

Una domanda esegue la pipeline di query (design §6.2) implementata in `src/query.py`. Ogni query è loggata in `data/query_log.jsonl` (audit trail append-only).

```powershell
python scripts/ask.py "Chi è il portatore dell'Anello?"
python scripts/ask.py "Quanti abitanti ha Trantor?" --domain asimov
python scripts/ask.py --eval tests/eval_set_threedomains.yaml   # batch su un eval set
```

#### Cosa fa la pipeline, passo per passo

La query è una **singola chiamata LLM** preceduta da una fase di preparazione del contesto. Il modello non itera né richiama tool: ha tutto in una sola passata e produce risposta + metadati strutturati.

1. **Embedding della domanda** (`query:embedding`).
   La domanda viene embeddata con lo stesso modello usato per i documenti (Azure `text-embedding-3-small`). Costo: 1 chiamata embedding, ~50-300 token.

2. **Retrieval multi-indice in parallelo** (operazione locale su ChromaDB, **zero token**).
   - `wiki_pages`: top **4** pagine (sintesi + relazioni). Filtro: `domain ∈ {<domain>, _mixed}` se `--domain` è passato.
   - `raw_chunks`: top **6** chunk grezzi (dettagli, numeri, citazioni testuali). Filtro: `domain = <domain>` stretto se `--domain` è passato.
   - I due `top-k` sono separati per design (`WIKI_TOP_K=4`, `RAW_TOP_K=6`): wiki sono pagine dense, raw sono frammenti corti — servono cardinalità diverse.
   - I filtri sono applicati **lato ChromaDB**, non post-filter: così il top-k opera già sul sottoinsieme rilevante e non si "perdono" hit utili.

3. **Caricamento del Hot Layer** (file `data/wiki/HOT_LAYER.md`, ricostruito ad ogni ingest L1/L2).
   È una "mappa" del corpus: overview tematica (generata da LLM) + index deterministico delle pagine entity con tag. Iniettato nel system prompt come **orientamento**: dice al modello "ecco la geografia del corpus, ecco dove cercare".

4. **Calcolo della whitelist citazioni** (cruciale anti-hallucination).
   Dai hit recuperati si estrae l'elenco esatto dei `page_id` (wiki) e dei `doc_id` (raw) che il modello **può** citare con `[[id]]`. Per riferirsi a entità non recuperate, il modello deve usare il nome in chiaro **senza wikilink**. Senza questa whitelist il modello fabbrica id plausibili ma inesistenti.

5. **Policy multi-corpus** (si attiva solo se `--domain` non è passato e il corpus contiene più di un dominio).
   Una scansione filesystem rileva quali domini esistono nel corpus e quali sono coperti dal retrieval. Il system prompt riceve due regole:
   - Se il retrieval copre **più** domini → struttura la risposta **per sezioni separate per dominio** (no fusione tra mondi), cap confidence a `medium`.
   - Se copre **un solo** dominio ma il corpus ne ha altri → rispondi sul dominio coperto **ma dichiara esplicitamente** che gli altri corpora potrebbero rispondere diversamente; cap confidence a `medium` (eccetto quando la domanda nomina entità univoche).
   Questa policy nasce per evitare la "dominanza silenziosa" — domanda generica → risposta da un solo corpus senza segnalare gli altri (caso classico osservato in eval).

6. **Costruzione del system prompt** (lungo per design, è la "configurazione runtime" del modello).
   Contiene, nell'ordine: la strategia in 6 punti, l'eventuale policy multi-corpus, lo **scan obbligatorio dei conflitti** (vedi sotto), le regole di citazione con whitelist, il vincolo di compattezza, le `CONFLICT_RULES`, il contratto di output JSON, `AGENTS.md`, e il **Hot Layer** in coda.

7. **Sintesi finale** (`query:llm_synthesis`, fase più costosa).
   Una singola chiamata `chat.completions.create` con `max_tokens=3500`. Il modello riceve la domanda + i blocchi wiki+raw formattati e produce risposta in markdown **terminata con un blocco JSON** auto-descrittivo (`answer / wiki_sources / raw_sources / confidence / gaps`).

8. **Parsing tollerante della risposta**.
   - *Path nominale*: estrae l'ultimo blocco JSON dal testo e ne ricava i campi strutturati.
   - *Fallback*: se il JSON manca o è malformato (tipico caso: risposta troncata a `max_tokens`), estrae gli id dai `[[wikilink]]` inline e li classifica con un'euristica (`doc_id` se termina con `_AAAAMMGGHHMMSS`, altrimenti `page_id`). In questo caso la `confidence` viene forzata a `low` per segnalare onestamente il degrado.

9. **Append al query log**.
   Record JSONL in `data/query_log.jsonl` con domanda, timestamp, risposta, sorgenti, confidence, gap.

#### Cosa cerca dove, e a chi crede di più

La pipeline cerca **due cose diverse in due indici diversi**, e ha regole esplicite su quale prevale quando divergono. Queste regole — le `CONFLICT_RULES` — sono iniettate nel system prompt di **ogni** query.

| Tipo di informazione cercata | Indice prioritario | Perché |
|---|---|---|
| Numeri specifici (date, cifre, codici) | **RAW** | I dati grezzi sono fedeli al documento originale; le sintesi possono arrotondare. |
| Citazioni testuali | **RAW** | Solo il raw contiene il testo letterale. |
| Stati attuali ("cosa è vero ora") | **RAW** se più recente, **WIKI** se aggrega più fonti | Trade-off recency vs aggregazione. |
| Sintesi e interpretazioni | **WIKI** | È esattamente il lavoro che le pagine entity fanno. |
| Relazioni e collegamenti | **WIKI** | Le entity hanno la sezione `## Relazioni`; i raw vedono solo la propria prospettiva. |

**Comportamento sui conflitti**:
- **WIKI vs RAW divergono su un fatto**: la risposta **non nasconde il conflitto** — espone entrambe le versioni ("secondo [[wiki_page]]…; secondo il documento [[doc_id]]…") e indica quale prevale per regola. (Esempio nel corpus: Hogwarts fondata "circa 990" nella entity vs "993" in un raw → la risposta cita entrambe le date.)
- **RAW vs RAW divergono** (due documenti grezzi danno valori diversi sullo stesso fatto, nessuno chiaramente più autorevole): il modello **non** ne sceglie uno arbitrariamente. Riporta **entrambi** con le rispettive citazioni, dichiara il conflitto come irrisolto, e cappa la confidence a `medium` (o `low` se il fatto è centrale per la domanda). (Esempio: popolazione di Trantor, ~40 mld vs ~45 mld in due raw asimov diversi.)

Per forzare questo comportamento il prompt include uno **scan obbligatorio dei conflitti** prima della formulazione della risposta: il modello deve fare una passata attiva su tutti i frammenti retrieved (wiki + raw) cercando discrepanze su numeri, date, nomi — **incluse menzioni di passaggio nei chunk marginali**, perché una contraddizione in un frammento secondario è un segnale, non rumore. Senza questo vincolo esplicito il modello tende a fidarsi del primo risultato ad alto ranking e ignorare il resto.

#### Cosa contiene la risposta

Ogni invocazione di `ask.py` stampa:

- la **risposta** in markdown, con citazioni inline `[[id]]` (solo id presenti nella whitelist);
- le **Wiki sources** e **Raw sources** effettivamente usate;
- la **Confidence** (`high` / `medium` / `low`) — calibrata per tipo di domanda e qualità delle fonti;
- eventuali **Gaps**: lacune dichiarate dal modello (entità non coperte, conflitti irrisolti, dominio non retrieved).

In modalità `--eval` ogni domanda viene affiancata al proprio `expected_summary` per confronto umano; il run viene salvato anche in `tests/results/evalset_results_YYYYMMDD_HHMMSS.txt` per analisi di regressione.

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

1. **Regole deterministiche** (`data/classification/rules.yaml`, opzionale): se un documento combacia per sorgente/titolo/dominio, livello fissato a regola — niente LLM, confidence alta, `rule_applied=True`.
2. **Proposta LLM**: criteri da `AGENTS.md` + esempi few-shot dalle conferme umane precedenti (`data/classification/examples.jsonl`). È *active learning*: ogni conferma affina le proposte successive. Output JSON `{level, confidence, rationale}`.
3. **Gate di confidenza asimmetrico**: regola, oppure L0/L1 ad **alta** confidence → ingest automatico; **L2 o confidence non alta → coda di review umana** (`data/classification/review_queue.yaml`). Mai auto-ingest di un L2: sbagliare verso il basso perde il documento per le query concettuali ed è il rischio grave; sbagliare verso l'alto è solo spreco.

**Quando si attiva la classificazione.** La classificazione viene innescata in due situazioni:
- da `ingest_folder.py`, automaticamente, per ogni documento del manifest **senza `level`** (e senza default `level` ereditato — vedi tabella nella sezione *Ingest*);
- da `classify.py --file ...`, esplicitamente, su un singolo documento (utile per testare una proposta prima di decidere se ingestare).

In entrambi i casi le entry che il gate non auto-ingesta vengono accodate in `review_queue.yaml` con `approved_level: null` (in attesa).

**Workflow proposta → conferma** (`classify.py`):

```powershell
# 1. PROPOSTA su singolo documento, read-only (stampa livello/confidence/motivazione).
python scripts/classify.py --file data/raw/incoming/tolkien/frodo_intro.txt --title "Frodo Baggins" --domain tolkien

# 2. PROPOSTA + accodamento per review umana.
python scripts/classify.py --file <doc> --title "<t>" --domain <d> --enqueue

# 3. REVIEW della coda (read-only): elenca le entry con stato PENDING / -> L0|L1|L2|reject.
python scripts/classify.py --review

# 4. EDIT MANUALE di data/classification/review_queue.yaml: per ogni entry impostare
#    approved_level a uno tra:
#       L0 | L1 | L2   → verrà ingestato a questo livello dal --confirm
#       reject         → scartato (uscirà dalla coda, nessun ingest)
#       null           → lasciato in attesa (resterà in coda anche dopo --confirm)
#    Il subtype (per L2) NON va specificato: è estratto automaticamente dal contenuto.

# 5. CONFIRM: ingesta le entry approvate al livello scelto, le rimuove dalla coda
#    e le registra come esempi few-shot per le classificazioni successive.
python scripts/classify.py --confirm
```

**Cosa fa `--confirm`** entry per entry, in ordine:

| `approved_level` nella queue | Azione di `--confirm` |
|---|---|
| `L0` / `L1` / `L2` | Chiama `pipeline.ingest(file, level=approved, subtype=None, domain=...)`. Per L2 il subtype viene proposto dall'estrattore entità sul contenuto. La decisione viene registrata in `examples.jsonl` (active learning). L'entry esce dalla coda. |
| `reject` | Stampa `REJECT <file>`. Nessun ingest. L'entry esce dalla coda. |
| `null` (o assente o stringa vuota) | L'entry **resta in coda** — utile per decidere solo alcune entry alla volta. Contatore `In attesa`. |
| Valore non valido (es. `L3`, `maybe`) | Warning a stderr, entry lasciata in coda. |

Alla fine `--confirm` stampa un riepilogo `Ingestati / Rifiutati / In attesa` e il consumo token. La coda è **deduplicata** su `(file, domain)`: ri-classificare lo stesso file sovrascrive la proposta precedente, non crea un duplicato.

**Differenza pratica tra le due porte d'ingresso alla coda**: il bulk ingest da manifest tipicamente vi accoda blocchi di documenti L2 ad alta confidence (per il gate asimmetrico tutti gli L2 passano sempre per la coda), mentre `classify.py --file --enqueue` è utile per accodare singoli documenti in modo iterativo, fuori da un batch.

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
- `build:llm_triples`             — estrazione triple tipate per il graph layer (solo `build_graph.py --llm-relations`)

## Graph layer — indice strutturale sperimentale (Arch B)

Esperimento architetturale che mette a confronto due modi di fornire il **contesto strutturale** a query time, mantenendo invariata la priorità del progetto: **la revisione umana del wiki**. Le pagine MD non vengono mai eliminate — cambia solo *come* il loro contenuto raggiunge il modello.

| | **Arch A** (default, `ask.py`) | **Arch B** (sperimentale, `query_graph.py`) |
|---|---|---|
| Indice raw | ChromaDB `raw_chunks` | ChromaDB `raw_chunks` (identico) |
| Contesto strutturale | ChromaDB `wiki_pages` (embedding delle pagine wiki) | **Grafo Kuzu** (`get_subgraph()`): sottografo entità + relazioni |
| Pagine MD su disco | Sì (sorgente dell'embedding wiki) | Sì (**solo** revisione umana, **non** embeddate) |
| Costo embedding wiki | Pagato a ogni ingest L1/L2 | **Zero** (le entity page non entrano in ChromaDB) |
| Contesto a query time | raw hits + wiki hits | raw hits + sottografo entità (JSON compatto) |

L'idea: il grafo sostituisce il *ruolo* dell'indice vettoriale wiki come fonte di contesto strutturale. Le query pescano raw chunks (ChromaDB) + sottografo (Kuzu) invece di raw + wiki embeds. Le pagine wiki MD restano come artefatto leggibile, senza costo di embedding.

Stack: **Kuzu** (graph DB embedded, sintassi Cypher-compatibile, nessun server, persistenza su file in `data/graph/kuzu_db/`). Zero token a query time: la traversal del grafo è puramente strutturale.

### Costruzione del grafo (`build_graph.py`)

Due fasi, la prima a costo zero, la seconda opzionale:

```powershell
python scripts/build_graph.py                          # zero token: solo nodi + CO_MENZIONATO
python scripts/build_graph.py --rebuild                # wipe e ricostruzione completa
python scripts/build_graph.py --rebuild --llm-relations  # + triple tipate via LLM (~18k token one-time)
```

- **FASE 1 — Nodi** (zero token): un nodo `Entity` per ogni entry dell'`_entity_index.yaml`, con `label`, `subtype`, `domain`, `state` e uno `snippet` (primi 400 char del body della pagina più rilevante — entity page se consolidated, altrimenti prima source page).
- **FASE 2 — Archi co-menzione** (zero token): per ogni `doc_id` condiviso da due o più entità si crea un arco bidirezionale `CO_MENZIONATO`. È la struttura "gratis" derivata dalle `sources` dell'indice.
- **FASE 3 — Triple tipate** (opzionale, `--llm-relations`): per ogni entità, una chiamata LLM legge il body wiki ed estrae triple `(soggetto, relazione, oggetto)` tipate, scegliendo i soggetti/oggetti **solo** dalla whitelist degli `entity_id` noti (anti-allucinazione). Tipi ammessi: `CONOSCE, E_UN, SI_TROVA_IN, PARTE_DI, OPPONE, ALLEATO_DI, POSSIEDE, CREA, GOVERNA, DISTRUGGE, MEMBRO_DI`. Le triple invalide (entità fuori whitelist, self-loop, type vuoto) vengono scartate in parsing.

**Stato del grafo sul pilot a 3 corpora** (tolkien + asimov + rowling, build con `--llm-relations`):

```
Nodi Entity        : 62
Archi CO_MENZIONATO: 230
Archi tipati LLM   : 123   (PARTE_DI 35 · SI_TROVA_IN 33 · CREA 16 ·
                            POSSIEDE/GOVERNA/OPPONE/CONOSCE 8 ciascuno ·
                            ALLEATO_DI 3 · MEMBRO_DI 2)
Archi totali       : 353
```

Nota di qualità: l'estrazione LLM produce occasionalmente un tipo malformato fuori dall'elenco (osservati 2 archi `ALLESATO_DI`, refuso di `ALLEATO_DI`). È rumore tollerato — non rompe la traversal — ma indica che un filtro `type ∈ whitelist` lato build sarebbe un irrobustimento utile.

### Pipeline di query a grafo (`query_graph.py`)

`QueryPipelineGraph.ask()` riusa l'intera infrastruttura di `ask.py` (Hot Layer, `CONFLICT_RULES`, policy multi-corpus, parser tollerante, token tracking, audit log) e cambia **solo** la fonte del contesto strutturale:

1. **Embedding domanda + retrieval raw**: identici ad Arch A (la collection `wiki_pages` non viene mai toccata).
2. **Rilevamento entità** (zero token LLM): unione di (a) entità le cui `sources` compaiono nei `doc_id` dei raw hits, e (b) entità il cui id leggibile (`snake_case` → parole) è citato nel testo della domanda.
3. **Traversal del grafo**: `get_subgraph(entity_ids, hops=1)` espande ai soli vicini diretti. Il sottografo viene troncato a **12 nodi** e ogni nodo porta uno **snippet di 100 char** nel prompt. Le relazioni `CO_MENZIONATO` bidirezionali sono deduplicate.
4. **Iniezione**: il sottografo è serializzato come **JSON compatto** (`focus / nodes / relations`) e inserito nel prompt al posto dei wiki hits.
5. **Whitelist citazioni**: `entity_id` del sottografo + `doc_id` raw recuperati. **Le regole di citazione sono vincolanti e rafforzate**: ogni entità del grafo nominata nella risposta DEVE portare `[[entity_id]]` (id esatto in snake_case, non il label), e deve comparire in `wiki_sources`.

Parametri (in cima a `query_graph.py`): `_MAX_SUBGRAPH_NODES = 12`, `_SUBGRAPH_HOPS = 1`, `_SNIPPET_IN_PROMPT = 100`. Tarati per non saturare il contesto: un primo giro con `hops=2`/`snippet=300` rendeva Arch B **più** costoso di Arch A; riducendo a `hops=1`/`snippet=100` il sottografo torna selettivo e il prompt più corto.

> **Lezione di prompt (fix citazioni).** Nella prima versione, la regola di citazione di Arch B era troppo "morbida": su una domanda relazionale (`ac01`, il ruolo di Frodo) il modello produceva una risposta accurata e completa **senza alcun** `[[entity_id]]` — `citation_quality = 0` al judge, contro 5 di Arch A. La regola è stata resa **obbligatoria ed esplicita** ("ogni entità del grafo nominata → wikilink alla prima menzione; usa l'id esatto; rileggi la risposta prima del JSON e popola `wiki_sources`"). Post-fix: `ac01` passa a `citation_quality = 5` con 12 entità citate, e il giudizio si ribalta da *winner A* a *winner B*.

### Confronto A vs B (`eval_compare.py`)

Esegue **entrambe** le pipeline sulle stesse domande, misura i token per domanda (snapshot dei record del TokenTracker, fase `query:llm_synthesis`) e, con `--judge`, fa valutare le due risposte da un LLM-giudice (accuracy / completeness / citation_quality 0-5 + winner).

```powershell
python scripts/eval_compare.py --limit 4 --judge                              # prime 4 domande (tolkien/asimov) + giudizio
python scripts/eval_compare.py --eval-set tests/eval_set_hp_judge.yaml --judge  # eval HP-only (ac08, ac09)
python scripts/eval_compare.py --domain tolkien                                # filtro dominio, senza giudizio
```

Per vincoli di lock di Kuzu, le due pipeline sono inizializzate **una sola volta** fuori dal loop e `pipe_b.close()` rilascia il file lock a fine run. L'output va in `tests/results/eval_compare_YYYYMMDD_HHMMSS.json`.

### Risultati empirici

Misure sul pilot a 3 corpora (`gpt-5.1`; il delta token oscilla tra run perché la lunghezza dell'output non è deterministica):

**Corpora con wiki densa (tolkien/asimov)** — Arch B **risparmia token** e regge la qualità:

```
Run                         A tokens   B tokens     d%      Judge (A·tie·B)
─────────────────────────────────────────────────────────────────────────
pre-fix  (4 domande)          46.559    43.070    -7.5%    1 · 2 · 1  (ac01 a A per cit=0)
post-fix (4 domande)          47.547    46.170    -2.9%    1 · 1 · 2  (ac01 ribaltata a B)
```

**Corpus con wiki sparsa (harry_potter, `ac08`/`ac09`) — caso istruttivo, prima un artefatto di test, poi il dato pulito.**

Una prima esecuzione sembrava mostrare Arch B vincente (B 2x) ma **costoso** (+30.5%). Indagando si è scoperto che il risultato era **contaminato da un bug dell'eval set**: in `eval_set_hp_judge.yaml` le due domande erano taggate `domain: harry_potter`, mentre il corpus HP è interamente taggato `domain: rowling`. Arch A applica un **filtro ChromaDB di uguaglianza stretta** sul dominio (`{"domain": "harry_potter"}`), che non matchava **nessuno** dei 10 vettori wiki / 31 chunk raw del corpus rowling → retrieval **vuoto** → `citation_quality = 0`, confidence `low`. Arch B sopravviveva perché il suo seeding del sottografo avviene **per nome di entità** (`harry_potter`, `lord_voldemort`, `horcrux` esistono come nodi a prescindere dal tag dominio), non per filtro metadati.

Ri-eseguendo con il dominio **corretto** (`--domain rowling`):

```
            A tokens   B tokens     d%       Judge
──────────────────────────────────────────────────────
  ac08       12.637    12.688     +0.4%    winner A
  ac09       12.017    12.126     +0.9%    winner A
──────────────────────────────────────────────────────
  TOTAL      24.654    24.814     +0.6%    A vince 2x
```

Con il filtro corretto Arch A cita pulito (4 page wiki + 3 raw, confidence `high`) e **vince entrambe** le domande sulla completezza (judge: A=5, B=4 in entrambe). Il meccanismo delle entità **aliased** (§13) — che permette di citare `[[entity_id]]` aliased sintetizzandole runtime da source page + raw chunk — gestisce benissimo il corpus sparso: la collection `wiki_pages` di rowling **non è vuota** (contiene 10 source page), e A le recupera correttamente.

**Due lezioni, distinte:**
1. *Igiene dei metadati.* Il filtro dominio di Arch A è un **single point of failure silenzioso**: un tag disallineato azzera il retrieval senza errore visibile. A scala (molti corpus, ingestion multi-fonte) serve validazione dei tag dominio e/o un fallback quando il retrieval filtrato torna vuoto.
2. *Robustezza del seeding.* Il seeding per nome di Arch B è **robusto al drift dei metadati** in un modo che il filtro di A non è — un vantaggio architetturale reale, ma diverso da "il grafo è migliore sulle wiki sparse" (affermazione che il dato pulito **non** supporta).

### Trade-off — quando conviene Arch B

- **Pari o leggermente meglio** su corpora con wiki densa e distribuzione *long-tail* delle sources: il sottografo è più selettivo dell'embedding wiki, il prompt si accorcia, la qualità tiene (B ≥ A al judge su tolkien/asimov dopo il fix citazioni).
- **Robustezza ed esplicabilità**, non risparmio: il seeding per nome resiste al drift dei metadati, e le triple tipate rendono le relazioni **ispezionabili** (valore per audit/compliance). Sui token il confronto è sostanzialmente **pari** (delta tra -2.9% e +0.9%, dentro il rumore): il risparmio token **non** è il discriminante.
- **Invariante rispettato**: in ogni scenario le pagine MD restano su disco per la revisione umana — Arch B rimuove **solo** il costo di embedding wiki, non l'artefatto leggibile.

## Struttura

```
hybrid-wiki/
├── src/                       # moduli core (ingest, query, lint, classifier, stores, ...)
│   ├── query.py               # pipeline Arch A (raw + wiki ChromaDB)
│   ├── query_graph.py         # pipeline Arch B sperimentale (raw + grafo Kuzu)
│   └── graph_store.py         # wrapper Kuzu: nodi Entity + archi Relation
├── data/
│   ├── raw/incoming/          # documenti sorgente per dominio (tolkien/, asimov/) + manifest
│   ├── raw/                   # documenti originali ingestati (immutabili)
│   ├── wiki/                  # pagine sintetizzate + HOT_LAYER.md
│   ├── vectors/               # ChromaDB (creato a runtime)
│   ├── graph/kuzu_db/         # graph DB Kuzu (Arch B, creato da build_graph.py)
│   ├── lint/                  # consolidation_report.yaml + applied_merges.jsonl (audit)
│   ├── classification/        # review_queue.yaml + examples.jsonl (active learning) + rules.yaml
│   └── *_log.jsonl            # query_log, token_log (audit append-only)
├── schema/AGENTS.md           # contratto operativo dominio-agnostico (v0.2)
├── scripts/                   # CLI: ingest_doc, ingest_folder, ask, classify, lint, tokens,
│                              #      build_graph (costruzione grafo), eval_compare (A vs B)
└── tests/
    ├── eval_set.yaml          # eval dominio singolo (tolkien)
    ├── eval_set_crossdomain.yaml
    ├── eval_set_arch_compare.yaml   # 10 domande tolkien/asimov/HP/cross per il confronto A vs B
    ├── eval_set_hp_judge.yaml       # ac08/ac09 (harry_potter) per il judge Arch B
    └── results/               # report per-run: evalset_results_*.txt + eval_compare_*.json
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

**Completati**: Walking Skeleton · correzioni dal pilot (design §11) · fase **Scaling** (design §12: inventario gerarchico, lint di consolidazione, classificazione assistita, promozione retroattiva) · lazy materialization delle entity page (design §13).

**Prototipo sperimentale**: **graph layer / Arch B** (design §14) — grafo strutturale Kuzu come alternativa all'embedding wiki, con harness di confronto A vs B e LLM-as-judge. Implementa il *grafo dei collegamenti* previsto in §4.1/§5.2 ma finora mai materializzato.

**Non ancora implementati** (design §7 e §10): access control multi-utente, sincronizzazione batch/near-real-time, synthesis pages persistenti, eval framework con scoring automatico, multimodal, ottimizzazione costi.

Riferimento completo: `../hybrid-wiki-rag-design.md` (v3.1; §§11–12 correzioni pilot/Scaling, §13 lazy materialization, §14 graph layer Arch B).
