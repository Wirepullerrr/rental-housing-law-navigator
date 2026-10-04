"""Prompt-v4 / M2.7: cross-page quote parts with raw-span reconstruction, coverage
closure with one targeted repair pass, and effective-date evidence classification.
Synthetic documents (and the real D052 text) only; no network."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import (FakeProvider, make_candidate, page_body, paged, parts, provision, running_header,
                      synthetic_source)
from navigator.extraction.cache import ResponseCache
from navigator.extraction.extractor import extract_document, prepare_request
from navigator.extraction.models import QuotePart
from navigator.extraction.prompt import REPAIR_SYSTEM_INSTRUCTION, SYSTEM_INSTRUCTION
from navigator.extraction.provider import ProviderError
from navigator.extraction.quotes import verify_quote_parts
from navigator.extraction.source_view import Artifact, Segment, SourceView

# A sentence that runs from the end of page 1 into page 2, across the page-2 running header.
TAIL = "The landlord shall pay the tenant relocation assistance equal to"
HEAD = "two months of rent within fifteen days of the notice."
EXEMPT_TAIL = "This Division shall not apply to a dwelling unit owned by a natural person"
EXEMPT_HEAD = "who is not a corporation, if the tenant received written notice of the exemption."
PAGES = [page_body(0)[:-1] + [TAIL], [HEAD] + page_body(1)[1:], page_body(2)[:-1] + [EXEMPT_TAIL],
         [EXEMPT_HEAD] + page_body(3)[1:]]


@pytest.fixture
def doc():
    return synthetic_source(paged(PAGES, running_header))


def run(source, *responses, tmp_path, provider=None, **kwargs):
    provider = provider or FakeProvider(*responses)
    result = extract_document(source, provider_name=provider.name, model=provider.model, provider=provider,
                              cache=kwargs.pop("cache", None) or ResponseCache(tmp_path / "cache"), **kwargs)
    return result, provider


def rule(quote_parts, citation="Code § 1(a)", **overrides):
    fields = {"quote_parts": quote_parts, "citation": citation, "category": "just_cause_eviction", "key_value": None,
              "requirement": "A landlord must pay relocation assistance."}
    return make_candidate(**{**fields, **overrides})


# ------------------------------------------------------------- quote parts

def test_view_labels_numbered_segments(doc):
    view = doc.view
    assert [s.id for s in view.segments] == [1, 2, 3, 4] and len(view.artifacts) == 4
    assert "[[SEGMENT 1]]" in prepare_request(doc, "fake", "fake-model").prompt
    assert all(view.text[s.view_start:s.view_start + s.length] == doc.body[s.raw_start:s.raw_end]
               for s in view.segments)


def test_single_segment_quote_uses_the_exact_single_span_path(doc, tmp_path):
    line = PAGES[1][3]
    result, _ = run(doc, {"rules": [rule(parts(line, first_segment=2))]}, tmp_path=tmp_path)
    c = result.candidates[0]
    assert c.accepted and c.citation.status == "exact_match" and not c.citation.reconstructed
    assert result.rules[0]["quoted_span"] == line and c.citation.crossed_artifacts == []


def test_two_adjacent_parts_reconstruct_the_exact_raw_span_across_one_artifact(doc, tmp_path):
    candidate = rule(parts(TAIL, HEAD), requirement="A landlord must pay two months of rent within 15 days.")
    result, _ = run(doc, {"rules": [candidate]}, tmp_path=tmp_path)
    c, published = result.candidates[0], result.rules[0]["quoted_span"]
    header = doc.view.artifacts[1]
    assert c.accepted and c.citation.reconstructed and c.citation.status == "exact_match"
    assert published == doc.body[c.citation.start:c.citation.end]             # contiguous raw substring
    assert published.startswith(TAIL) and published.endswith(HEAD)
    assert header.text in published and "9 8 7 2" in published                # the header is inside, as in the source
    assert c.citation.crossed_artifacts == [{"raw_start": header.raw_start, "raw_end": header.raw_end,
                                             "text": header.text}]
    assert [(p["segment_id"], doc.body[p["start"]:p["end"]]) for p in c.citation.parts] == [(1, TAIL), (2, HEAD)]
    assert any("crosses one page artifact" in w for w in c.warnings)
    assert not any("which its quoted_span does not contain" in w for w in c.warnings)


def test_reconstruction_is_deterministic(doc, tmp_path):
    first, _ = run(doc, {"rules": [rule(parts(TAIL, HEAD))]}, tmp_path=tmp_path / "a")
    second, _ = run(doc, {"rules": [rule(parts(TAIL, HEAD))]}, tmp_path=tmp_path / "b")
    assert first.rules == second.rules


@pytest.mark.parametrize("quote, reason", [
    ([{"segment_id": 1, "quoted_text": TAIL}, {"segment_id": 3, "quoted_text": PAGES[2][0]}],
     "adjacent segments"),                                                       # segments 1 and 3
    ([{"segment_id": 2, "quoted_text": HEAD}, {"segment_id": 1, "quoted_text": TAIL}], "adjacent segments"),
    (parts(PAGES[0][4], HEAD), "would be skipped"),                              # part 1 not at the page break
    (parts(TAIL, PAGES[1][2]), "would be skipped"),                              # part 2 not at the page break
    (parts(TAIL, "two months of rent within ten days of the notice."), "part 2 not found"),
    (parts(TAIL, HEAD, PAGES[2][0]), "at most two"),
    (parts(TAIL + "\n[[PAGE BREAK]]\n" + HEAD), "view marker"),
    (parts(TAIL + " " + HEAD), "across a removed page artifact"),               # one part stitched across
])
def test_invalid_quote_parts_are_rejected(doc, tmp_path, quote, reason):
    result, _ = run(doc, {"rules": [rule(quote)]}, tmp_path=tmp_path)
    c = result.candidates[0]
    assert not c.accepted and c.citation.status == "failed" and reason in c.citation.reason
    assert result.rules == []


def test_more_than_one_crossed_artifact_is_rejected():
    raw = "Alpha text that ends here\nHEADER ONE\nHEADER TWO\nbegins here the second part.\n"
    a1, a2 = raw.index("HEADER ONE"), raw.index("HEADER TWO")
    s2 = raw.index("begins")
    view = SourceView(text="(unused)", artifacts=(Artifact(a1, a2, raw[a1:a2]), Artifact(a2, s2, raw[a2:s2])),
                      segments=(Segment(1, 0, 0, a1), Segment(2, 0, s2, len(raw) - s2)))
    check, _ = verify_quote_parts([QuotePart(segment_id=1, quoted_text="Alpha text that ends here"),
                                   QuotePart(segment_id=2, quoted_text="begins here the second part.")], raw, view)
    assert check.status == "failed" and "cross 2 page artifacts" in check.reason


def test_raw_source_and_artifact_provenance_are_preserved(doc, tmp_path):
    before = doc.body
    result, _ = run(doc, {"rules": [rule(parts(TAIL, HEAD))]}, tmp_path=tmp_path)
    assert doc.body == before
    sv = result.source_view
    assert [(s["raw_start"], s["raw_end"]) for s in sv["segments"]] == \
        [(s.raw_start, s.raw_end) for s in doc.view.segments]
    assert all(doc.body[a["raw_start"]:a["raw_end"]] == a["text"] for a in sv["artifacts"])
    assert result.candidates[0].citation.crossed_artifacts[0] in sv["artifacts"]


def test_global_exemption_quoted_across_a_page_break_is_verified_and_propagated(doc, tmp_path):
    scope = {"id": "S1", "kind": "exemption", "statement": "Units owned by natural persons with notice are exempt.",
             "citation": "Code § 3(l)", "governs": None,
             "evidence_parts": parts(EXEMPT_TAIL, EXEMPT_HEAD, first_segment=3)}
    result, _ = run(doc, {"global_scope": [scope], "rules": [rule(parts(TAIL, HEAD))]}, tmp_path=tmp_path)
    entry = result.global_scope[0]
    assert entry["propagated"] and entry["evidence_check"]["reconstructed"]
    assert "Units owned by natural persons with notice are exempt. [Code § 3(l); document-wide]" == \
        result.rules[0]["exemptions"]
    assert result.candidates[0].propagated_scope == [{"id": "S1", "kind": "exemption", "applied": True,
                                                      "basis": "document-wide"}]


# ------------------------------------------- coverage closure and targeted repair

D052_RETURN_SPAN = "The lessor shall, within thirty days after the termination of occupancy under a tenancy-at-will"
RETURN_RULE = make_candidate(citation="M.G.L. c. 186, § 15B(4)", quoted_span=D052_RETURN_SPAN, title="Return",
                             requirement="The lessor must return the deposit within thirty days.", key_value="30 days")
PRIMARY = {"provisions": [provision("§ 15B(1)(b)", rule_indices=[0]), provision("§ 15B(4)"),
                          provision("§ 15B(9)", scope="out_of_scope")],
           "global_scope": [], "rules": [make_candidate()]}


def test_one_targeted_repair_pass_covers_uncovered_provisions(run_fake):
    off_target = make_candidate(title="Not requested")               # cites § 15B(1)(b)(iii): not requested
    result, provider = run_fake(PRIMARY, {"rules": [RETURN_RULE, off_target]})
    assert len(provider.calls) == 2
    repair_call = provider.calls[1]
    assert repair_call["system_instruction"] == REPAIR_SYSTEM_INSTRUCTION != SYSTEM_INSTRUCTION
    assert "- § 15B(4):" in repair_call["prompt"] and "- § 15B(1)(b):" not in repair_call["prompt"]
    assert list(repair_call["response_json_schema"]["properties"]) == ["rules"]
    rp = result.repair
    assert (rp.invoked, rp.requested[0]["ref"], rp.candidate_indices, rp.accepted_count, rp.rejected_count) == \
        (True, "§ 15B(4)", [1, 2], 1, 1)
    repaired, off = result.candidates[1], result.candidates[2]
    assert repaired.origin == "repair" and repaired.accepted and repaired.citation.status == "exact_match"
    assert not off.accepted and "repair: candidate does not cite a requested" in off.rejection_reasons[-1]
    assert result.coverage["after_primary"]["uncovered"] == ["§ 15B(4)"]
    assert result.coverage["after_repair"] == {"uncovered": [], "unaccepted": []}
    assert result.document_status == "complete" and result.accepted_count == 2


def test_there_is_never_a_second_repair_pass(run_fake, d052, cache):
    result, provider = run_fake(PRIMARY, {"rules": []}, cache=cache)
    assert len(provider.calls) == 2 and result.repair.invoked and result.repair.candidate_indices == []
    assert result.coverage["after_repair"]["uncovered"] == ["§ 15B(4)"]
    assert result.document_status == "review_required"
    assert any("no candidate record: ['§ 15B(4)']" in r for r in result.review_reasons)
    offline = extract_document(d052, provider_name="fake", model="fake-model", cache=cache)   # both cached
    assert offline.cache_hit and offline.repair.cache_hit and offline.rules == result.rules


def test_repair_that_cannot_run_is_reported_not_skipped(run_fake, d052, cache):
    run_fake(PRIMARY, cache=cache, repair=False)                     # only the primary response is cached
    offline = extract_document(d052, provider_name="fake", model="fake-model", cache=cache)
    assert offline.repair is not None and not offline.repair.invoked
    assert offline.document_status == "review_required"
    assert any("no cached repair response" in r for r in offline.review_reasons)


def test_repair_provider_failure_keeps_primary_results(run_fake):
    class FailsOnRepair(FakeProvider):
        def generate(self, **kwargs):
            if self.calls:
                self.calls.append(kwargs)
                raise ProviderError("503 UNAVAILABLE (simulated)")
            return super().generate(**kwargs)

    result, provider = run_fake(None, provider=FailsOnRepair(PRIMARY))
    assert len(provider.calls) == 2 and result.accepted_count == 1
    assert result.repair.invoked and "503" in result.repair.errors[0]
    assert result.document_status == "review_required"


REMEDIES_DOC = ("§9 Remedies\n"
                "(a) A tenant may file an action against a landlord in a court of competent jurisdiction.\n"
                "(b) A tenant may seek injunctive relief and money damages in a civil action.\n"
                "(A) Nested item about punitive damages in egregious cases.\n"
                "(B) Nested item about equitable relief in other cases.\n"
                "(c) A tenant may raise any violation as an affirmative defense to an eviction.\n"
                "(d) The City may enforce this section, including through civil penalties.\n"
                "§10 Notices\n"
                "(e) An unrelated label in the next section about notice delivery rules.\n")
REMEDY_B = "A tenant may seek injunctive relief and money damages in a civil action."
REMEDY_C = "A tenant may raise any violation as an affirmative defense to an eviction."


@pytest.mark.parametrize("parent_cited, expected", [(False, [{"ref": "§ 9", "cited": ["b", "c"],
                                                              "unrecorded": ["a", "d"]}]), (True, [])])
def test_parent_covered_only_in_part_is_never_reported_complete(tmp_path, parent_cited, expected):
    rules = [rule(parts(REMEDY_B), citation="§ 9(b)"), rule(parts(REMEDY_C), citation="§ 9(c)")]
    if parent_cited:
        rules.append(rule(parts("(a) A tenant may file an action against a landlord"), citation="§ 9"))
    response = {"provisions": [provision("§ 9", category="just_cause_eviction", rule_indices=[0, 1])],
                "global_scope": [], "rules": rules}
    result, provider = run(synthetic_source(REMEDIES_DOC), response, tmp_path=tmp_path)
    assert len(provider.calls) == 1 and result.repair is None          # a guard, not a repair trigger
    assert result.coverage["final"]["uncovered"] == [] and result.coverage["unrecorded_subdivisions"] == expected
    assert result.document_status == ("complete" if parent_cited else "review_required")
    if not parent_cited:
        assert any("§ 9: (a), (d)" in r for r in result.review_reasons)


# ------------------------------------------------ effective-date evidence

HISTORY_DOC = ("Sec. 5. A landlord shall give each tenant a copy of the tenant protection guide at move-in.\n"
               "(Added 3-1-2020 by Ord. 1234; effective 4-1-2020.)\n"
               "Sec. 6. This section shall take effect on January 1, 2027.\n"
               "Sec. 7. This act takes effect on the first day of the twelfth month following enactment.\n"
               "Sec. 8. This duty does not apply until the agency's online filing system is operational.\n")
GUIDE = "A landlord shall give each tenant a copy of the tenant protection guide at move-in."


@pytest.mark.parametrize("model_kind", ["history_note", "explicit_operative_date"])
def test_history_note_never_populates_effective_date(tmp_path, model_kind):
    candidate = rule(parts(GUIDE), effective_date="2020-04-01", effective_date_evidence="effective 4-1-2020",
                     effective_date_evidence_kind=model_kind)
    result, _ = run(synthetic_source(HISTORY_DOC), {"rules": [candidate]}, tmp_path=tmp_path)
    c, record = result.candidates[0], result.rules[0]
    assert c.accepted and (record["effective_date"], record["status"]) == (None, "in_force")
    assert c.temporal["applied_kind"] == "history_note"
    assert c.temporal["history_evidence"] == "(Added 3-1-2020 by Ord. 1234; effective 4-1-2020.)"
    assert any("history" in w for w in c.warnings)


@pytest.mark.parametrize("evidence, kind, eff, as_of, accepted, status, published, reason", [
    ("This section shall take effect on January 1, 2027.", "explicit_operative_date", "2027-01-01",
     date(2026, 10, 1), True, "not_yet_effective", "2027-01-01", None),
    ("This section shall take effect on January 1, 2027.", "explicit_operative_date", "2027-01-01",
     date(2027, 2, 1), True, "in_force", "2027-01-01", None),
    ("This section shall take effect on January 1, 2027.", "explicit_operative_date", "2027-02-01",
     date(2026, 10, 1), False, None, None, "is not written in its verified evidence"),
    ("This section shall take effect on January 1, 2027.", "explicit_operative_date", None,
     date(2026, 10, 1), False, None, None, "without an effective_date"),
    ("the first day of the twelfth month following enactment", "relative_date_formula", None,     # D069 behaviour
     date(2026, 10, 1), False, None, None, "relative or conditional terms"),
    ("the first day of the twelfth month following enactment", "relative_date_formula", "2027-10-01",
     date(2026, 10, 1), False, None, None, "relative or conditional terms"),
    ("the first day of the twelfth month following enactment", "history_note", None,             # mislabelled
     date(2026, 10, 1), False, None, None, "neither history-note structure nor a calendar date"),
    ("does not apply until the agency's online filing system is operational", "operative_condition", None,
     date(2026, 10, 1), True, "in_force", None, None),
    ("This section shall take effect on January 1, 2027.", None, "2027-01-01",
     date(2026, 10, 1), False, None, None, "not classified"),
    ("This section shall take effect on January 1, 2026.", "explicit_operative_date", "2026-01-01",  # fabricated
     date(2026, 10, 1), False, None, None, "effective_date_evidence not found"),
])
def test_effective_date_evidence_classification(tmp_path, evidence, kind, eff, as_of, accepted, status,
                                                published, reason):
    candidate = rule(parts(GUIDE), effective_date=eff, effective_date_evidence=evidence,
                     effective_date_evidence_kind=kind)
    result, _ = run(synthetic_source(HISTORY_DOC), {"rules": [candidate]}, tmp_path=tmp_path, as_of=as_of)
    c = result.candidates[0]
    assert c.accepted is accepted
    if accepted:
        assert (c.rule["status"], c.rule["effective_date"]) == (status, published)
    else:
        assert any(reason in r for r in c.rejection_reasons), c.rejection_reasons


def test_operative_condition_evidence_becomes_an_unresolved_condition(tmp_path):
    candidate = rule(parts(GUIDE), effective_date_evidence="does not apply until the agency's online filing "
                     "system is operational", effective_date_evidence_kind="operative_condition")
    result, _ = run(synthetic_source(HISTORY_DOC), {"rules": [candidate]}, tmp_path=tmp_path)
    c = result.candidates[0]
    assert "Operative condition (unresolved" in c.rule["coverage_conditions"]
    assert c.operative_conditions[0]["from"] == "effective_date_evidence"
    assert any("unresolved operative condition" in w for w in c.warnings)
