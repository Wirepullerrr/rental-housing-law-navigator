"""M3.3 large-document mode (chunked.py): structural chunking, per-chunk extraction with the
v7 semantics, document-level merge, cross-chunk scope logic, excerpt-only repair and the
failure mode. Synthetic documents only; no network."""

from __future__ import annotations

import pytest

from conftest import FakeProvider, make_candidate, parts, provision, synthetic_source
from navigator.extraction.cache import ResponseCache
from navigator.extraction.chunked import chunk_view, extract_large_document, merge_payloads, plan_chunks
from navigator.extraction.corpus import IntegrityViolation, MeteredProvider, SpendLedger
from navigator.extraction.provider import ProviderError, ProviderResult
from navigator.extraction.quotes import verify_quote_parts
from navigator.extraction.models import QuotePart

FILL = "This sentence is explanatory filler about the history of the local housing program and its goals."
RENT = "A landlord shall not increase rent by more than 5 percent in any year."
DEPOSIT = "A landlord shall not charge a deposit above one month of rent."
NOTICE = "A landlord shall give 30 days written notice of any change in deposit terms."
REFS = "Sections 1 and 2 do not apply to an owner who lives in the building."
LOCAL = "This section does not apply to units built after 2010."
BODY = ("Housing Code of Example City\n\n"
        f"Section 1. Rent limits\n{RENT}\n" + "\n".join([FILL] * 4) + "\n\n"
        f"Section 2. Deposits\n{DEPOSIT}\n{NOTICE}\n" + "\n".join([FILL] * 4) + "\n\n"
        f"Section 3. Exemptions\n{REFS}\n{LOCAL}\n" + "\n".join([FILL] * 4) + "\n")
MAX, MIN = 700, 300


def doc():
    return synthetic_source(BODY)


def chunk_response(provisions, rules=(), scope=(), justification=None):
    return {"document": {"posture": "codified_current_law", "evidence": "Housing Code of Example City"},
            "provisions": list(provisions), "global_scope": list(scope), "rules": list(rules),
            "no_rules_justification": justification}


def rule(quote, citation, pid="P1", category="rent_increase_limits"):
    return make_candidate(quote_parts=parts(quote), citation=citation, provision_ids=[pid], category=category,
                          key_value=None, coverage_conditions=None, requirement="A landlord must follow this rule.")


def condition(sid, quote, words, source="P1"):
    return {"id": sid, "kind": "exemption", "statement": "Some owners are exempt.", "citation": "Section 3",
            "scope_mode": "structural", "scope_quote": words, "source_provision_id": source,
            "governed_provision_ids": None, "evidence_parts": parts(quote)}


C1 = chunk_response([provision("§ 1", "in_scope", "rent_increase_limits", anchor=RENT)], [rule(RENT, "§ 1")])
C2 = chunk_response([provision("§ 2", "in_scope", "security_deposits", anchor=DEPOSIT)],
                    [rule(DEPOSIT, "§ 2", category="security_deposits")])
C3 = chunk_response([provision("§ 3", "out_of_scope", anchor=REFS, role="exemption")], [],
                    [condition("S1", REFS, "Sections 1 and 2 do not apply to"),
                     condition("S2", LOCAL, "This section does not apply to")])


def run(*responses, tmp_path, **kwargs):
    source = doc()
    provider = FakeProvider(*responses)
    result = extract_large_document(source, provider_name=provider.name, model=provider.model, provider=provider,
                                    cache=ResponseCache(tmp_path / "cache"), max_chars=MAX, min_chars=MIN, **kwargs)
    return result, source, provider


# ----------------------------------------------------------------- chunking

def test_chunks_tile_the_raw_text_exactly_with_stable_ids_and_offsets():
    source = doc()
    chunks = plan_chunks("DTEST", source.body, source.view, MAX, MIN)
    assert chunks == plan_chunks("DTEST", source.body, source.view, MAX, MIN)          # deterministic
    assert [c.id for c in chunks] == [f"DTEST:c{n:02d}" for n in range(1, len(chunks) + 1)]
    assert chunks[0].raw_start == 0 and chunks[-1].raw_end == len(source.body)
    assert all(a.raw_end == b.raw_start for a, b in zip(chunks, chunks[1:]))         # each char exactly once
    assert "".join(source.body[c.raw_start:c.raw_end] for c in chunks) == source.body   # nothing discarded
    assert all(c.raw_end - c.raw_start <= MAX for c in chunks)


def test_chunks_are_cut_at_section_headings():
    source = doc()
    chunks = plan_chunks("DTEST", source.body, source.view, MAX, MIN)
    assert [c.boundary for c in chunks[1:]] == ["heading"] * (len(chunks) - 1)
    assert [source.body[c.raw_start:].split("\n")[0] for c in chunks[1:]] == ["Section 2. Deposits",
                                                                           "Section 3. Exemptions"]
    assert chunks[2].heading == "Section 2. Deposits"     # context: the heading BEFORE the excerpt


def test_long_unbroken_text_is_cut_hard_but_never_truncated():
    body = "x" * 2500
    chunks = plan_chunks("DTEST", body, None, 1000, 500)
    assert [c.boundary for c in chunks] == ["document_start", "hard", "hard"]
    assert sum(c.raw_end - c.raw_start for c in chunks) == len(body)


