"""Read-only loaders for the official starter pack.

Every loader returns data exactly as supplied: CSV fields stay strings (ZIPs
such as "07030" and use codes such as "0500" must keep their leading zeros),
and missing values stay empty strings. Interpretation belongs to later stages.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Paths relative to the starter-pack root (README section 4).
SCHEMA_PATH = Path("schema/rule_record.schema.json")
SAMPLE_RULE_PATH = Path("schema/sample_rule_record.json")
MANIFEST_PATH = Path("corpus/corpus_manifest.csv")
LINKS_ONLY_PATH = Path("corpus/links_only.csv")
CORPUS_DIR = Path("corpus")
TEXT_DIR = Path("corpus/text")
ADDRESSES_PATH = Path("data/sample_addresses.csv")
CHANGE_TESTS_PATH = Path("dev/change_tests.json")
TEMPLATES_DIR = Path("submission_templates")

# README section 4.1.
ADDRESS_COLUMNS = (
    "address_id", "street_address", "postal_city", "state", "zip", "year_built",
    "units", "use_code", "use_description", "source_dataset", "retrieved_at",
)
MANIFEST_COLUMNS = (
    "doc_id", "jurisdictions", "url", "source_type", "capture", "retrieved_at",
    "sha256", "text_file", "status",
)
LINKS_ONLY_COLUMNS = ("doc_id", "jurisdictions", "url", "source_type")

# README section 5, "result values". NOT_APPLICABLE is deliberately absent:
# rules that don't apply are omitted from lookups.json.
LOOKUP_RESULT_VALUES = ("applies", "unknown", "superseded", "not_yet_effective", "pending")

# How a manifest row's text is (or is not) available locally.
SUPPLIED_TEXT = "supplied_text"    # text_file present
LINK_ONLY = "link_only"            # intentionally not captured (secondary / check-terms)
CAPTURE_FAILED = "capture_failed"  # capture attempted, status records the error


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Return (header, rows) with every value kept as a raw string."""
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def classify_source(row: dict[str, str]) -> str:
    if row.get("text_file"):
        return SUPPLIED_TEXT
    if row.get("status", "").startswith("manual"):
        return CAPTURE_FAILED
    return LINK_ONLY


@dataclass(frozen=True)
class CorpusText:
    """A corpus/text file split into its provenance header and body."""

    source_url: str
    retrieved: str  # as written in the header, e.g. "2026-10-01 22:44 UTC"
    body: str


def parse_corpus_text(text: str) -> CorpusText:
    """Split a supplied text file into header fields and body.

    Files start with "SOURCE: <url>", "RETRIEVED: <timestamp>", then a blank
    line. Raises ValueError if that header is absent.
    """
    lines = text.split("\n", 3)
    if len(lines) < 3 or not lines[0].startswith("SOURCE: ") or not lines[1].startswith("RETRIEVED: "):
        raise ValueError("missing SOURCE/RETRIEVED header")
    if lines[2].strip():
        raise ValueError("header not followed by a blank line")
    return CorpusText(
        source_url=lines[0].removeprefix("SOURCE: ").strip(),
        retrieved=lines[1].removeprefix("RETRIEVED: ").strip(),
        body=lines[3] if len(lines) > 3 else "",
    )


def header_timestamp_to_manifest(retrieved: str) -> str:
    """'2026-10-01 22:44 UTC' -> '2026-10-01T22:44Z' (manifest format)."""
    return retrieved.removesuffix(" UTC").replace(" ", "T", 1) + "Z"
