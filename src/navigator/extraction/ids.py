"""Deterministic team_rule_id generation.

    team_rule_id = "r-" + sha256(canonical [source_doc_id, category,
                                            citation, verified quoted_span])[:10 hex]

Design notes:
- The key uses the *verified source span*, not the model's paraphrased
  `requirement`, because the span is anchored in the source and is far more
  stable across reruns than generated prose.
- Citation and span are canonicalized (NFC, whitespace collapsed, casefolded),
  so cosmetic differences do not change the id.
- 10 hex chars = 40 bits. For n rules the collision probability is about
  n^2 / 2^41 (about 5e-7 at n = 1,000). Identical inputs give identical ids by
  design; that is how duplicates are detected. Any id collision across
  *different* inputs is detected at build time, not silently accepted.
- These ids are ours. They are NOT the organizer ids used in dev/change_tests.json.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

ID_PREFIX = "r-"
ID_HEX_CHARS = 10


def _canon(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def make_team_rule_id(*, source_doc_id: str, category: str, citation: str, quoted_span: str) -> str:
    payload = json.dumps([source_doc_id, category, _canon(citation), _canon(quoted_span)],
                         ensure_ascii=False, separators=(",", ":"))
    return ID_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:ID_HEX_CHARS]
