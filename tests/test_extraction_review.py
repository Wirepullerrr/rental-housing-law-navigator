"""Prompt-v2 contract and deterministic review checks. Review checks warn; they never reject."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import D052_DATE_EVIDENCE, make_candidate
from navigator.extraction.models import generation_json_schema
from navigator.extraction.review import uncovered_provisions, unsupported_figures


def test_generation_schema_requests_inventory_before_rules():
    schema = generation_json_schema()
    assert list(schema["properties"]) == ["provisions", "rules"]  # decoded in this order
    rule = schema["properties"]["rules"]["items"]
    assert {"version_note", "version_evidence"} <= set(rule["required"])


# ---------------------------------------------------------- temporal versions

def test_version_history_is_not_a_conflict(run_fake):
    candidate = make_candidate(version_note="Introductory paragraph amended effective August 1, 2025.",
                               version_evidence=D052_DATE_EVIDENCE)
    run, _ = run_fake({"rules": [candidate]})
    c, rule = run.candidates[0], run.rules[0]
    assert c.accepted and c.version_evidence.status == "exact_match"
    assert (rule["conflict_flag"], rule["conflict_note"]) == (False, None)
    assert (rule["effective_date"], rule["status"]) == (None, "in_force")


def test_fabricated_version_evidence_is_rejected(run_fake):
    candidate = make_candidate(version_note="amended", version_evidence="As amended by chapter 99, effective March 3, 2024.")
    run, _ = run_fake({"rules": [candidate]})
    assert not run.candidates[0].accepted
    assert any("version_evidence" in r for r in run.candidates[0].rejection_reasons)


def test_version_annotation_dated_after_as_of_is_rejected_conservatively(run_fake):
    candidate = make_candidate(version_note="amended", version_evidence=D052_DATE_EVIDENCE)
    run, _ = run_fake({"rules": [candidate]}, as_of=date(2025, 7, 31))
    assert not run.candidates[0].accepted
    assert any("dated 2025-08-01, after as_of" in r for r in run.candidates[0].rejection_reasons)


# ------------------------------------------------------- quote-to-claim figures

@pytest.mark.parametrize("claim, span, expected", [
    ("must be returned within 45 days", "shall return within thirty days", ["45 days"]),
    ("must be returned within 30 days", "shall, within thirty days after termination", []),
    ("interest at 5% per year", "interest at the rate of five per cent per year", []),
    ("within 10 business days", "within ten (10) business days", []),
    ("tenancies of 100 days or less", "tenancy of one hundred days or less", []),
    ("a fee of $1,000", "a fee not to exceed $1000.00", []),
    ("a fee of $75", "a fee not to exceed $50", ["$75"]),
    ("effective August 1, 2025", "effective 2025-08-01", []),
    ("takes effect January 1, 2027", "takes effect January 1, 2026", ["January 1, 2027"]),
])
def test_unsupported_figures(claim, span, expected):
    assert unsupported_figures(claim, span) == expected


def test_unsupported_figure_warns_but_does_not_reject(run_fake):
    candidate = make_candidate(requirement="A deposit may not exceed the first month's rent and is due back within 45 days.")
    run, _ = run_fake({"rules": [candidate]})
    c = run.candidates[0]
    assert c.accepted
    assert any("'45 days'" in w for w in c.warnings)


# ------------------------------------------------------- run-level review

CATEGORIES = ["security_deposits", "rent_increase_limits", "just_cause_eviction"]


@pytest.mark.parametrize("confidences, flagged", [([0.95, 0.95, 0.95], True), ([0.9, 0.7, 0.8], False)])
def test_uniform_confidence_is_flagged(run_fake, confidences, flagged):
    rules = [make_candidate(category=cat, confidence=conf) for cat, conf in zip(CATEGORIES, confidences)]
    run, _ = run_fake({"rules": rules})
    assert run.accepted_count == 3
    assert any("carries no signal" in w for w in run.warnings) is flagged


def test_inventory_is_recorded_and_uncited_in_scope_provisions_are_flagged(run_fake):
    provisions = [{"ref": "(1)(b)", "summary": "upfront payment cap", "category": "security_deposits"},
                  {"ref": "(2)(b)", "summary": "deposit receipt", "category": "security_deposits"},
                  {"ref": "(1)(c)", "summary": "late rent penalty", "category": None}]
    run, _ = run_fake({"provisions": provisions, "rules": [make_candidate()]})  # cites § 15B(1)(b)(iii)
    assert len(run.provision_inventory) == 3 and run.accepted_count == 1
    [warning] = [w for w in run.warnings if "cited by no extracted record" in w]
    assert "'(2)(b)'" in warning and "(1)(b)" not in warning and "(1)(c)" not in warning


def test_inventory_ref_matching_ignores_punctuation_style_and_descriptive_suffixes():
    inventory = [{"ref": "Section 15B(2)(a), first paragraph", "category": "security_deposits"},
                 {"ref": "§ 98.0704 (a)", "category": "just_cause_eviction"},
                 {"ref": "4.a", "category": "algorithmic_rent_setting"},
                 {"ref": "Section 15B(5)", "category": "security_deposits"},
                 {"ref": "Section 15B(2)(b)", "category": "security_deposits"}]
    cited = ["M.G.L. c. 186, § 15B(2)(a)", "San Diego Mun. Code §98.0704(a)(1)", "P.L. 2026, c. 43, § 4(a)",
             "M.G.L. c. 186, § 15B(4)"]
    assert uncovered_provisions(inventory, cited) == ["Section 15B(5)", "Section 15B(2)(b)"]


def test_status_rejected_records_still_count_as_extracted_for_recall(run_fake):
    provisions = [{"ref": "(1)(b)", "summary": "upfront payment cap", "category": "security_deposits"}]
    candidate = make_candidate(effective_date_evidence=D052_DATE_EVIDENCE)  # relative/undated -> status rejected
    run, _ = run_fake({"provisions": provisions, "rules": [candidate]})
    assert run.accepted_count == 0
    assert not any("cited by no extracted record" in w for w in run.warnings)


@pytest.mark.parametrize("payload_extra, message", [({}, "no provision inventory"),
                                                    ({"provisions": [{"ref": ""}]}, "inventory is malformed")])
def test_missing_or_malformed_inventory_is_only_a_warning(run_fake, payload_extra, message):
    run, _ = run_fake({**payload_extra, "rules": [make_candidate()]})
    assert run.errors == [] and run.accepted_count == 1
    assert any(message in w for w in run.warnings)
