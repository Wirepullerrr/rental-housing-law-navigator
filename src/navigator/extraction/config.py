"""Extraction settings. Model choice lives here and nowhere else."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from navigator.starter_pack import REPO_ROOT

PROVIDER_GEMINI = "gemini"

# Current GA Gemini Flash model. There is no hidden fallback: if this model is
# unavailable the live run fails and says so. Override with --model.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"

# Generation settings sent to the provider and recorded in the cache key.
# Temperature is left at the model default (Google advises against lowering it
# for Gemini 3 models); reproducibility comes from the content-addressed cache,
# with a fixed seed as a best-effort extra.
GENERATION_SETTINGS: dict = {"temperature": None, "seed": 20261001}

# README section 1: default query date. Used only to derive `status`.
DEFAULT_AS_OF = date(2026, 10, 1)

API_KEY_ENV = "GEMINI_API_KEY"
CACHE_DIR = REPO_ROOT / "cache" / "extraction"
OUTPUT_DIR = REPO_ROOT / "outputs" / "m2"


def artifact_path(doc_id: str, out_dir: Path = OUTPUT_DIR) -> Path:
    return out_dir / f"{doc_id}_extraction.json"
