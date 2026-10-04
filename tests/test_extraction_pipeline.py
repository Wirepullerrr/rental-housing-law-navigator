"""End-to-end extraction pipeline on the real D052 text, driven by FakeProvider."""

from __future__ import annotations

import json
import re
from datetime import date

import pytest

from conftest import (D052_DATE_EVIDENCE, D052_DEPOSIT_SPAN, D052_DOUBLE_SPACED, FakeProvider, make_candidate)
from navigator import starter_pack as sp
from navigator.extraction.extractor import CacheMiss, extract_document, write_artifact
from navigator.extraction.provider import StructuredLLMProvider
from navigator.validation import make_rule_validator


def test_fake_provider_satisfies_protocol():
    assert isinstance(FakeProvider({"rules": []}), StructuredLLMProvider)


def test_valid_candidate_is_accepted_with_trusted_metadata(run_fake, d052):
    run, provider = run_fake({"rules": [make_candidate()]})
    assert len(provider.calls) == 1
    assert (run.candidate_count, run.accepted_count, run.errors) == (1, 1, [])
    c = run.candidates[0]
    assert c.accepted and c.pydantic_valid and c.schema_valid
    assert c.citation.status == "exact_match"
    rule = run.rules[0]
    assert rule["source_doc_id"] == "D052"
    assert rule["source_url"] == d052.meta.url == "https://malegislature.gov/Laws/GeneralLaws/PartII/TitleI/Chapter186/Section15B"
    assert (rule["jurisdiction"], rule["level"], rule["status"], rule["overrides"]) == ("MA", "state", "in_force", [])
    assert re.fullmatch(r"r-[0-9a-f]{10}", rule["team_rule_id"])
    assert run.source.retrieved_at == "2026-10-01T22:37Z"


def test_accepted_rule_passes_official_json_schema(run_fake):
    run, _ = run_fake({"rules": [make_candidate()]})
    make_rule_validator(sp.read_json(sp.REPO_ROOT / sp.SCHEMA_PATH)).validate(run.rules[0])


def test_fabricated_quoted_span_is_rejected(run_fake):
    span = "A lessor may never require a security deposit above half of one month's rent."
    run, _ = run_fake({"rules": [make_candidate(quoted_span=span)]})
    c = run.candidates[0]
    assert c.citation.status == "failed" and not c.accepted
    assert any(r.startswith("citation:") for r in c.rejection_reasons)
    assert run.rules == [] and c.rule is not None  # kept for review, never published


def test_near_miss_paraphrase_is_rejected(run_fake):
    run, _ = run_fake({"rules": [make_candidate(quoted_span=D052_DEPOSIT_SPAN.replace("month's", "months"))]})
    assert run.candidates[0].citation.status == "failed"
    assert run.accepted_count == 0


def test_whitespace_normalized_span_publishes_exact_source_text(run_fake, d052):
    collapsed = " ".join(D052_DOUBLE_SPACED.split())
    assert collapsed not in d052.body
    run, _ = run_fake({"rules": [make_candidate(quoted_span=collapsed)]})
    c = run.candidates[0]
    assert c.accepted and c.citation.status == "normalized_match"
    assert c.citation.model_span == collapsed
    assert run.rules[0]["quoted_span"] == D052_DOUBLE_SPACED
    assert run.rules[0]["quoted_span"] in d052.body


def test_invalid_category_is_rejected(run_fake):
    run, _ = run_fake({"rules": [make_candidate(category="rent_control")]})
    c = run.candidates[0]
    assert not c.pydantic_valid and not c.accepted
    assert any("category" in e for e in c.pydantic_errors)
    assert run.rules == []


def test_model_supplied_trusted_fields_are_ignored(run_fake, d052):
    candidate = make_candidate(source_url="https://example.com/fake", source_doc_id="D999",
                               jurisdiction="CA", team_rule_id="r-0001", status="pending")
    run, _ = run_fake({"rules": [candidate]})
    rule = run.rules[0]
    assert (rule["source_url"], rule["source_doc_id"], rule["jurisdiction"]) == (d052.meta.url, "D052", "MA")
    assert rule["team_rule_id"] != "r-0001" and rule["status"] == "in_force"
    warned = " ".join(run.candidates[0].warnings)
    for name in ("source_url", "source_doc_id", "jurisdiction", "team_rule_id", "status"):
        assert repr(name) in warned


def test_no_rules_response_is_clean(run_fake):
    run, _ = run_fake({"rules": []})
    assert (run.candidate_count, run.accepted_count, run.errors, run.rules) == (0, 0, [], [])
    assert run.fully_accepted


