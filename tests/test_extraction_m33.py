"""M3.3: validity windows (start vs end dates), layout-only citation normalization and
conservative same-source deduplication. Synthetic documents only; no network."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import FakeProvider, codified, make_candidate, parts, provision, synthetic_source
from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache
from navigator.extraction.citation import verify_span
from navigator.extraction.corpus import integrity_violations
from navigator.extraction.extractor import extract_document

# ----------------------------------------------------------------- shared synthetic document

RULE = "A landlord shall not increase rent by more than 5 percent in any year."
FREEZE = "Effective March 30, 2020, through January 31, 2024, rent increases are prohibited for covered units."
NOTE = ("(Amended by Stats. 2025, Ch. 9, Sec. 1. Effective January 1, 2026. Repealed as of January 1, 2030, "
        "by its own provisions.)")
CODE = (f"Civil Code Section 900\n(a) {RULE}\n(b) {FREEZE}\n"
        "(c) This section shall remain in effect only until January 1, 2030, and as of that date is repealed.\n"
        f"{NOTE}\n")


def run(body, rules, *more, as_of=date(2026, 10, 1), inventory=None, tmp_path, cache=None):
    source = synthetic_source(body)
    response = {"provisions": inventory if inventory is not None else
                [provision("§ 900(a)", "in_scope", "rent_increase_limits", anchor=RULE),
                 provision("§ 900(b)", "in_scope", "rent_increase_limits", id="P2", anchor=FREEZE)],
                "global_scope": [], "rules": rules, "no_rules_justification": None}
    provider = FakeProvider(response, *more, document=codified(source))
    result = extract_document(source, provider_name=provider.name, model=provider.model, provider=provider,
                              cache=cache or ResponseCache(tmp_path / "cache"), as_of=as_of)
    return result, source, provider


def rule(quote, citation="§ 900(a)", pid="P1", **overrides):
    return make_candidate(**{"quote_parts": parts(quote), "citation": citation, "provision_ids": [pid],
                             "category": "rent_increase_limits", "key_value": None, "coverage_conditions": None,
                             "requirement": "A landlord must follow this rule.", **overrides})


# ----------------------------------------------------------------- TEMPORAL

def test_future_repeal_date_is_an_end_not_a_future_start(tmp_path):
    result, _, _ = run(CODE, [rule(RULE, version_note="amended", version_evidence=NOTE)], tmp_path=tmp_path)
    c = result.candidates[0]
    assert c.accepted and result.rules[0]["status"] == "in_force"
    kinds = {(b["kind"], b["date"]) for b in c.validity["boundaries"]}
    assert {("amendment_history", "2026-01-01"), ("repealed_on", "2030-01-01")} <= kinds
    assert c.validity["end_exclusive"] == "2030-01-01" and c.validity["decision"]["state"] == "current_until"


@pytest.mark.parametrize("as_of, published, state", [
    (date(2026, 10, 1), True, "in_force"),
    (date(2029, 12, 31), True, "in_force"),
    (date(2030, 1, 1), False, "repealed"),       # "repealed as of" a date: not in force on that date
])
def test_end_boundary_keeps_the_current_rule_in_force_only_before_it(as_of, published, state, tmp_path):
    result, _, _ = run(CODE, [rule(RULE, version_note="amended", version_evidence=NOTE)], as_of=as_of,
                       tmp_path=tmp_path)
    c = result.candidates[0]
    assert (c.accepted, c.historical, c.temporal_state) == (published, not published, state)


def test_expired_rule_is_not_currently_publishable_but_kept_in_the_audit(tmp_path):
    result, source, _ = run(CODE, [rule(RULE), rule(FREEZE, citation="§ 900(b)", pid="P2",
                                                     effective_date="2020-03-30", effective_date_evidence=FREEZE,
                                                     effective_date_evidence_kind="explicit_operative_date")],
                            tmp_path=tmp_path)
    current, expired = result.candidates
    assert [r["citation"] for r in result.rules] == ["§ 900(a)"]
    assert expired.historical and not expired.accepted and not expired.held
    assert expired.temporal_state == "expired" and expired.rule["status"] is None
    assert expired.rule["quoted_span"] == FREEZE and expired.rule["effective_date"] == "2020-03-30"
    end = next(b for b in expired.validity["boundaries"] if b["role"] == "end")
    assert (end["kind"], end["date"], end["end_exclusive"]) == ("valid_through", "2024-01-31", "2024-02-01")
    assert "through January 31, 2024" in source.body[end["raw_start"]:end["raw_end"]]   # exact raw offsets
    assert result.document_status == "complete"     # a verified historical rule is not a review problem
    assert not integrity_violations(result, source, ResponseCache(tmp_path / "cache"), "fake", "fake-model",
                                    result.generation_settings, sp.REPO_ROOT / sp.SCHEMA_PATH)


def test_an_end_that_cannot_be_established_safely_holds_the_record(tmp_path):
    until = "This rule remains in effect until October 1, 2026."
    body = f"Civil Code Section 900\n(a) {RULE} {until}\n"
    quote = f"{RULE} {until}"
    on_the_day, _, _ = run(body, [rule(quote)], tmp_path=tmp_path / "a")
    assert on_the_day.candidates[0].held                     # "until" a date: that day itself is ambiguous
    before, _, _ = run(body, [rule(quote)], as_of=date(2026, 9, 30), tmp_path=tmp_path / "b")
    assert before.candidates[0].accepted
    two = f"{FREEZE} The allowance is valid through June 30, 2025."
    conflicting, _, _ = run(f"Civil Code Section 900\n(a) {RULE}\n(b) {two}\n", [rule(two, "§ 900(b)", "P2")],
                            tmp_path=tmp_path / "c")
    c = conflicting.candidates[0]
    assert c.held and "different end boundaries" in c.status_derivation


def test_unclassified_future_date_in_version_evidence_holds_instead_of_guessing(tmp_path):
    note = "(Ord. 12 adopted March 3, 2027.)"
    result, _, _ = run(f"Civil Code Section 900\n(a) {RULE}\n{note}\n", [rule(RULE, version_note="x",
                                                                             version_evidence=note)],
                       tmp_path=tmp_path)
    c = result.candidates[0]
    assert c.held and "does not classify" in c.status_derivation


# ----------------------------------------------------------------- CITATION

AMENDED = "(Amended by Stats. 2023, Ch. 776, Sec. 1.   (SB 267)   Effective January 1, 2024.)"


@pytest.mark.parametrize("model_text", [
    "( Amended by Stats. 2023, Ch. 776, Sec. 1.   (SB 267)   Effective January 1, 2024. )",
    "(Amended by Stats. 2023, Ch. 776, Sec. 1. ( SB 267 ) Effective January 1, 2024.)",
])
def test_whitespace_next_to_parentheses_or_brackets_is_layout_only(model_text):
    c = verify_span(model_text, f"Text before.\n{AMENDED}\nText after.")
    assert c.status == "layout_normalized_match" and c.source_span == AMENDED


@pytest.mark.parametrize("model_text", [
    "(Amendedby Stats. 2023, Ch. 776",        # a space between words removed
    "(Amended by Stats 2023, Ch. 776",         # punctuation dropped
    "(Amended by Stats. 2023, Ch. 777",        # number changed
    "[Amended by Stats. 2023, Ch. 776",        # bracket character changed
    "Amended by Stats. 2023, Ch. 776, Sec. 1. Effective January 1, 2024.",   # words omitted
])
def test_no_other_difference_is_tolerated(model_text):
    assert verify_span(model_text, f"Text before.\n{AMENDED}\nText after.").status == "failed"


def test_inserted_bracket_characters_never_match():
    raw = "two or more persons to engage in\n1\nor\notherwise facilitate\n1\nparallel pricing"
    assert verify_span("engage in 1 [or otherwise facilitate] 1 parallel", raw).status == "failed"


def test_layout_matched_quote_publishes_the_exact_raw_span(tmp_path):
    raw_quote = "A landlord shall not raise rent (except as permitted by Section 901) more than once a year."
    body = f"Civil Code Section 900\n(a) {raw_quote}\n"
    model = "A landlord shall not raise rent ( except as permitted by Section 901 ) more than once a year."
    result, source, _ = run(body, [rule(model)], inventory=[provision("§ 900(a)", "in_scope", "rent_increase_limits",
                                                                      anchor=raw_quote)], tmp_path=tmp_path)
    c = result.candidates[0]
    assert c.accepted and c.citation.status == "layout_normalized_match"
    assert result.rules[0]["quoted_span"] == raw_quote and raw_quote in source.body
    assert c.citation.model_span == model                     # the model's own text stays in the audit


# ----------------------------------------------------------------- DEDUPE

def test_identical_same_source_passage_is_published_once(tmp_path):
    only_a = [provision("§ 900(a)", "in_scope", "rent_increase_limits", anchor=RULE)]
    result, source, provider = run(CODE, [rule(RULE), rule(RULE, citation="Civil Code Section 900(a)",
                                                           title="Annual rent increase cap")],
                                   inventory=only_a, tmp_path=tmp_path)
    assert len(provider.calls) == 1 and len(result.rules) == 1 and result.rules[0]["citation"] == "§ 900(a)"
    kept, dup = result.candidates
    assert dup.duplicate_of == kept.index and not dup.accepted
    entry = result.dedupe[0]
    assert (entry["kept_team_rule_id"], entry["suppressed_team_rule_id"]) == (kept.rule["team_rule_id"],
                                                                             dup.rule["team_rule_id"])
    assert entry["provenance"]["suppressed"]["citation"] == "Civil Code Section 900(a)"


def test_primary_and_repair_duplicate_publishes_once_and_the_target_stays_resolved(tmp_path):
    inventory = [provision("§ 900(a)", "in_scope", "rent_increase_limits", anchor=RULE),
                 provision("Rent cap", "in_scope", "rent_increase_limits", id="P3", anchor=RULE)]
    repair = {"target_resolutions": [{"ref": "Rent cap", "scope": "in_scope", "reason": "It caps rent.",
                                      "evidence_parts": parts(RULE)}],
              "scope_mappings": [], "rules": [rule(RULE, citation="Rent cap", pid="P3")]}
    result, _, provider = run(CODE, [rule(RULE)], repair, inventory=inventory, tmp_path=tmp_path)
    assert len(provider.calls) == 2 and len(result.rules) == 1
    primary, repaired = result.candidates
    assert (primary.origin, repaired.origin, repaired.duplicate_of) == ("primary", "repair", primary.index)
    assert result.dedupe[0]["provenance"]["kept"]["origin"] == "primary"
    assert result.repair.targets[0]["final_resolution"] == "resolved_by_accepted_rule"
    assert result.document_status == "complete"


def test_different_passages_are_never_auto_merged(tmp_path):
    table = "Allowable increase Effective Period March 1, 2026 - February 28, 2027 1.6%"
    prose = "The allowable increase for March 1, 2026 to February 28, 2027 is 1.6%."
    body = f"Civil Code Section 900\n(a) {table}\n(b) {prose}\n"
    inventory = [provision("§ 900(a)", "in_scope", "rent_increase_limits", anchor=table),
                 provision("§ 900(b)", "in_scope", "rent_increase_limits", id="P2", anchor=prose)]
    result, _, _ = run(body, [rule(table), rule(prose, "§ 900(b)", "P2")], inventory=inventory, tmp_path=tmp_path)
    assert len(result.rules) == 2 and not result.dedupe
