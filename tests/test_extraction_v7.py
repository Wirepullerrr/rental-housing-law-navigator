"""Prompt v7 / M3.2: scope modes (structural / explicit_reference / named_subject), named-subject
scope_mapping_challenges in the single repair pass, and deterministic legislative status
(Massachusetts session resolver). Synthetic documents only; no network."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import FakeProvider, codified, make_candidate, parts, provision, synthetic_source
from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache
from navigator.extraction.corpus import integrity_violations
from navigator.extraction.extractor import extract_document, prepare_repair_request
from navigator.extraction.legislative import decide, find_session
from navigator.extraction.prompt import REPAIR_SYSTEM_INSTRUCTION
from navigator.extraction.scope import classify


def run(source, *responses, tmp_path, cache=None, **kwargs):
    provider = FakeProvider(*responses, document=kwargs.pop("document", codified(source)))
    result = extract_document(source, provider_name=provider.name, model=provider.model, provider=provider,
                              cache=cache or ResponseCache(tmp_path / "cache"), **kwargs)
    return result, provider


def rule(quote, citation, pid, category="rent_increase_limits", **overrides):
    return make_candidate(**{"quote_parts": parts(quote), "citation": citation, "provision_ids": [pid],
                             "category": category, "key_value": None, "coverage_conditions": None,
                             "requirement": "A landlord must follow this rule.", **overrides})


def condition(quote_text, scope_quote, governed, mode, source=None, sid="S1"):
    return {"id": sid, "kind": "exemption", "statement": "Some units are exempt.", "citation": "Code",
            "scope_mode": mode, "scope_quote": scope_quote, "source_provision_id": source,
            "governed_provision_ids": governed, "evidence_parts": parts(quote_text)}


# ----------------------------------------------------------- scope modes

CODE = ("§ 5 Rent limits\n"
        "(a) A landlord shall not increase rent by more than 5 percent in any year.\n"
        "(b) A landlord shall give 30 days notice of any rent increase.\n"
        "(c) The provisions of this section do not apply to units built after 2010.\n"
        "§ 6 Relocation\n"
        "A landlord who ends a tenancy for no fault shall pay one month of rent as relocation aid.\n"
        "§ 7 Deposits\n"
        "A landlord shall not charge a deposit above one month of rent.\n"
        "§ 8 Exemptions\n"
        "Sections 6 and 7 do not apply to an owner who lives in the building.\n"
        "The rent limit does not apply to units owned by a nonprofit housing provider.\n")
A = "A landlord shall not increase rent by more than 5 percent in any year."
B = "A landlord shall give 30 days notice of any rent increase."
C = "The provisions of this section do not apply to units built after 2010."
RELOC = "A landlord who ends a tenancy for no fault shall pay one month of rent as relocation aid."
DEPOSIT = "A landlord shall not charge a deposit above one month of rent."
REFS = "Sections 6 and 7 do not apply to an owner who lives in the building."
NAMED = "The rent limit does not apply to units owned by a nonprofit housing provider."
INVENTORY = [provision("§ 5(a)", "in_scope", "rent_increase_limits", anchor=A),
             provision("§ 5(b)", "in_scope", "rent_increase_limits", id="P2", anchor=B),
             provision("§ 5(c)", "out_of_scope", id="P3", anchor=C, role="exemption"),
             provision("§ 6", "in_scope", "just_cause_eviction", id="P4", anchor=RELOC),
             provision("§ 7", "in_scope", "security_deposits", id="P5", anchor=DEPOSIT),
             provision("§ 8", "out_of_scope", id="P6", anchor=REFS, role="exemption")]
RULES = [rule(A, "§ 5(a)", "P1"), rule(B, "§ 5(b)", "P2"),
         rule(RELOC, "§ 6", "P4", category="just_cause_eviction"),
         rule(DEPOSIT, "§ 7", "P5", category="security_deposits")]


def code_response(*conditions):
    return {"provisions": INVENTORY, "global_scope": list(conditions), "rules": RULES, "no_rules_justification": None}


def exempted(result):
    return {r["citation"]: r["exemptions"] is not None for r in result.rules}


def test_structural_condition_propagates_to_its_verified_container_only(tmp_path):
    s1 = condition(C, "The provisions of this section", ["P1", "P2", "P3"], "structural", source="P3")
    result, provider = run(synthetic_source(CODE), code_response(s1), tmp_path=tmp_path)
    assert result.global_scope[0]["scope"]["mode"] == "structural"
    assert result.global_scope[0]["scope"]["verification"] == "provisions under 5"
    assert exempted(result) == {"§ 5(a)": True, "§ 5(b)": True, "§ 6": False, "§ 7": False}
    assert len(provider.calls) == 1 and result.scope_mappings == []      # no repair call for a structural scope


def test_unverifiable_structural_claim_is_never_propagated_but_challenged(tmp_path):
    s1 = condition(C, "The provisions of this section", ["P1", "P5"], "structural", source="P3")  # not a container
    result, provider = run(synthetic_source(CODE), code_response(s1), tmp_path=tmp_path, repair=False)
    scope = result.global_scope[0]["scope"]
    assert (scope["mode"], scope["fallback_from"]) == ("named_subject", "structural")
    assert [m["provision_id"] for m in result.scope_mappings] == ["P1", "P5"]
    assert not any(exempted(result).values()) and result.document_status == "review_required"


def test_explicit_reference_propagates_only_to_the_referenced_provisions(tmp_path):
    s1 = condition(REFS, "Sections 6 and 7", ["P4", "P5", "P1"], "explicit_reference", source="P6")
    result, provider = run(synthetic_source(CODE), code_response(s1), tmp_path=tmp_path)
    scope = result.global_scope[0]["scope"]
    assert (scope["mode"], scope["detail"], scope["propagate_ids"]) == ("explicit_reference", ["6", "7"], ["P4", "P5"])
    assert "proposed ids outside them are not used: ['P1']" in scope["verification"]
    assert exempted(result) == {"§ 5(a)": False, "§ 5(b)": False, "§ 6": True, "§ 7": True}
    assert len(provider.calls) == 1


def test_named_subject_never_auto_propagates_from_model_ids(tmp_path):
    # The model proposes the narrow rent-limit exemption for the rent limit AND the unrelated notice duty.
    s1 = condition(NAMED, "The rent limit", ["P1", "P2"], "structural", source="P6")   # mislabelled mode
    result, provider = run(synthetic_source(CODE), code_response(s1), tmp_path=tmp_path, repair=False)
    assert result.global_scope[0]["scope"]["mode"] == "named_subject"                   # decided from the words
    assert not any(exempted(result).values())
    assert [m["final"] for m in result.scope_mappings] == ["unresolved", "unresolved"]
    assert any("named-subject scope mappings unresolved" in r for r in result.review_reasons)


@pytest.mark.parametrize("text, mode", [
    ("This Division shall not apply to hotels.", "structural"),
    ("For purposes of this section, the following definitions apply:", "structural"),
    ("short-term occupancy, as defined in and subject to Division 1 of this Code;", "named_subject"),
    ("Sections 98.0704 and 98.0705 shall not apply to", "explicit_reference"),
    ("The rent cap does not apply to new buildings.", "named_subject"),
])
def test_scope_mode_is_decided_from_the_scope_words(text, mode):
    assert classify(text, "§ 98.0703(a)")[0] == mode


def mapping(provision_id, decision, reason="Because the text says so.", evidence=()):
    return {"condition_id": "S1", "provision_id": provision_id, "decision": decision, "reason": reason,
            "evidence_parts": parts(*evidence)}


def named(governed=("P1", "P2")):
    return code_response(condition(NAMED, "The rent limit", list(governed), "named_subject", source="P6"))


@pytest.mark.parametrize("decisions, exemptions, finals, status", [
    ([mapping("P1", "applies"), mapping("P2", "does_not_apply", evidence=[B])],
     {"§ 5(a)": True, "§ 5(b)": False}, ["applies", "does_not_apply"], "complete"),
    ([mapping("P1", "applies"), mapping("P2", "does_not_apply")],                   # no evidence
     {"§ 5(a)": True, "§ 5(b)": False}, ["applies", "unresolved"], "review_required"),
    ([mapping("P1", "applies"), mapping("P2", "uncertain")],
     {"§ 5(a)": True, "§ 5(b)": False}, ["applies", "unresolved"], "review_required"),
    ([mapping("P1", "applies")],                                                     # P2 omitted
     {"§ 5(a)": True, "§ 5(b)": False}, ["applies", "unresolved"], "review_required"),
])
def test_named_subject_mapping_outcomes(tmp_path, decisions, exemptions, finals, status):
    repair = {"target_resolutions": [], "scope_mappings": decisions, "rules": []}
    result, provider = run(synthetic_source(CODE), named(), repair, tmp_path=tmp_path)
    assert len(provider.calls) == 2                                                   # one primary, ONE repair
    assert {k: v for k, v in exempted(result).items() if k in exemptions} == exemptions
    assert [m["final"] for m in result.scope_mappings] == finals
    assert result.document_status == status
    assert not integrity_violations(result, synthetic_source(CODE), ResponseCache(tmp_path / "cache"), "fake",
                                    "fake-model", result.generation_settings, sp.REPO_ROOT / sp.SCHEMA_PATH)


def test_mappings_share_the_single_repair_request_with_other_targets(tmp_path):
    response = named(("P1",))
    response["provisions"] = INVENTORY + [provision("§ 9", "in_scope", "security_deposits", id="P7",
                                                    anchor="§ 9 Receipts")]                      # uncovered
    repair = {"target_resolutions": [{"ref": "§ 9", "scope": "uncertain", "reason": "Unclear.", "evidence_parts": []}],
              "scope_mappings": [mapping("P1", "applies")], "rules": []}
    result, provider = run(synthetic_source(CODE), response, repair, tmp_path=tmp_path)
    assert len(provider.calls) == 2 and provider.calls[1]["system_instruction"] == REPAIR_SYSTEM_INSTRUCTION
    prompt = provider.calls[1]["prompt"]
    assert "- § 9 (provision id P7)" in prompt and "- S1 -> P1:" in prompt
    assert result.repair.cache_key and result.scope_mappings[0]["final"] == "applies"


def test_repair_cache_identity_includes_the_mapping_set(d052):
    pair = [{"condition_id": "S1", "provision_id": "P1", "kind": "exemption", "statement": "x", "scope_words": "x",
             "condition_evidence": "x", "target_ref": "§ 1", "target_anchor": "x", "target_quote": "x"}]
    key = lambda m: prepare_repair_request(d052, "fake", "fake-model", {}, "k", [], [], m)  # noqa: E731
    assert key(pair).key != key([]).key and key(pair).key_fields["scope_mappings"] == [["S1", "P1"]]


# ------------------------------------------------- legislative status (MA)

PRIOR = ("Bill H.100\n193rd (2023 - 2024)\nAn Act relative to rent stabilization in the city\n"
         "Bill History\n4/10/2023 House Referred to the committee on Housing\n"
         "3/4/2024 House No further action taken on the extension to Thursday, April 18, 2024\n"
         "9/9/2024 House Accompanied a study order, see H5035\n"
         "Similar Bills\nH.200 194th (Current) An Act relative to rent stabilization\n")
CURRENT = ("Bill S.300\n194th (Current)\nAn Act relative to limiting rent increases\n"
           "Bill History\n3/12/2026 Senate Accompanied a study order, see S2900\n")
TERMINAL = ("Bill S.301\n194th (Current)\nAn Act relative to limiting rent increases\n"
            "Bill History\n3/12/2026 Senate\nNo further action taken\n")
ENACTED = "Bill H.400\n194th (Current)\nAn Act relative to deposits\nSigned by the Governor, Chapter 12 of the Acts of 2026\n"


@pytest.mark.parametrize("text, model, status, basis", [
    (PRIOR, "pending", "failed", "session_expired"),                        # A + E: refile is a separate bill
    (CURRENT, "pending", "pending", "current_session_pending"),             # B: study order is not terminal
    (CURRENT, "failed", "pending", "current_session_pending"),              # the model may not call it failed
    (TERMINAL, "pending", "failed", "explicit_terminal_action"),            # C
    (ENACTED, "pending", None, None),                                       # D: enactment conflicts -> held
])
def test_massachusetts_legislative_status(text, model, status, basis):
    session = find_session(text, "Boston, MA")
    audit = decide(model, text, session, date(2026, 10, 1))
    assert (audit["status"], audit["status_basis"]) == (status, basis)
    assert session["resolver"] == "ma-general-court-session/v1"


def test_session_resolver_is_massachusetts_only_and_checks_the_label():
    assert find_session(CURRENT, "Newark, NJ") is None
    assert find_session(PRIOR, "MA")["years"] == [2023, 2024] and find_session(PRIOR, "MA")["other_labels"]
    assert not find_session("194th (2023 - 2024)\n", "MA")["consistent"]
    # the 194th General Court is still in session on the challenge query date, even after formal sessions end
    assert decide("pending", CURRENT, find_session(CURRENT, "MA"), date(2026, 8, 1))["status"] == "pending"
    assert decide("enacted", PRIOR, find_session(PRIOR, "MA"), date(2026, 10, 1))["status_basis"] == "enacted"


def test_prior_session_bill_summary_becomes_failed_in_the_pipeline(tmp_path):
    source = synthetic_source(PRIOR)
    source.meta.jurisdiction = "Boston, MA"
    title = "An Act relative to rent stabilization in the city"
    candidate = rule(title, "H.100", "P1", source_basis="official_bill_summary", enactment_status="pending",
                     enactment_status_evidence="9/9/2024 House Accompanied a study order, see H5035",
                     requirement="The bill would authorize rent stabilization in the city.")
    response = {"provisions": [provision("H.100", "in_scope", "rent_increase_limits", anchor=title)],
                "global_scope": [], "rules": [candidate], "no_rules_justification": None}
    result, _ = run(source, response, tmp_path=tmp_path,
                    document={"posture": "bill_status_or_summary_page", "evidence": "Bill History"})
    c = result.candidates[0]
    assert c.accepted and c.rule["status"] == "failed" and c.legislative["status_basis"] == "session_expired"
    assert c.rule["requirement"].endswith(candidate["requirement"]) and c.source_basis == "official_bill_summary"
    assert result.document_status == "complete"
