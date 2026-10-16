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
CACHE_DIR = DATA_DIR / "cache"  # LLM responses, keyed by input hash
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "boreholes.sqlite"))

# Models: Claude is approved for the challenge data. Classification runs on a small
# thumbnail at low effort; extraction gets the full page at higher effort.
CLASSIFY_MODEL = os.getenv("CLASSIFY_MODEL", "claude-opus-5-5")
CLASSIFY_EFFORT = os.getenv("CLASSIFY_EFFORT", "low")
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "claude-opus-5-5")
EXTRACT_EFFORT = os.getenv("EXTRACT_EFFORT", "medium")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "6"))

# Rendering: long edge in pixels. Logs are dense, so extraction uses a large image.
EXTRACT_LONG_EDGE = int(os.getenv("EXTRACT_LONG_EDGE", "2000"))
THUMB_LONG_EDGE = int(os.getenv("THUMB_LONG_EDGE", "900"))

# Human in the loop: records with a release score at or above this value and no blocking
# findings are released automatically; all others go to the review queue.
AUTO_APPROVE_THRESHOLD = float(os.getenv("AUTO_APPROVE_THRESHOLD", "0.9"))

# Bump when prompts or extraction models change so cached responses are not reused.
PROMPT_VERSION = "v1"
