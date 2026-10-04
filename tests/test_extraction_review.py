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
    assert list(schema["properties"]) == ["document", "provisions", "global_scope", "rules",
                                          "no_rules_justification"]                 # decoded in this order
    rule = schema["properties"]["rules"]["items"]
    assert {"version_note", "version_evidence", "operative_conditions", "scope_carve_outs", "quote_parts",
            "effective_date_evidence_kind", "provision_ids", "source_basis",
            "enactment_status_evidence"} <= set(rule["required"])
    assert list(rule["properties"])[:5] == ["category", "citation", "provision_ids", "source_basis", "quote_parts"]
    item = schema["properties"]["provisions"]["items"]
    assert list(item["properties"]) == ["id", "ref", "anchor", "summary", "role", "scope", "category", "reason"]


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
    provisions = [provision("(1)(b)"), provision("(2)(b)", id="P2"), provision("(1)(c)", scope="out_of_scope", id="P3")]
    run, provider = run_fake({"provisions": provisions, "rules": [make_candidate()]}, repair=False)  # cites (1)(b)(iii)
    assert len(run.provision_inventory) == 3 and run.accepted_count == 1 and len(provider.calls) == 1
    assert run.coverage["after_primary"] == {"uncovered": ["(2)(b)"], "unaccepted": [],
                                             "unrecorded_subdivisions": [], "scope_challenges": []}
    assert run.document_status == "review_required"
    assert any("repair targets still unresolved: (2)(b) (repair did not complete: repair disabled" in r
               for r in run.review_reasons)


def _candidate(index: int, citation: str, links: list[str], accepted: bool = True) -> CandidateResult:
    return CandidateResult(index=index, accepted=accepted, rule={"citation": citation}, provision_ids=links)


def test_closure_needs_a_provision_link_with_a_structurally_matching_citation():
    refs = ["Section 15B(2)(a), first paragraph", "§ 98.0704 (a)", "4.a", "Section 15B(5)", "Section 15B(2)(b)",
            "§ 98.0709(a)-(c)"]
    inventory = [provision(ref, id=f"P{n}") for n, ref in enumerate(refs, start=1)]
    cited = [("M.G.L. c. 186, § 15B(2)(a)", ["P1"]), ("San Diego Mun. Code §98.0704(a)(1)", ["P2"]),
             ("P.L. 2026, c. 43, § 4(a)", ["P3"]), ("M.G.L. c. 186, § 15B(4)", ["P4"]),
             ("§ 98.0709(b)", ["P6"]),
             ("M.G.L. c. 186, § 15B(2)(b)", [])]           # names Section 15B(2)(b), but links nothing
    result = closure(inventory, [_candidate(i, c, links) for i, (c, links) in enumerate(cited)])
    assert result["uncovered"] == ["Section 15B(5)", "Section 15B(2)(b)", "§ 98.0709(a)", "§ 98.0709(c)"]
    assert result["link_mismatches"] == ["rule 3 links P4 ('Section 15B(5)') but cites 'M.G.L. c. 186, § 15B(4)'"]


def test_a_link_counts_for_an_ancestor_citation_but_not_for_a_sibling():
    inventory = [provision("§ 98.0704(b)(1)(A)"), provision("§ 98.0704(b)(2)", id="P2"),
                 provision("§ 98.0705(c)", id="P3")]
    result = closure(inventory, [_candidate(0, "San Diego Mun. Code § 98.0704(b)(1)", ["P1", "P2"])])
    assert result["uncovered"] == ["§ 98.0704(b)(2)", "§ 98.0705(c)"]
    assert result["provisions"][0]["basis"] == {"0": "provision link; citation names an ancestor"}
    assert len(result["link_mismatches"]) == 1       # (b)(1) is not (b)(2)


def test_rejected_records_count_as_candidates_but_not_as_accepted(run_fake):
    candidate = make_candidate(quoted_span="A lessor may never require a deposit above half a month's rent.")
    run, provider = run_fake({"provisions": [provision("(1)(b)")], "rules": [candidate]})
    assert run.accepted_count == 0 and len(provider.calls) == 1                 # not uncovered: no repair
    assert run.coverage["after_primary"] == {"uncovered": [], "unaccepted": ["(1)(b)"],
                                             "unrecorded_subdivisions": [], "scope_challenges": []}
    assert run.document_status == "review_required"


@pytest.mark.parametrize("payload_extra, message", [({}, "no provision inventory"),
                                                    ({"provisions": [{"ref": ""}]}, "inventory is malformed")])
def test_missing_or_malformed_inventory_never_rejects_rules_but_blocks_completeness(run_fake, payload_extra, message):
    run, _ = run_fake({**payload_extra, "rules": [make_candidate()]})
    assert run.errors == [] and run.accepted_count == 1
    assert any(message in w for w in run.warnings)
    assert run.document_status == "review_required"


def test_complete_inventory_coverage_marks_the_document_complete(run_fake):
    run, _ = run_fake({"provisions": [provision("§ 15B(1)(b)")], "global_scope": [], "rules": [make_candidate()],
                       "no_rules_justification": None})
    assert (run.document_status, run.review_reasons, run.repair) == ("complete", [], None)


def test_calendar_dates_reads_us_numeric_dates():
    assert [d.isoformat() for d in calendar_dates("added 5-25-2023 by O-1; effective 6/24/2023.")] ==         ["2023-05-25", "2023-06-24"]
