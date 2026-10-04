"""Prompt-v3 robustness: page artifacts, provenance, document-level scope, operative
conditions, no model confidence, thinking level. Synthetic documents only."""

from __future__ import annotations

import pytest

from conftest import (D052_DATE_EVIDENCE, FakeProvider, make_candidate, page_body, paged, parts,
                      running_header, synthetic_source)
from navigator.extraction import extractor
from navigator.extraction.cache import sha256_hex
from navigator.extraction.extractor import SourceDocument, extract_document
from navigator.extraction.models import generation_json_schema
from navigator.extraction.source_view import PAGE_BREAK_MARKER, build_view

# ------------------------------------------------------------ page artifacts

def test_running_headers_with_page_counter_are_marked_and_recorded():
    pages = [page_body(n) for n in range(4)]
    raw = paged(pages, running_header)
    view = build_view(raw)
    assert len(view.artifacts) == 4 and view.text.count(PAGE_BREAK_MARKER) == 4
    assert [a.text.splitlines()[1] for a in view.artifacts] == ["9 8 7 1", "9 8 7 2", "9 8 7 3", "9 8 7 4"]
    assert all(raw[a.raw_start:a.raw_end] == a.text for a in view.artifacts)       # recorded verbatim
    assert "Example City Municipal Code" not in view.text
    assert all(line in view.text for page in pages for line in page)                # no legal text lost


def test_view_maps_back_to_raw_exactly():
    raw = paged([page_body(n) for n in range(3)], running_header)
    view = build_view(raw)
    for s in view.segments:
        assert view.text[s.view_start:s.view_start + s.length] == raw[s.raw_start:s.raw_start + s.length]
        assert view.to_raw(s.view_start) == s.raw_start
    assert view.to_raw(view.text.index(PAGE_BREAK_MARKER)) is None
    assert sum(s.length for s in view.segments) + sum(len(a.text) for a in view.artifacts) == len(raw)


@pytest.mark.parametrize("header, pages, lines", [
    (lambda n: ["3/12/2026", "House"], 4, 10),                               # identical repeats: no page counter
    (lambda n: ["RAC Reg", f"{[210, 250, 240, 230][n - 1]} )."], 4, 10),     # numbers do not count upwards
    (running_header, 2, 10),                                                 # fewer than three occurrences
    (running_header, 4, 3),                                                  # counter, but not a page apart
])
def test_repeats_that_are_not_provably_page_furniture_are_kept(header, pages, lines):
    raw = paged([page_body(n, lines) for n in range(pages)], header)
    assert build_view(raw).artifacts == ()


def test_single_repeated_lines_and_adjacent_rows_are_kept():
    raw = "\n".join(["Intro line one here."] + ["or", "Clause text continues here."] * 5) + "\n"
    rows = "\n".join(f"Page {n}\nHistory row" for n in range(1, 5)) + "\n"   # counter, but rows are adjacent
    assert build_view(raw).artifacts == () and build_view(rows).artifacts == ()


def test_quote_across_a_page_break_is_rejected_and_diagnosed(tmp_path):
    pages = [page_body(n) for n in range(3)]
    source = synthetic_source(paged(pages, running_header))
    within = pages[1][2]
    across = pages[0][-1] + "\n" + pages[1][0]       # joins both sides, header dropped
    response = {"rules": [make_candidate(quoted_span=within, citation="Code § 1"),
                          make_candidate(quoted_span=across, citation="Code § 2")]}
    run, _ = _run(source, response, tmp_path)
    assert run.candidates[0].accepted and run.candidates[0].citation.status == "exact_match"
    assert not run.candidates[1].accepted
    assert "across a removed page artifact" in run.candidates[1].citation.reason
    assert run.source_view["artifacts_removed"] == 3 and run.source_view["artifacts"][0]["text"].startswith("Ch. Art.")


def test_model_reads_the_marked_view_but_cache_identity_tracks_it(tmp_path):
    source = synthetic_source(paged([page_body(n) for n in range(3)], running_header))
    req = extractor.prepare_request(source, "fake", "fake-model")
    assert PAGE_BREAK_MARKER in req.prompt and "Example City Municipal Code" not in req.prompt
    assert req.key_fields["view_sha256"] == sha256_hex(source.view.text)


# ------------------------------------------------------- document-level scope

D052_VACATION_EXEMPTION = ("The provisions of this section shall not apply to any lease, rental, occupancy or "
                           "tenancy of one hundred days or less in duration")
D052_RETURN_SPAN = "The lessor shall, within thirty days after the termination of occupancy under a tenancy-at-will"


def scope(sid="S1", governs=None, evidence=D052_VACATION_EXEMPTION, kind="exemption"):
    return {"id": sid, "kind": kind, "statement": "Vacation rentals of 100 days or less are exempt.",
            "citation": "M.G.L. c. 186, § 15B(9)", "governs": governs, "evidence_parts": parts(evidence)}


