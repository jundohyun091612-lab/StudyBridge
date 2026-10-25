"""
config.py
- Holds all file paths and settings in one place.
- Reads the OpenAI key from OpenAI_Key.txt (same folder as the project root).
- Defines which AI model each task uses (easy to change in one spot).
"""
import os

# ---- Folder layout -----------------------------------------------------------
# This file lives in <project>/app/config.py, so the project root is one level up.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)

TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
STATIC_DIR = os.path.join(APP_DIR, "static")
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
DATA_DIR = os.path.join(ROOT_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

DB_PATH = os.path.join(DATA_DIR, "studybridge.db")
KEY_FILE = os.path.join(ROOT_DIR, "OpenAI_Key.txt")

for d in (LOGS_DIR, DATA_DIR, UPLOAD_DIR):
    os.makedirs(d, exist_ok=True)

# ---- Server ------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8080

# ---- AI models ---------------------------------------------------------------
# If you ever want cheaper or stronger models, change these strings only.
# Tiering keeps cost low: the heavy/bulk work (compressing long material) runs on the
# cheap mini model; the study sheet runs on mini too (the depth comes from the prompt,
# not the model). Bump MODEL_SUMMARY to "gpt-4o" if you want maximum sheet quality.
MODEL_SUMMARY = "gpt-4o-mini"   # study sheet generation (input is small/compressed, so cheap)
MODEL_COMPRESS = "gpt-4o-mini"  # STAGE 1: compress long chapter material into key notes
MODEL_PLAN = "gpt-4o-mini"      # study plan
MODEL_SYLLABUS = "gpt-4o"       # syllabus auto-generation (one call; quality matters)
MODEL_MATCH = "gpt-4o-mini"     # matching material -> chapter (cheap, structured)
MODEL_VISION = "gpt-4o-mini"    # reading text out of images / scanned pages
MODEL_QA = "gpt-4o-mini"        # follow-up questions on the study screen

# ---- Cost-control / compression knobs ---------------------------------------
# STAGE 1 (map): split a chapter's material into chunks of this many characters and
# summarize each into key notes. STAGE 2 (sheet) then reads only the compressed notes,
# so the sheet-generation input is fixed no matter how big the source material is.
COMPRESS_CHUNK_CHARS = 40000      # ~10k tokens per compression call (fewer calls = much faster)
COMPRESS_NOTES_PER_CHUNK = 1500   # target chars of notes kept per chunk
COMPRESS_MAX_NOTES_CHARS = 7000   # hard cap on combined notes fed to stage 2 (~1.7k tokens)
COMPRESS_TRIGGER_CHARS = 9000     # if material is shorter than this, skip compression
COMPRESS_PARALLEL = 5             # compress this many chunks at once (speed, no content lost)

# ---- Cache -------------------------------------------------------------------
# Cache key = hash(chapter material + compressed notes + PROMPT_VERSION + model).
# PROMPT_VERSION is derived automatically from the source code of the generation
# functions (see ai.prompt_version()), so changing the prompt/logic auto-invalidates
# the cache — you never get a stale sheet from old code. Set env CACHE=off while
# developing to bypass the cache entirely.
CACHE_ENABLED = os.environ.get("CACHE", "on").lower() not in ("off", "0", "false", "no")

# ---- Upload handling ---------------------------------------------------------
# We do NOT cap file size — class materials (full textbook PDFs, big slide decks)
# are legitimately large. Instead we process big files safely: extract PDFs page by
# page, and only downscale oversized IMAGES before OCR (content is preserved; this
# just keeps Tesseract fast). Below: the pixel ceiling above which an image is
# resized down for OCR.
MAX_IMAGE_PIXELS = 24_000_000   # ~24 MP; bigger images are downscaled before OCR only

# Files are never blocked by size, but anything over this gets a visible warning
# (it still gets processed — it just may be slow).
WARN_FILE_BYTES = 1024 * 1024 * 1024   # 1 GB


def load_openai_key() -> str:
    """
    Read the API key from OpenAI_Key.txt.
    Returns "" if the file is missing or empty so the caller can show a clear message.
    """
    try:
        with open(KEY_FILE, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