def test_chunk_view_keeps_document_segment_numbers_so_quotes_verify_against_raw():
    source = doc()
    c2 = plan_chunks("DTEST", source.body, source.view, MAX, MIN)[1]
    view = chunk_view(source.view, source.body, c2.raw_start, c2.raw_end)
    assert view.startswith("[[SEGMENT 1]]\nSection 2. Deposits")
    check, _ = verify_quote_parts([QuotePart(segment_id=1, quoted_text=DEPOSIT)], source.body, source.view)
    assert check.status == "exact_match" and source.body[check.start:check.end] == DEPOSIT


# ----------------------------------------------------------------- merge and scope

def test_merge_namespaces_ids_and_narrows_a_chunk_level_document_scope():
    source = doc()
    chunks = plan_chunks("DTEST", source.body, source.view, MAX, MIN)
    merged = merge_payloads(list(zip(chunks, [C1, C2, C3])))
    assert [p["id"] for p in merged["provisions"]] == ["c01.P1", "c02.P1", "c03.P1"]
    assert [r["provision_ids"] for r in merged["rules"]] == [["c01.P1"], ["c02.P1"]]
    assert [s["governed_provision_ids"] for s in merged["global_scope"]] == [["c03.P1"], ["c03.P1"]]


def test_document_level_merge_and_cross_chunk_scope_only_where_verified(tmp_path):
    result, source, provider = run(C1, C2, C3, tmp_path=tmp_path)
    assert len(provider.calls) == 3                                  # one request per chunk, no repair
    assert all("this request covers EXCERPT" in c["prompt"] for c in provider.calls)
    assert [r["citation"] for r in result.rules] == ["§ 1", "§ 2"]
    s1, s2 = result.global_scope
    assert s1["scope"]["mode"] == "explicit_reference"               # verified at document level
    assert sorted(s1["scope"]["propagate_ids"]) == ["c01.P1", "c02.P1"]
    applied = [[p["id"] for p in c.propagated_scope if p["applied"]] for c in result.candidates]
    assert applied == [["c03.S1"], ["c03.S1"]]                       # S2 ("this section") never left chunk 3
    assert result.chunking["tiling"] == "exact" and result.document_status == "complete"


def test_repair_is_one_request_built_from_the_relevant_excerpts_only(tmp_path):
    c2 = chunk_response([provision("§ 2", "in_scope", "security_deposits", anchor=DEPOSIT),
                         provision("§ 2 notice", "in_scope", "security_deposits", id="P2", anchor=NOTICE)],
                        [rule(DEPOSIT, "§ 2", category="security_deposits")])
    repair = {"target_resolutions": [{"ref": "§ 2 notice", "scope": "in_scope", "reason": "A notice duty.",
                                      "evidence_parts": parts(NOTICE)}],
              "scope_mappings": [], "rules": [rule(NOTICE, "§ 2 notice", "c02.P2", category="security_deposits")]}
    result, _, provider = run(C1, c2, C3, repair, tmp_path=tmp_path)
    assert len(provider.calls) == 4 and result.repair.invoked
    prompt = provider.calls[3]["prompt"]
    assert "excerpt 2 (raw" in prompt and NOTICE in prompt and RENT not in prompt and REFS not in prompt
    assert result.chunking["repair_chunks"] == ["DTEST:c02"]
    assert [r["citation"] for r in result.rules] == ["§ 1", "§ 2", "§ 2 notice"]


class FailingOnce(FakeProvider):
    def generate(self, **kwargs):
        if len(self.calls) == 1:
            self.calls.append(kwargs)
            raise ProviderError("HTTP 500 after 3 attempts")
        return super().generate(**kwargs)


def test_a_failed_chunk_keeps_the_other_chunks_and_requires_review(tmp_path):
    source = doc()
    provider = FailingOnce(C1, C2, C3)
    result = extract_large_document(source, provider_name=provider.name, model=provider.model, provider=provider,
                                    cache=ResponseCache(tmp_path / "cache"), max_chars=MAX, min_chars=MIN)
    statuses = [c["status"] for c in result.chunking["chunks"]]
    assert statuses[0] == "extracted" and statuses[1].startswith("provider failure")
    assert result.document_status == "review_required"
    assert any("excerpt(s) not extracted (DTEST:c02)" in r for r in result.review_reasons)
    assert [r["citation"] for r in result.rules] == ["§ 1"]          # validated records are kept


# ----------------------------------------------------------------- metering

class Inner:
    name, model = "fake", "fake-model"

    def generate(self, **kwargs):
        return ProviderResult(text="{}", metadata={"usage": {"input_tokens": 1000, "output_tokens": 100}})


def test_metered_provider_allows_one_request_per_excerpt_and_one_repair(tmp_path):
    from navigator.extraction.prompt import REPAIR_SYSTEM_INSTRUCTION, SYSTEM_INSTRUCTION
    metered = MeteredProvider(Inner(), SpendLedger(tmp_path / "ledger.json"), budget_usd=1.0)
    metered.doc_id = "DTEST"
    call = lambda system, prompt: metered.generate(system_instruction=system, prompt=prompt,  # noqa: E731
                                                   response_json_schema={}, settings={})
    for k in (1, 2):
        call(SYSTEM_INSTRUCTION, f"... this request covers EXCERPT {k} of 2 of the document ...")
    call(REPAIR_SYSTEM_INSTRUCTION, "repair")
    with pytest.raises(IntegrityViolation):
        call(SYSTEM_INSTRUCTION, "... this request covers EXCERPT 2 of 2 of the document ...")
    with pytest.raises(IntegrityViolation):
        call(REPAIR_SYSTEM_INSTRUCTION, "repair again")
    assert [e["chunk"] for e in metered.ledger.entries] == [1, 2, None]