@pytest.mark.parametrize("response", ["not json at all", {"items": []}, {"rules": "none"}])
def test_malformed_response_is_a_run_error(run_fake, response):
    run, _ = run_fake(response)
    assert run.errors and run.rules == [] and not run.fully_accepted


def test_effective_date_requires_verified_evidence(run_fake):
    run, _ = run_fake({"rules": [make_candidate(effective_date="2025-08-01")]})
    assert not run.candidates[0].accepted
    assert any(r.startswith("effective_date:") for r in run.candidates[0].rejection_reasons)


def test_fabricated_effective_date_evidence_is_rejected(run_fake):
    run, _ = run_fake({"rules": [make_candidate(effective_date="2025-08-01",
                                                effective_date_evidence="This section takes effect August 1, 2025.")]})
    assert run.candidates[0].effective_date_evidence.status == "failed"
    assert run.accepted_count == 0


@pytest.mark.parametrize("kind", ["explicit_operative_date", "history_note"])
def test_d052_amendment_annotation_never_becomes_the_effective_date(run_fake, kind):
    """The 2025 amendment note is history: whatever the model calls it, effective_date stays null."""
    candidate = make_candidate(effective_date="2025-08-01", effective_date_evidence=D052_DATE_EVIDENCE,
                               effective_date_evidence_kind=kind)
    run, _ = run_fake({"rules": [candidate]})
    c, rule = run.candidates[0], run.rules[0]
    assert c.accepted and (rule["effective_date"], rule["status"]) == (None, "in_force")
    assert c.temporal["applied_kind"] == "history_note"
    assert c.temporal["history_evidence"].startswith("[ Introductory paragraph")   # enclosing note, verbatim
    assert any("history" in w for w in c.warnings)


def test_d052_amendment_annotation_after_as_of_rejects_conservatively(run_fake):
    candidate = make_candidate(effective_date_evidence=D052_DATE_EVIDENCE, effective_date_evidence_kind="history_note")
    run, _ = run_fake({"rules": [candidate]}, as_of=date(2025, 7, 31))
    assert not run.candidates[0].accepted
    assert any("history note is dated 2025-08-01" in r for r in run.candidates[0].rejection_reasons)


def test_pending_and_failed_pass_through(run_fake):
    run, _ = run_fake({"rules": [make_candidate(enactment_status="pending"),
                                 make_candidate(enactment_status="failed", category="rent_increase_limits")]})
    assert [r["status"] for r in run.rules] == ["pending", "failed"]


def test_duplicate_candidates_are_flagged(run_fake):
    run, _ = run_fake({"rules": [make_candidate(), make_candidate(title="Same rule, different title")]})
    assert [c.accepted for c in run.candidates] == [True, False]
    assert "duplicate" in run.candidates[1].rejection_reasons[0]


# ------------------------------------------------------------------- caching

def test_cache_hit_avoids_provider_call(run_fake, d052, cache):
    response = {"rules": [make_candidate()]}
    first, provider = run_fake(response)
    assert not first.cache_hit and len(provider.calls) == 1
    second, _ = run_fake(response, provider=provider)
    assert second.cache_hit and len(provider.calls) == 1
    offline = extract_document(d052, provider_name=provider.name, model=provider.model, cache=cache)
    assert offline.cache_hit and offline.rules == first.rules


def test_force_bypasses_cache(run_fake):
    _, provider = run_fake({"rules": [make_candidate()]})
    forced, _ = run_fake(None, provider=provider, force=True)
    assert not forced.cache_hit and len(provider.calls) == 2


def test_cache_miss_without_provider_does_not_call_anything(d052, cache):
    with pytest.raises(CacheMiss):
        extract_document(d052, provider_name="fake", model="fake-model", cache=cache)
    assert not cache.root.exists() or not any(cache.root.glob("*.json"))


def test_provider_identity_must_match_cache_identity(d052, cache):
    with pytest.raises(ValueError):
        extract_document(d052, provider_name="fake", model="other-model", provider=FakeProvider({"rules": []}),
                         cache=cache)


# ------------------------------------------------------------------ artifact

def test_audit_artifact_is_complete_and_secret_free(run_fake, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-sentinel-value")
    run, _ = run_fake({"rules": [make_candidate()]})
    path = tmp_path / "D052_extraction.json"
    write_artifact(run, path)
    text = path.read_text(encoding="utf-8")
    assert "sk-test-sentinel-value" not in text and "GEMINI_API_KEY" not in text
    data = json.loads(text)
    for key in ("source", "provider", "model", "prompt_version", "prompt_sha256", "cache_key", "cache_hit",
                "candidate_count", "accepted_count", "rules", "candidates", "warnings", "errors", "disclaimer"):
        assert key in data
    assert data["source"]["content_sha256"] == run.source.content_sha256
    assert data["candidates"][0]["schema_valid"] is True
