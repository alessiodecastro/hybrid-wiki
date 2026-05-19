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
# Layout su filesystem. Tutto sotto data/ è transitorio e ricostruibile a
# partire da data/raw/incoming/ + scripts/ingest_doc.py.
# ----------------------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"                  # documenti raw immutabili (uno .md per ingest)
INCOMING_DIR = RAW_DIR / "incoming"         # documenti sorgente prima dell'ingest (txt/md)
WIKI_DIR = DATA_DIR / "wiki"                # pagine wiki generate (entity + source + Hot Layer)
VECTORS_DIR = DATA_DIR / "vectors"          # store ChromaDB persistente
HOT_LAYER_PATH = WIKI_DIR / "HOT_LAYER.md"  # singolo file: overview + index
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
# Wiki ha pagine "dense" (sintesi), 4 sono di norma sufficienti per orientare
# l'LLM. Raw ha chunk frammentati, k più alto aumenta la probabilità di
# pescare la frase precisa che risolve la domanda.
# ----------------------------------------------------------------------------
WIKI_TOP_K = 4
RAW_TOP_K = 6

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
for d in (RAW_DIR, INCOMING_DIR, WIKI_DIR, VECTORS_DIR, SCHEMA_DIR, LINT_DIR, CLASSIFICATION_DIR):
    d.mkdir(parents=True, exist_ok=True)
