"""Unit tests: citation check, ids, cache identity, status derivation, model contracts."""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import date

import pytest
from pydantic import ValidationError

from conftest import D052_DEPOSIT_SPAN, make_candidate
from navigator import starter_pack as sp
from navigator.extraction import extractor
from navigator.extraction.cache import CacheIntegrityError, ResponseCache, cache_key, sha256_hex
from navigator.extraction.citation import verify_span
from navigator.extraction.ids import make_team_rule_id
from navigator.extraction.models import ExtractedRule, RuleRecord, generation_json_schema
from navigator.extraction.normalize import derive_status, level_for

AS_OF = date(2026, 10, 1)


# ------------------------------------------------------------------ citation

def test_exact_span_offsets_point_at_source(d052):
    check = verify_span(D052_DEPOSIT_SPAN, d052.body)
    assert check.status == "exact_match" and check.occurrences == 1
    assert d052.body[check.start:check.end] == D052_DEPOSIT_SPAN


def test_line_break_and_space_runs_are_safe_normalization():
    source = "Section 1.\nNo lessor may\n  require a deposit  in excess of one month."
    check = verify_span("No lessor may require a deposit in excess of one month.", source)
    assert check.status == "normalized_match"
    assert check.source_span == "No lessor may\n  require a deposit  in excess of one month."
    assert source[check.start:check.end] == check.source_span


@pytest.mark.parametrize("span, expected", [
    ("No lessor may require a deposit in excess of two months.", "failed"),    # one word changed
    ("No lessor may  require a deposit in excess of one month.", "normalized_match"),
    ("No lessor may require a deposit in excess of one month", "exact_match"),  # a true substring
])
def test_only_exact_text_or_whitespace_differences_verify(span, expected):
    assert verify_span(span, "No lessor may require a deposit in excess of one month.").status == expected


@pytest.mark.parametrize("span", ["", "   ", "“No lessor” may require"])
def test_empty_or_retyped_quotes_fail(span):
    assert verify_span(span, '"No lessor" may require a deposit.').status == "failed"


# ----------------------------------------------------------------------- ids

ID_ARGS = dict(source_doc_id="D052", category="security_deposits", citation="M.G.L. c. 186, § 15B",
               quoted_span=D052_DEPOSIT_SPAN)


def test_team_rule_id_is_deterministic_and_canonical():
    rid = make_team_rule_id(**ID_ARGS)
    assert rid == make_team_rule_id(**ID_ARGS)
    assert rid == make_team_rule_id(**{**ID_ARGS, "citation": "  m.g.l. C. 186,  § 15B "})
    assert re.fullmatch(r"r-[0-9a-f]{10}", rid)


@pytest.mark.parametrize("field, value", [("source_doc_id", "D051"), ("category", "rent_increase_limits"),
                                          ("citation", "M.G.L. c. 186, § 15C"), ("quoted_span", "different text here")])
def test_team_rule_id_changes_with_identity_fields(field, value):
    assert make_team_rule_id(**ID_ARGS) != make_team_rule_id(**{**ID_ARGS, field: value})


# --------------------------------------------------------------------- cache

def test_cache_key_is_order_independent_and_stable():
    a = {"model": "m", "provider": "p", "settings": {"seed": 1, "temperature": None}}
    b = {"settings": {"temperature": None, "seed": 1}, "provider": "p", "model": "m"}
    assert cache_key(a) == cache_key(b) == cache_key(json.loads(json.dumps(a)))


def test_prepared_request_key_is_stable(d052):
    assert extractor.prepare_request(d052, "gemini", "m1").key == extractor.prepare_request(d052, "gemini", "m1").key


def test_key_changes_with_source_content(d052):
    body = d052.body.replace("first month's rent", "first two months' rent", 1)
    changed = dataclasses.replace(d052, body=body, meta=d052.meta.model_copy(update={"content_sha256": sha256_hex(body)}))
    assert extractor.prepare_request(d052, "gemini", "m1").key != extractor.prepare_request(changed, "gemini", "m1").key


