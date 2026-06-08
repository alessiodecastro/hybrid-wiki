"""
Configurazione centrale del walking skeleton.

Contiene tutte le costanti del sistema (path, modelli, parametri di chunking
e retrieval) in modo che i moduli core non contengano "magic values" e siano
agevolmente riconfigurabili per esperimenti o pilot diversi.

Carica anche le variabili d'ambiente da .env all'import: tutti gli altri
moduli possono assumere che AZURE_OPENAI_* siano disponibili in os.environ.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Risolto a tempo di import: la radice del progetto è la directory che
# contiene src/, data/, schema/, tests/, scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Carica .env dalla root. Idempotente: chiamare load_dotenv più volte non
# sovrascrive variabili già definite nell'ambiente del processo.
load_dotenv(PROJECT_ROOT / ".env")

# ----------------------------------------------------------------------------
# Azure OpenAI: nomi di deployment letti da .env per evitare hard-coding.
# Su Azure il "model" passato all'API è in realtà il deployment name, non il
# nome del modello base — è una convenzione di Azure OpenAI.
# ----------------------------------------------------------------------------
LLM_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.1")
EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
AZURE_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

# Dimensione vettore di text-embedding-3-small. Usata solo a scopo
# documentale: ChromaDB la infera dal primo vettore inserito.
EMBEDDING_DIM = 1536

# ----------------------------------------------------------------------------
# Backoff/retry per le chiamate di embedding.
# Il tier S0 di Azure ha un limite per-minuto basso su text-embedding-3-*:
# il burst di un singolo doc L2 ricco (raw chunks + source page + ogni entity
# page consolidata) può superarlo e tornare HTTP 429. Senza retry l'ingest del
# documento falliva a metà (osservato nello scaling test a 100 doc). Backoff
# esponenziale con jitter, troncato a un cap, che rispetta l'header Retry-After
# del server quando presente. Tutti override via env per i pilot.
EMBED_MAX_RETRIES = int(os.environ.get("EMBED_MAX_RETRIES", "6"))
EMBED_BACKOFF_BASE = float(os.environ.get("EMBED_BACKOFF_BASE", "1.0"))   # secondi
EMBED_BACKOFF_CAP = float(os.environ.get("EMBED_BACKOFF_CAP", "60.0"))    # secondi

# ----------------------------------------------------------------------------
# Layout su filesystem. Tutto sotto data/ è transitorio e ricostruibile a
# partire da data/raw/incoming/ + scripts/ingest_doc.py.
# ----------------------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"                  # documenti raw immutabili (uno .md per ingest)
INCOMING_DIR = RAW_DIR / "incoming"         # documenti sorgente prima dell'ingest (txt/md)
WIKI_DIR = DATA_DIR / "wiki"                # pagine wiki generate (entity + source + Hot Layer)
VECTORS_DIR = DATA_DIR / "vectors"          # store ChromaDB persistente
HOT_LAYER_PATH = WIKI_DIR / "HOT_LAYER.md"  # singolo file: overview + index
ENTITY_INDEX_PATH = WIKI_DIR / "_entity_index.yaml"  # indice centrale entità (§13)
GRAPH_DIR = DATA_DIR / "graph"               # Kuzu graph DB (Arch B: indice strutturale)
QUERY_LOG_PATH = DATA_DIR / "query_log.jsonl"  # audit trail append-only delle query
TOKEN_LOG_PATH = DATA_DIR / "token_log.jsonl"  # consumo token per fase, append-only

# Output persistente dei run dell'eval set (oltre alla console).
# Sotto tests/ (non data/): è materiale di valutazione, non stato runtime.
TESTS_DIR = PROJECT_ROOT / "tests"
EVAL_RESULTS_DIR = TESTS_DIR / "results"

# Classificazione L0/L1/L2 assistita (§6.1: l'LLM propone, l'umano conferma).
CLASSIFICATION_DIR = DATA_DIR / "classification"
# Dataset few-shot che cresce a ogni conferma umana (active learning,
# asset gemello dell'eval set §7.2).
CLASSIFICATION_EXAMPLES_PATH = CLASSIFICATION_DIR / "examples.jsonl"
# Coda delle proposte in attesa di triage umano.
CLASSIFICATION_QUEUE_PATH = CLASSIFICATION_DIR / "review_queue.yaml"
# Regole deterministiche opzionali (precedono l'LLM). Se il file non
# esiste, nessuna regola → si va sempre di proposta LLM.
CLASSIFICATION_RULES_PATH = CLASSIFICATION_DIR / "rules.yaml"
# Soglia: sopra questa confidence, una proposta L0/L1 può essere
# auto-accettata; L2 e confidence bassa vanno SEMPRE in coda (errore
# "verso il basso" L2→L0 è il rischio grave, §6.1). Usata dalla slice 2.
CLASSIFIER_AUTO_CONFIDENCE = os.environ.get("CLASSIFIER_AUTO_CONFIDENCE", "high")
# Quanti esempi few-shot iniettare al massimo nel prompt del classificatore.
CLASSIFIER_FEWSHOT_MAX = int(os.environ.get("CLASSIFIER_FEWSHOT_MAX", "12"))

# Lint pipeline / consolidazione duplicati (§6.3, §11.1).
LINT_DIR = DATA_DIR / "lint"
CONSOLIDATION_REPORT_PATH = LINT_DIR / "consolidation_report.yaml"
APPLIED_MERGES_PATH = LINT_DIR / "applied_merges.jsonl"
# Similarità COSENO minima (calcolata esplicitamente dai vettori salvati,
# NON dalla distanza dell'indice: ChromaDB usa L2 di default, semantica
# diversa). Sopra questa soglia due pagine entity sono candidate-duplicato.
# Conservativa ma non quanto credevamo: pagine su entità correlate stanno
# spesso a cos ~0.5-0.7; alias/sinonimi tipicamente >0.75. Override via env.
DUP_SIM_MIN_COSINE = float(os.environ.get("DUP_SIM_MIN_COSINE", "0.75"))
# Vicini da esaminare per ogni pagina nella fase di detection.
DUP_NEIGHBORS_K = int(os.environ.get("DUP_NEIGHBORS_K", "6"))
# Quante coppie più vicine stampare come diagnostica per calibrare la soglia.
DUP_DIAG_TOP = int(os.environ.get("DUP_DIAG_TOP", "15"))
# Cap deterministico sulle adjudication LLM per run: in un corpus narrativo
# il coseno è uniformemente alto (entità co-tematiche), quindi la sola
# soglia non limita il numero di coppie. Si adjudicano al massimo le prime
# N coppie per coseno decrescente sopra soglia; il resto è segnalato come
# "non valutato" nel report (l'umano può rilanciare con cap più alto).
DUP_MAX_ADJUDICATIONS = int(os.environ.get("DUP_MAX_ADJUDICATIONS", "40"))

SCHEMA_DIR = PROJECT_ROOT / "schema"
AGENTS_MD_PATH = SCHEMA_DIR / "AGENTS.md"   # contratto operativo letto in ogni chiamata LLM

# ----------------------------------------------------------------------------
# Chunking del raw layer.
# Trade-off: chunk grandi = più contesto per chunk ma menzioni di passaggio
# diluite (rischio di non emergere nella top-k). Chunk piccoli = miglior
# recall su dettagli marginali, ma più rumore.
# Il rapporto OVERLAP/SIZE = 0.2 garantisce che ogni parola finisca mediamente
# in ~1.25 chunk, evitando di tagliare frasi importanti senza esplodere il
# numero totale di token embeddati.
# ----------------------------------------------------------------------------
CHUNK_SIZE_WORDS = 200
CHUNK_OVERLAP_WORDS = 40

# ----------------------------------------------------------------------------
# Retrieval: top-k separati per indice wiki e raw.
# Wiki ha pagine "dense" (sintesi), il top-k va calibrato in base al regime
# di materializzazione. Con lazy merge (§13), molte entità centrali del
# corpus restano aliased e sono recuperabili SOLO attraverso le loro
# source page: alzare WIKI_TOP_K da 4 a 6 compensa pescando più source
# (con embedding più "stretto" sul singolo doc) accanto alle entity
# consolidated. Raw ha chunk frammentati, k più alto aumenta la probabilità
# di pescare la frase precisa che risolve la domanda.
# ----------------------------------------------------------------------------
WIKI_TOP_K = 6
RAW_TOP_K = 6

# Lazy materialization delle entity page (§13).
# Soglia di consolidamento: un'entità diventa pagina md materializzata solo
# quando raggiunge questo numero di sources cumulative. Sotto la soglia resta
# `aliased` (solo entry nell'indice, nessun file/vettore). Default 3.
# Soglia=1 equivale al merge eager classico (ogni entità è subito pagina).
ENTITY_CONSOLIDATION_THRESHOLD = int(os.environ.get("ENTITY_CONSOLIDATION_THRESHOLD", "3"))

# Inventario entità per _identify_entities (debito noto §11.1).
# Sotto il CAP: inventario piatto completo (comportamento storico,
# retro-compatibile). Sopra: modalità gerarchica = scheletro aggregato
# per subtype + shortlist semantica di K entità affini al documento.
# Override via env per poter forzare la modalità gerarchica in test
# anche su corpus piccoli (es. ENTITY_INVENTORY_CAP=20).
ENTITY_INVENTORY_CAP = int(os.environ.get("ENTITY_INVENTORY_CAP", "150"))
ENTITY_SHORTLIST_K = int(os.environ.get("ENTITY_SHORTLIST_K", "30"))

# Nomi delle due collection ChromaDB. Sono il "doppio indice" del design.
RAW_COLLECTION = "raw_chunks"
WIKI_COLLECTION = "wiki_pages"

# Whitelist dei valori ammessi per metadata e CLI. Tenere allineati con
# schema/AGENTS.md (sezione "Tipi di entità ammessi" e "Criteri L0/L1/L2").
VALID_LEVELS = {"L0", "L1", "L2"}
VALID_SUBTYPES = {"character", "place", "artifact", "event", "book"}

# Dominio di default per i documenti ingestati senza override esplicito.
# Tenuto come default e non come whitelist: i domini sono stringhe libere
# (es. "tolkien", "asimov", "work_notes") e si aggiungono nel manifest.
# Etichetta speciale per pagine wiki costruite da sorgenti di domini diversi.
DEFAULT_DOMAIN = "tolkien"
MIXED_DOMAIN = "_mixed"

# Crea le directory necessarie all'import. Idempotente: exist_ok=True evita
# errori se il filesystem è già popolato (caso comune in pilot).
for d in (RAW_DIR, INCOMING_DIR, WIKI_DIR, VECTORS_DIR, SCHEMA_DIR, LINT_DIR, CLASSIFICATION_DIR, GRAPH_DIR):
    d.mkdir(parents=True, exist_ok=True)