def test_document_wide_exemption_propagates_to_every_rule(run_fake):
    rules = [make_candidate(), make_candidate(category="rent_increase_limits")]
    run, _ = run_fake({"global_scope": [scope()], "rules": rules})
    assert run.accepted_count == 2
    for c, rule in zip(run.candidates, run.rules):
        assert "Vacation rentals of 100 days or less are exempt. [M.G.L. c. 186, § 15B(9); document-wide]" == rule["exemptions"]
        assert c.propagated_scope == [{"id": "S1", "kind": "exemption", "applied": True, "basis": "document-wide"}]
    assert run.global_scope[0]["propagated"] and run.global_scope[0]["evidence_check"]["status"] == "exact_match"


def test_scoped_condition_applies_only_to_rules_it_governs(run_fake):
    rules = [make_candidate(),  # cites § 15B(1)(b)(iii)
             make_candidate(citation="M.G.L. c. 186, § 15B(4)", quoted_span=D052_RETURN_SPAN, title="Return")]
    run, _ = run_fake({"global_scope": [scope(governs="§ 15B(4)")], "rules": rules})
    assert run.rules[0]["exemptions"] is None
    assert "applies to § 15B(4)" in run.rules[1]["exemptions"]


def test_unverified_scope_condition_is_never_propagated(run_fake):
    run, _ = run_fake({"global_scope": [scope(evidence="This Division does not apply to any building at all.")],
                       "rules": [make_candidate()]})
    assert run.rules[0]["exemptions"] is None and not run.global_scope[0]["propagated"]
    assert any("S1 evidence not found" in w for w in run.warnings)


@pytest.mark.parametrize("evidence, applied", [(D052_DATE_EVIDENCE, False), ("Rule 7 is exempt from S1.", True)])
def test_carve_out_needs_verified_evidence(run_fake, evidence, applied):
    candidate = make_candidate(scope_carve_outs=[{"scope_id": "S1", "evidence": evidence}])
    run, _ = run_fake({"global_scope": [scope()], "rules": [candidate]})
    assert (run.rules[0]["exemptions"] is not None) is applied
    assert run.candidates[0].propagated_scope[0]["applied"] is applied


# ------------------------------------------------------- operative conditions

def test_operative_condition_is_kept_without_date_or_status_change(tmp_path):
    body = ("A landlord shall file each termination notice with the Commission.\n"
            "This requirement does not apply until 30 days after the Commission establishes a submission portal.\n")
    candidate = make_candidate(quoted_span="A landlord shall file each termination notice with the Commission.",
                               operative_conditions=[{"statement": "Applies once the Commission portal exists.",
                                                      "evidence": "does not apply until 30 days after the Commission "
                                                                  "establishes a submission portal"}])
    run, _ = _run(synthetic_source(body), {"rules": [candidate]}, tmp_path)
    c, rule = run.candidates[0], run.rules[0]
    assert c.accepted and (rule["status"], rule["effective_date"], rule["exemptions"]) == ("in_force", None, None)
    assert "Operative condition (unresolved" in rule["coverage_conditions"]
    assert c.operative_conditions[0]["evidence_check"]["status"] == "exact_match"
    assert any("unresolved operative condition" in w for w in c.warnings)


def test_fabricated_operative_condition_is_rejected(run_fake):
    candidate = make_candidate(operative_conditions=[{"statement": "x", "evidence": "once the portal is live"}])
    run, _ = run_fake({"rules": [candidate]})
    assert not run.candidates[0].accepted


# ------------------------------------------------------------ confidence

def test_model_confidence_is_neither_requested_nor_published(run_fake):
    rule_schema = generation_json_schema()["properties"]["rules"]["items"]
    assert "confidence" not in rule_schema["properties"]
    run, _ = run_fake({"rules": [{**make_candidate(), "confidence": 0.99}]})
    assert run.candidates[0].accepted and run.rules[0]["confidence"] is None
    assert any("'confidence'; ignored" in w for w in run.candidates[0].warnings)


# ------------------------------------------------------------ thinking level

def test_thinking_level_is_part_of_cache_identity(d052):
    low = extractor.prepare_request(d052, "gemini", "m", {"temperature": None, "seed": 1, "thinking_level": "low"})
    medium = extractor.prepare_request(d052, "gemini", "m", {"temperature": None, "seed": 1, "thinking_level": "medium"})
    assert low.key != medium.key and low.key_fields["generation_settings"]["thinking_level"] == "low"


def _run(source: SourceDocument, response, tmp_path):
    from navigator.extraction.cache import ResponseCache
    provider = FakeProvider(response)
    return extract_document(source, provider_name=provider.name, model=provider.model, provider=provider,
                            cache=ResponseCache(tmp_path / "cache")), provider