def test_key_changes_with_prompt_version(d052, monkeypatch):
    before = extractor.prepare_request(d052, "gemini", "m1").key
    monkeypatch.setattr(extractor, "EXTRACTION_PROMPT_VERSION", extractor.EXTRACTION_PROMPT_VERSION + "-changed")
    assert extractor.prepare_request(d052, "gemini", "m1").key != before


def test_key_changes_with_model_and_provider(d052):
    keys = {extractor.prepare_request(d052, p, m).key for p, m in [("gemini", "m1"), ("gemini", "m2"), ("other", "m1")]}
    assert len(keys) == 3


def test_key_changes_with_generation_settings(d052):
    a = extractor.prepare_request(d052, "gemini", "m1", {"temperature": None, "seed": 1}).key
    assert a != extractor.prepare_request(d052, "gemini", "m1", {"temperature": None, "seed": 2}).key


def test_cache_roundtrip_and_tamper_detection(tmp_path):
    store = ResponseCache(tmp_path)
    fields = {"source_sha256": "abc", "model": "m"}
    key = cache_key(fields)
    assert store.get(key) is None
    store.put(key, {"key": key, "key_fields": fields, "response_text": "{}"})
    assert store.get(key)["response_text"] == "{}"
    store.put(key, {"key": key, "key_fields": {**fields, "model": "other"}, "response_text": "{}"})
    with pytest.raises(CacheIntegrityError):
        store.get(key)


# -------------------------------------------------------------------- status

@pytest.mark.parametrize("enactment, eff, evidence, expected", [
    ("enacted", None, False, "in_force"),
    ("enacted", "2025-08-01", True, "in_force"),
    ("enacted", "2026-10-01", True, "in_force"),          # effective on the query date
    ("enacted", "2026-10-02", True, "not_yet_effective"),
    ("enacted", "2027-07", True, "not_yet_effective"),
    ("enacted", "2025", True, "in_force"),
    ("enacted", "2026-10", True, None),                    # straddles as_of: undeterminable
    ("enacted", "2026", True, None),
    ("enacted", None, True, None),                         # relative date wording: never guessed
    ("enacted", "2026-02-30", True, None),                 # not a real date
    ("pending", None, False, "pending"),
    ("failed", "2025-01-01", True, "failed"),
])
def test_derive_status(enactment, eff, evidence, expected):
    assert derive_status(enactment, eff, evidence, AS_OF)[0] == expected


def test_level_from_manifest_jurisdiction():
    assert (level_for("MA"), level_for("Boston, MA"), level_for("San Francisco, CA")) == ("state", "city", "city")


# ------------------------------------------------------------ model contracts

def test_pydantic_categories_match_official_schema():
    official = sp.read_json(sp.REPO_ROOT / sp.SCHEMA_PATH)
    schema = generation_json_schema()
    rule = schema["properties"]["rules"]["items"]
    assert rule["properties"]["category"]["enum"] == official["properties"]["category"]["enum"]
    assert set(RuleRecord.model_fields) == set(official["properties"])
    assert set(official["required"]) <= {n for n, f in RuleRecord.model_fields.items() if f.is_required()}


def test_generation_schema_is_self_contained():
    text = json.dumps(generation_json_schema())
    assert "$ref" not in text and "$defs" not in text and "pattern" not in text
    rule = generation_json_schema()["properties"]["rules"]["items"]
    assert set(rule["required"]) == set(ExtractedRule.model_fields)


@pytest.mark.parametrize("override", [{"category": "rent_control"}, {"confidence": 1.5}, {"confidence": "0.9"},
                                      {"effective_date": "Aug 1, 2025"}, {"unexpected": "field"}])
def test_extracted_rule_rejects_invalid_values(override):
    with pytest.raises(ValidationError):
        ExtractedRule.model_validate(make_candidate(**override))


def test_rule_record_enforces_official_constraints():
    record = {"team_rule_id": "r-1", "jurisdiction": "MA", "level": "state", "category": "security_deposits",
              "status": "in_force", "title": "t", "requirement": "r", "citation": "c", "source_doc_id": "D052",
              "source_url": "u", "quoted_span": "x" * 20}
    RuleRecord.model_validate(record)
    for bad in ({"quoted_span": "too short"}, {"level": "county"}, {"status": "enacted"}):
        with pytest.raises(ValidationError):
            RuleRecord.model_validate({**record, **bad})
