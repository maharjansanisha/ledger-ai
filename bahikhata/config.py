"""Central settings for BahiKhata AI (ARCHITECTURE.md §3.15).

One place for paths, prompt versions, tolerances and limits, so no module
hides "magic numbers". Secrets are NOT stored here: they come from environment
variables (loaded from `.env`, which is gitignored).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# --- Paths -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load `.env` from the project root (if it exists) into os.environ.
# Existing environment variables win over values in the file.
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"          # saved receipt images (gitignored)
CACHE_DIR = DATA_DIR / "cache"            # LLM response cache (gitignored)
EVAL_DATA_DIR = DATA_DIR / "eval"         # dev/ and test/ images + labels
LEDGER_DB_PATH = DATA_DIR / "ledger.db"   # the real ledger (gitignored)
EVAL_DB_PATH = DATA_DIR / "eval_ledger.db"  # rebuilt from ground truth for query eval
PROMPTS_DIR = PROJECT_ROOT / "prompts"
EVAL_RESULTS_DIR = PROJECT_ROOT / "eval" / "results"

# --- LLM provider (ARCHITECTURE.md §3.4, §13) --------------------------------
# Exact Gemini model id, chosen in TASK-002 (see TASKS.md Spike Log).
# Override with GEMINI_MODEL in .env (e.g. to try another model) without code changes.
DEFAULT_MODEL_NAME = "gemini-3.5-flash-lite"
MODEL_NAME: str = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL_NAME
LLM_TIMEOUT_SECONDS = 60
LLM_RETRY_WAIT_SECONDS = 2               # one automatic retry on 5xx/timeout; none on 429

# Active prompt versions. A version is the prompt file name in prompts/.
# Never edit a version after it has been used in a test-split run; copy to _v2.
EXTRACTION_PROMPT = "extraction_v1"
SQL_PROMPT = "sql_v1"
EXPLANATION_PROMPT = "explain_v1"

# --- Image intake (ARCHITECTURE.md §3.2-3.3) --------------------------------
MAX_UPLOAD_BYTES = 10 * 1024 * 1024       # 10 MB
ALLOWED_IMAGE_FORMATS = ("JPEG", "PNG")   # Pillow format names
MAX_LONG_EDGE_PX = 2000                   # starting value; tune on dev set

# --- Money & validation (ARCHITECTURE.md §8, §10) ---------------------------
# All money is integer paisa (1 NPR = 100 paisa). No floats for money.
AMOUNT_TOLERANCE_PAISA = 100              # +/- NPR 1 for arithmetic checks
VAT_RATE_PERCENT = 13                     # Nepal VAT; integer to avoid floats
VAT_TOLERANCE_MIN_PAISA = 200             # V6: max(NPR 2, 1% of expected VAT)
VAT_TOLERANCE_PERCENT = 1

# --- Dates (ARCHITECTURE.md §8) ---------------------------------------------
# Plausible AD years (<= 2035) and BS years (>= 2070) do not overlap,
# which lets the normalizer tell BS from AD deterministically.
AD_YEAR_MAX = 2035
BS_YEAR_MIN = 2070
EARLIEST_PLAUSIBLE_DATE_BS = "2075-01-01"  # V8 lower bound

# --- Ask Your Ledger (ARCHITECTURE.md §5-6, §3.13; Amendment A1) -----------
QUERY_ALLOWED_TABLES = ("receipts", "line_items")
SQL_ROW_LIMIT = 200
MAX_QUESTION_CHARS = 500
READONLY_STATEMENT_TIMEOUT_MS = 5000  # Amendment A1: statement_timeout=5s
READONLY_ROLE = "ledger_reader"  # the only role Ask Your Ledger may connect as


def get_gemini_api_key() -> str:
    """Return the Gemini API key from the environment.

    Read lazily (not at import time) so tests and the UI can start without a key.
    Never print or log the returned value.
    """
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key == "your-gemini-api-key-here":
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return key


def get_database_url() -> str:
    """Return DATABASE_URL (Neon owner role, Amendment A1) from the environment.

    Read lazily, like the API key. Never print or log the returned value.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url or "<" in url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return url


def get_database_url_readonly() -> str:
    """Return DATABASE_URL_READONLY (Neon `ledger_reader` role, Amendment A1).

    Used only by "Ask Your Ledger" (`db.run_readonly_query`); every other read
    in the app uses the owner connection. Read lazily, like the API key.
    Never print or log the returned value.
    """
    url = os.getenv("DATABASE_URL_READONLY", "").strip()
    if not url or "<" in url:
        raise RuntimeError(
            "DATABASE_URL_READONLY is not set. See .env.example and ARCHITECTURE.md "
            "Amendment A1 / TASK-005: create the ledger_reader role in the Neon SQL editor, "
            "grant it SELECT on receipts and line_items, then add its connection string here."
        )
    from psycopg.conninfo import conninfo_to_dict  # local: keep config importable without psycopg

    try:
        user = conninfo_to_dict(url).get("user") or ""
    except Exception:  # malformed URL; never echo it (it holds the password)
        user = ""
    if user != READONLY_ROLE:
        raise RuntimeError(
            f"DATABASE_URL_READONLY must connect as {READONLY_ROLE!r}, not the owner or any other "
            "role: that role's SELECT-only grant on receipts and line_items is one of Ask Your "
            "Ledger's two independent locks (Amendment A1)."
        )
    return url


def has_gemini_api_key() -> bool:
    """True if a (non-placeholder) key is configured. Safe to show in the UI."""
    try:
        get_gemini_api_key()
        return True
    except RuntimeError:
        return False
