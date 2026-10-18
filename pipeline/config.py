"""Paths and model settings. Override via environment variables or a local `.env` file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Input documents (confidential, git-ignored) and all pipeline output (also git-ignored).
DATASET_DIR = Path(os.getenv("DATASET_DIR", ROOT / "temp/HKBoreholeChallenge_Data_BFH2026/BoreholeData"))
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
PAGES_DIR = DATA_DIR / "pages"  # rendered page images, served to the UI
CACHE_DIR = Path(os.getenv("CACHE_DIR", DATA_DIR / "cache"))  # LLM responses, keyed by input hash
UPLOADS_DIR = DATA_DIR / "uploads"  # documents submitted through the web UI, one subfolder per upload
GAZETTEER_PATH = Path(os.getenv("GAZETTEER_PATH", DATA_DIR / "gazetteer.sqlite"))  # GeoNames index, see pipeline/gazetteer.py
# A page cap keeps a single web upload's API cost bounded without asking for confirmation. The byte
# cap is checked first (while streaming to disk) so an oversized file is rejected before it is rendered.
MAX_UPLOAD_PAGES = int(os.getenv("MAX_UPLOAD_PAGES", "100"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_UPLOAD_FILES = int(os.getenv("MAX_UPLOAD_FILES", "1000"))  # documents per batch, zip entries included
MAX_UPLOAD_JOBS = int(os.getenv("MAX_UPLOAD_JOBS", "8"))
MAX_UPLOAD_WORKERS = int(os.getenv("MAX_UPLOAD_WORKERS", "1"))
MAX_UPLOAD_CALLS = int(os.getenv("MAX_UPLOAD_CALLS", "400"))  # model calls one upload batch may make
# Model calls per UTC day after which uploads are refused; command-line runs count, but are not stopped.
MAX_DAILY_CALLS = int(os.getenv("MAX_DAILY_CALLS", "2000"))
# PDF renderings of DOCX files, converted with LibreOffice, same relative path as in the dataset (uploads
# under uploads/<folder>/). DOCX logs are often laid out from several image strips in table cells; only a
# rendered page shows them together. A PDF placed here by hand is used instead of converting.
CONVERTED_DIR = DATA_DIR / "converted"
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "boreholes.sqlite"))
# Reviewed reference records, the release audit and prompt experiments; they hold extracted values.
EVAL_DIR = DATA_DIR / "eval"
REFERENCE_PATH = EVAL_DIR / "reference.json"
AUDIT_PATH = EVAL_DIR / "audit.json"
EXPERIMENTS_DIR = EVAL_DIR / "experiments"
# Committed aggregates of the evaluation, served where there is no data/ (the Docker image).
MODEL_EVALUATION_PATH = ROOT / "fixtures" / "model_evaluation.json"

# Models: Claude is approved for the challenge data. Classification runs on a small
# thumbnail at low effort; extraction gets the full page at higher effort.
CLASSIFY_MODEL = os.getenv("CLASSIFY_MODEL", "claude-opus-5-5")
CLASSIFY_EFFORT = os.getenv("CLASSIFY_EFFORT", "low")
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "claude-opus-5-5")
EXTRACT_EFFORT = os.getenv("EXTRACT_EFFORT", "medium")
# "api": Anthropic API with ANTHROPIC_API_KEY. "claude-code": run the local Claude Code CLI
# (`claude -p`) with its own login, e.g. for testing with a Claude subscription. Slower, and
# subject to the subscription's usage limits.
# "openai": any OpenAI-compatible chat API at OPENAI_BASE_URL (key in OPENAI_API_KEY), e.g. OpenRouter
# (https://openrouter.ai/api/v1) or a local Ollama server (the default). Model names are the server's.
LLM_BACKEND = os.getenv("LLM_BACKEND", "api")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
# OpenRouter only: route to providers with zero data retention (the dataset is confidential). Set 0 for
# a model that has no such provider.
OPENROUTER_ZDR = os.getenv("OPENROUTER_ZDR", "1") == "1"
CLAUDE_CLI = os.getenv("CLAUDE_CLI", "claude")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "3" if LLM_BACKEND == "claude-code" else "6"))

# Rendering: long edge in pixels. Logs are dense, so extraction uses a large image.
EXTRACT_LONG_EDGE = int(os.getenv("EXTRACT_LONG_EDGE", "2000"))
THUMB_LONG_EDGE = int(os.getenv("THUMB_LONG_EDGE", "900"))

# Human in the loop: records with a release score at or above this value and no blocking
# findings are released automatically; all others go to the review queue.
AUTO_APPROVE_THRESHOLD = float(os.getenv("AUTO_APPROVE_THRESHOLD", "0.9"))

# Bump when prompts or extraction models change so cached responses are not reused.
PROMPT_VERSION = "v4"
# Classification has its own prompt and cache, so an extraction prompt change does not re-classify pages.
CLASSIFY_PROMPT_VERSION = "v4"
# Lab-sheet extraction has its own prompt and cache, so it can change without re-extracting logs.
LAB_PROMPT_VERSION = "lab-v1"
COORDS_PROMPT_VERSION = "coords-v1"  # second reading of printed coordinates, see pipeline/coordcheck.py
COORDS_EFFORT = os.getenv("COORDS_EFFORT", "low")
