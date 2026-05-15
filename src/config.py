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

# Nomi delle due collection ChromaDB. Sono il "doppio indice" del design.
RAW_COLLECTION = "raw_chunks"
WIKI_COLLECTION = "wiki_pages"

# Whitelist dei valori ammessi per metadata e CLI. Tenere allineati con
# schema/AGENTS.md (sezione "Tipi di entità ammessi" e "Criteri L0/L1/L2").
VALID_LEVELS = {"L0", "L1", "L2"}
VALID_SUBTYPES = {"character", "place", "artifact", "event", "book"}

# Crea le directory necessarie all'import. Idempotente: exist_ok=True evita
# errori se il filesystem è già popolato (caso comune in pilot).
for d in (RAW_DIR, INCOMING_DIR, WIKI_DIR, VECTORS_DIR, SCHEMA_DIR):
    d.mkdir(parents=True, exist_ok=True)
