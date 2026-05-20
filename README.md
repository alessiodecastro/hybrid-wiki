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

L'ingest è il processo con cui un documento entra nel sistema. Ogni documento è elaborato su uno di **tre livelli** a costo/valore crescente (design §6.1). I tre livelli sono **incrementali**: L1 include tutto ciò che fa L0, L2 include tutto ciò che fa L1.

| Livello | File su disco prodotti | Vettori ChromaDB | Hot Layer | Quando usarlo |
|---|---|---|---|---|
| **L0** | `data/raw/<doc_id>.md` (copia immutabile con frontmatter). Nessun file in `data/wiki/`. | `raw_chunks`: N chunk da ~200 parole con overlap 40, embeddati uno per uno (`ingest:l0:raw_index`). | Non aggiornato (il doc non compare nell'index). | Alto volume, basso valore individuale (avvisi, log, note di servizio, comunicazioni operative). Recuperabile **solo** via ricerca raw — non emerge nelle query orientative basate sul Hot Layer. |
| **L1** | L0 + `data/wiki/source_<doc_id>.md` (pagina *source*: sintesi autonoma del singolo documento, sezioni `## Overview / ## Dettagli / ## Citazioni notevoli`). | L0 + `wiki_pages`: 1 vettore per la pagina source (`ingest:l1:source_page` + `ingest:l2:wiki_index` per l'embedding). | Aggiornato a fine batch: la source page compare nell'index del Hot Layer. | Documento che merita una sintesi propria ma **non** va integrato col resto della wiki (es. atto puntuale, articolo singolo, scheda non riusabile). |
| **L2** | L1 + N file `data/wiki/<entity_id>.md` (pagine *entity*) creati o aggiornati — uno per ogni entità sostanziale estratta dal documento; può anche modificare entità già esistenti (merge col contenuto preesistente, con sezione `## Contraddizioni note` se ci sono divergenze). | L1 + 1 vettore per ogni pagina entity creata o aggiornata. Le entity pages già esistenti vengono re-embeddate dopo il merge. | Aggiornato a fine batch: tutte le nuove entity pages entrano nell'index del Hot Layer. | Documento strategico (biografie complete, eventi cardine, concetti centrali): cambia il quadro generale del corpus e va integrato. |

**Lettura della tabella**: una riga di ingest L2 può quindi produrre, in un solo passaggio: 1 file raw immutabile + 1 source page + da 1 a ~10 entity pages (nuove o aggiornate) + altrettanti vettori. Il riepilogo a fine ingest mostra `wiki=[source_..., entity_id_1, entity_id_2, ...]`.

Il documento raw è **immutabile**: una volta ingestato non viene mai riscritto. Unica eccezione: il metadato `level` in caso di **promozione retroattiva** (un doc L0 diventato strategico viene rieseguito agli step L1/L2 senza duplicare il raw — vedi *Classificazione assistita*).

#### Pagina *source* vs pagina *entity*

I file in `data/wiki/` sono di **due tipi** (campo `type` nel frontmatter), con cicli di vita radicalmente diversi:

| | **Source page** (`type: source`) | **Entity page** (`type: entity`) |
|---|---|---|
| **Identità** | "vista sul documento": fissa la prospettiva di **un singolo raw** | "vista sull'entità del mondo": rappresenta una cosa (personaggio, luogo, evento) **trasversale ai documenti** |
| **Cardinalità sorgenti** | 1:1 — sempre **una sola** source (il doc da cui è stata generata) | 1:N — lista cumulativa, deduplicata, di tutti i raw che hanno contribuito |
| **Mutabilità** | **Append-only / immutabile**. Una volta scritta non viene mai più modificata. | **Mergeable**. Ogni nuovo doc che la menziona sostanzialmente la raffina, arricchisce o contraddice (sezione `## Contraddizioni note`). |
| **Fedeltà** | Fedele al singolo doc anche se in conflitto con altri (se il raw dice "1604", la source dice "1604") | Aggrega le tensioni: se due raw divergono, l'entity **mantiene entrambe le versioni esplicitamente** |
| **id** | `source_<doc_id>` (con timestamp del doc) | `<entity_id>` (slug inglese snake_case stabile, es. `frodo_baggins`, `one_ring`) |
| **Domain** | Sempre del singolo doc | Diventa `_mixed` se le sources sono di domini diversi |
| **Struttura** | `## Overview / ## Dettagli / ## Citazioni notevoli` | `## Panoramica / ## Dettagli / ## Relazioni / ## Domande aperte` (+ `## Contraddizioni note` opzionale) |
| **Esiste dal livello** | L1 in su | Solo L2 |

**Come si declina nell'ingest L2** (`src/ingest.py`):

```
L0 → indicizzazione raw (chunk + embeddings)
L1 → L0 + _make_source_page()       # sempre CREATE, mai merge (doc_id unico)
L2 → L1 + _integrate_entities():
       ├─ _create_entity_page()     # entità nuova → 1 chiamata LLM, contesto = solo nuovo doc
       └─ _merge_entity_page()      # entità esistente → 1 chiamata LLM, contesto = pagina + nuovo doc
```

La source ha **un solo flusso**: `create`, mai `merge`. Lo stesso file ingestato due volte (es. `--force`) produce **due** source page con `doc_id` distinti, mai una fusione. Il prompt LLM vede solo il nuovo doc e ne produce una sintesi auto-contenuta.

L'entity ha **due flussi**, scelti runtime in base a `WikiStore.exists(page_id)`. Il merge è l'unico punto del sistema dove un prompt LLM riceve **due input concorrenti** (pagina esistente + nuovo doc) con regole esplicite: preservare le info ancora valide, aggiungere le nuove, e — se c'è divergenza — esplicitare il conflitto in `## Contraddizioni note`. Per questo è anche lo step più sensibile al content filter: combinare due contesti già "carichi" può superare le soglie di severity (vedi `data/content_filter_skips.jsonl` se compaiono skip granulari).

Conseguenze operative di questa distinzione:

| Aspetto | Source | Entity |
|---|---|---|
| Conflict resolution | Non opera (1 sola fonte per definizione) | Qui scattano `## Contraddizioni note` e le `CONFLICT_RULES` lato query |
| Lint consolidation (`--detect-duplicates`) | Non opera sulle source (immutabili 1:1) | Opera **solo** su entity (cluster di duplicati/alias, merge controllato) |
| Promozione retroattiva | Crea una nuova source (con `promoted_from`) | Può aggiornare entity esistenti tramite re-integration |
| Citazioni `[[id]]` in risposta | Tipicamente per dettagli puntuali, numeri, citazioni testuali | Tipicamente per concetti, sintesi, relazioni |

In una battuta: **la source è la memoria di "cosa ha detto questo documento", l'entity è la memoria di "cosa sappiamo su questa cosa"**. La prima fissa una prospettiva e non la rinnega mai; la seconda costruisce un consensus cumulativo gestendo esplicitamente le tensioni tra fonti.

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
