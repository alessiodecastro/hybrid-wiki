from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

LLM_MODEL = "claude-sonnet-4-6"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
INCOMING_DIR = RAW_DIR / "incoming"
WIKI_DIR = DATA_DIR / "wiki"
VECTORS_DIR = DATA_DIR / "vectors"
HOT_LAYER_PATH = WIKI_DIR / "HOT_LAYER.md"
QUERY_LOG_PATH = DATA_DIR / "query_log.jsonl"

SCHEMA_DIR = PROJECT_ROOT / "schema"
AGENTS_MD_PATH = SCHEMA_DIR / "AGENTS.md"

CHUNK_SIZE_WORDS = 400
CHUNK_OVERLAP_WORDS = 80

WIKI_TOP_K = 4
RAW_TOP_K = 4

RAW_COLLECTION = "raw_chunks"
WIKI_COLLECTION = "wiki_pages"

VALID_LEVELS = {"L0", "L1", "L2"}
VALID_SUBTYPES = {"character", "place", "artifact", "event", "book"}

for d in (RAW_DIR, INCOMING_DIR, WIKI_DIR, VECTORS_DIR, SCHEMA_DIR):
    d.mkdir(parents=True, exist_ok=True)
