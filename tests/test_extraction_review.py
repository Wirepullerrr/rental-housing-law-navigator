"""Generation contract and deterministic review checks. Review checks warn; they never reject."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import D052_DATE_EVIDENCE, make_candidate, provision
from navigator.extraction.coverage import closure
from navigator.extraction.models import CandidateResult
from navigator.extraction.models import generation_json_schema
from navigator.extraction.review import calendar_dates, unsupported_figures


def test_generation_schema_requests_inventory_before_rules():
    schema = generation_json_schema()
    assert list(schema["properties"]) == ["provisions", "global_scope", "rules"]  # decoded in this order
    rule = schema["properties"]["rules"]["items"]
    assert {"version_note", "version_evidence", "operative_conditions", "scope_carve_outs", "quote_parts",
            "effective_date_evidence_kind"} <= set(rule["required"])
    item = schema["properties"]["provisions"]["items"]
    assert list(item["properties"]) == ["ref", "summary", "scope", "category", "reason", "rule_indices"]


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
    ("effective June 24, 2023", "effective 6-24-2023.)", []),                      # US numeric dates
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

def test_inventory_is_recorded_and_uncovered_in_scope_provisions_make_the_document_review_required(run_fake):
    provisions = [provision("(1)(b)", rule_indices=[0]), provision("(2)(b)"),
                  provision("(1)(c)", scope="out_of_scope")]
    run, provider = run_fake({"provisions": provisions, "rules": [make_candidate()]}, repair=False)  # cites (1)(b)(iii)
    assert len(run.provision_inventory) == 3 and run.accepted_count == 1 and len(provider.calls) == 1
    assert run.coverage["after_primary"] == {"uncovered": ["(2)(b)"], "unaccepted": [],
                                         "unrecorded_subdivisions": []}
    assert run.document_status == "review_required"
    assert any("repair targets still unresolved: (2)(b) (repair did not complete: repair disabled" in r
               for r in run.review_reasons)


def _candidate(index: int, citation: str, accepted: bool = True) -> CandidateResult:
    return CandidateResult(index=index, accepted=accepted, rule={"citation": citation})


def test_closure_matches_citations_structurally():
    inventory = [provision("Section 15B(2)(a), first paragraph"), provision("§ 98.0704 (a)"),
                 provision("4.a"), provision("Section 15B(5)"), provision("Section 15B(2)(b)"),
                 provision("§ 98.0709(a)-(c)")]
    cited = ["M.G.L. c. 186, § 15B(2)(a)", "San Diego Mun. Code §98.0704(a)(1)", "P.L. 2026, c. 43, § 4(a)",
             "M.G.L. c. 186, § 15B(4)", "§ 98.0709(b)"]
    result = closure(inventory, [_candidate(i, c) for i, c in enumerate(cited)])
    assert result["uncovered"] == ["Section 15B(5)", "Section 15B(2)(b)", "§ 98.0709(a)", "§ 98.0709(c)"]


def test_declared_link_counts_only_for_an_ancestor_citation():
    inventory = [provision("§ 98.0704(b)(1)(A)", rule_indices=[0]), provision("§ 98.0704(b)(2)", rule_indices=[0]),
                 provision("§ 98.0705(c)", rule_indices=[9])]
    result = closure(inventory, [_candidate(0, "San Diego Mun. Code § 98.0704(b)(1)")])
    assert result["uncovered"] == ["§ 98.0704(b)(2)", "§ 98.0705(c)"]
    assert result["provisions"][0]["basis"] == {"0": "declared link; citation names an ancestor"}
    assert len(result["link_mismatches"]) == 2       # (b)(1) is not (b)(2); rule 9 does not exist


def test_rejected_records_count_as_candidates_but_not_as_accepted(run_fake):
    candidate = make_candidate(quoted_span="A lessor may never require a deposit above half a month's rent.")
    run, provider = run_fake({"provisions": [provision("(1)(b)", rule_indices=[0])], "rules": [candidate]})
    assert run.accepted_count == 0 and len(provider.calls) == 1                 # not uncovered: no repair
    assert run.coverage["after_primary"] == {"uncovered": [], "unaccepted": ["(1)(b)"],
                                         "unrecorded_subdivisions": []}
    assert run.document_status == "review_required"


@pytest.mark.parametrize("payload_extra, message", [({}, "no provision inventory"),
                                                    ({"provisions": [{"ref": ""}]}, "inventory is malformed")])
def test_missing_or_malformed_inventory_never_rejects_rules_but_blocks_completeness(run_fake, payload_extra, message):
    run, _ = run_fake({**payload_extra, "rules": [make_candidate()]})
    assert run.errors == [] and run.accepted_count == 1
    assert any(message in w for w in run.warnings)
    assert run.document_status == "review_required"


def test_complete_inventory_coverage_marks_the_document_complete(run_fake):
    run, _ = run_fake({"provisions": [provision("§ 15B(1)(b)", rule_indices=[0])], "global_scope": [],
                       "rules": [make_candidate()]})
    assert (run.document_status, run.review_reasons, run.repair) == ("complete", [], None)


def test_calendar_dates_reads_us_numeric_dates():
    assert [d.isoformat() for d in calendar_dates("added 5-25-2023 by O-1; effective 6/24/2023.")] ==         ["2023-05-25", "2023-06-24"]
