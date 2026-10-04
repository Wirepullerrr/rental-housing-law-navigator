"""Prompt v6 / M3.1: deterministic relative-date resolution, source posture, empty-result
semantics, official-summary records, scope challenges and provision-id scope targeting.
Synthetic documents only; no network."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import FakeProvider, codified, make_candidate, no_rules, parts, provision, synthetic_source
from navigator.extraction.cache import ResponseCache
from navigator.extraction.citation import verify_span
from navigator.extraction.coverage import provision_regions, scope_challenges
from navigator.extraction.extractor import extract_document
from navigator.extraction.normalize import BASIS_LABELS
from navigator.extraction.replay import adapt_v5
from navigator.extraction.temporal import RELATIVE_RESOLVER, find_base_dates, resolve_relative


def run(source, *responses, tmp_path, **kwargs):
    provider = FakeProvider(*responses, document=kwargs.pop("document", codified(source)))
    result = extract_document(source, provider_name=provider.name, model=provider.model, provider=provider,
                              cache=ResponseCache(tmp_path / "cache"), **kwargs)
    return result, provider


def rule(quote, citation, pid="P1", category="screening_restrictions", **overrides):
    return make_candidate(**{"quote_parts": parts(quote), "citation": citation, "provision_ids": [pid],
                             "category": category, "key_value": None, "coverage_conditions": None,
                             "requirement": "A landlord must follow this rule.", **overrides})


# ------------------------------------------------- relative effective dates

DUTY = "A landlord shall not ask an applicant about a criminal record before a conditional offer."
FORMULA = "This act shall take effect on the first day of the\nseventh month next following the date of enactment"


def session_law(*tail: str) -> str:
    return "\n".join(["AN ACT concerning rental housing applications.", "Be It Enacted by the Legislature:",
                      f"1. {DUTY}", *tail]) + "\n"


ENACTED = session_law(f"2. {FORMULA}, but the agency may take anticipatory administrative action.",
                      "Approved June 18, 2021.")
LAW_POSTURE = {"posture": "enacted_session_law", "evidence": "Approved June 18, 2021."}


def dated(quote=DUTY, evidence=FORMULA, kind="relative_date_formula", **overrides):
    return rule(quote, "Act § 1", effective_date_evidence=evidence, effective_date_evidence_kind=kind, **overrides)


@pytest.mark.parametrize("as_of, status", [(date(2026, 10, 1), "in_force"), (date(2021, 12, 31), "not_yet_effective")])
def test_supported_formula_is_resolved_from_the_documents_own_enactment_date(tmp_path, as_of, status):
    result, _ = run(synthetic_source(ENACTED), {"rules": [dated()]}, tmp_path=tmp_path, as_of=as_of,
                    document=LAW_POSTURE)
    c = result.candidates[0]
    assert c.accepted and (c.rule["effective_date"], c.rule["status"]) == ("2022-01-01", status)
    audit = c.temporal["relative_resolution"]
    assert (audit["outcome"], audit["resolver"], audit["base_date"]) == ("resolved", RELATIVE_RESOLVER, "2021-06-18")
    assert audit["base_date_evidence"]["text"] == "Approved June 18, 2021"
    assert audit["formula"] == ("take effect on the first day of the seventh month next following the date "
                                "of enactment")
    assert c.citation.source_span == DUTY                              # the citation itself is unchanged


def test_model_date_and_model_classification_are_never_used(tmp_path):
    candidate = dated(effective_date="2021-12-01", kind="explicit_operative_date")
    result, _ = run(synthetic_source(ENACTED), {"rules": [candidate]}, tmp_path=tmp_path, document=LAW_POSTURE)
    c = result.candidates[0]
    assert c.accepted and c.rule["effective_date"] == "2022-01-01"
    assert any("treated as relative_date_formula" in w for w in c.warnings)
    assert any("differs from the deterministic resolution" in w for w in c.warnings)


@pytest.mark.parametrize("text, evidence, why", [
    (session_law(f"2. {FORMULA}."), FORMULA, "records no date of enactment"),                     # no base date
    (session_law(f"2. {FORMULA}.", "Approved June 18, 2021.", "Approved July 2, 2021."), FORMULA,
     "several possible dates"),                                                                     # ambiguous
    (session_law("2. This act shall take effect 90 days after enactment.", "Approved June 18, 2021."),
     "This act shall take effect 90 days after enactment", "supported grammar"),                   # unsupported
    (session_law("2. This act takes effect on the first day of the third month following enactment.",
                 "Approved June 18, 2021."),
     "This act takes effect on the first day of the third month following enactment", "supported grammar"),
    (session_law(f"2. {FORMULA}, except that section 1 shall take effect immediately.", "Approved June 18, 2021."),
     FORMULA, "qualifies the formula"),                                                            # qualified
])
def test_unresolvable_formulas_hold_the_record_and_say_why(tmp_path, text, evidence, why):
    result, _ = run(synthetic_source(text), {"rules": [dated(evidence=evidence)]}, tmp_path=tmp_path,
                    document={"posture": "enacted_session_law", "evidence": "Be It Enacted by the Legislature:"})
    c = result.candidates[0]
    assert not c.accepted and c.held and c.rule["status"] is None and result.rules == []
    assert why in c.temporal["relative_resolution"]["reason"]
    assert result.document_status == "review_required"
    assert any("held, not published" in r for r in result.review_reasons)


def test_base_dates_come_only_from_enactment_records_in_the_raw_text():
    raw = ("Approved by the voters November 3, 2020.\nFiled with Secretary of State October 6, 2025.\n"
           "Approved  by\nGovernor\nOctober 06, 2025.\nP.L. 2026, CHAPTER 43, approved July 20, 2026\n")
    assert [(b["event"], b["date"]) for b in find_base_dates(raw)] == [("approved", "2025-10-06"),
                                                                       ("approved", "2026-07-20")]
    formula = FORMULA.replace("\n", " ")
    unverified = verify_span("This act shall take effect on the first day of the ninth month next following "
                             "the date of enactment", formula)
    assert resolve_relative(unverified, formula, [{"event": "approved", "date": "2021-06-18"}])["reason"] == \
        "the formula evidence is not verified source text"
    ok = resolve_relative(verify_span(formula, formula + " Approved June 18, 2021."), formula + " Approved June 18, "
                          "2021.", find_base_dates("Approved June 18, 2021."))
    assert ok["resolved_date"] == "2022-01-01"


# ------------------------------------------------------------- posture

@pytest.mark.parametrize("document, status, held", [
    ({"posture": "codified_current_law", "evidence": "Be It Enacted by the Legislature:"}, "in_force", False),
    ({"posture": "enacted_session_law", "evidence": "Be It Enacted by the Legislature:"}, None, True),
    ({"posture": "codified_current_law", "evidence": "Title 9, Chapter 4 of the Code"}, None, True),  # unverified
    ({"posture": "official_explanatory_page", "evidence": "Be It Enacted by the Legislature:"}, None, True),
    (None, None, True),                                                                              # undeclared
])
def test_enacted_rule_without_a_date_is_in_force_only_for_established_codified_law(tmp_path, document, status, held):
    result, _ = run(synthetic_source(session_law()), {"rules": [rule(DUTY, "Act § 1")]}, tmp_path=tmp_path,
                    document=document)
    c = result.candidates[0]
    assert (c.rule["status"], c.held, c.accepted) == (status, held, not held)
    assert result.rules == ([] if held else [c.rule])


def test_codified_posture_is_not_established_when_the_text_records_an_enactment(tmp_path):
    document = {"posture": "codified_current_law", "evidence": "Be It Enacted by the Legislature:"}
    result, _ = run(synthetic_source(session_law("Approved June 18, 2021.")), {"rules": [rule(DUTY, "Act § 1")]},
                    tmp_path=tmp_path, document=document)
    assert result.posture["established"] == "unknown" and "enactment date" in result.posture["basis"]
    assert result.candidates[0].held                                  # D022-style: never in force on every date


def test_session_law_without_an_effective_date_is_held_not_published_as_in_force(tmp_path):
    chaptered = ("Assembly Bill No. 7\nCHAPTER 338\nApproved by Governor October 06, 2025.\n"
                 "Sec. 2. A person shall not use a common pricing algorithm to set rents in a conspiracy.\n")
    quote = "A person shall not use a common pricing algorithm to set rents in a conspiracy."
    result, _ = run(synthetic_source(chaptered), {"provisions": [provision("Sec. 2", "in_scope",
                                                                            "algorithmic_rent_setting")],
                                                  "global_scope": [], "rules": [rule(quote, "Sec. 2",
                                                                                     category="algorithmic_rent_setting")]},
                    tmp_path=tmp_path, document={"posture": "enacted_session_law", "evidence": "CHAPTER 338"})
    c = result.candidates[0]
    assert c.held and c.rule["status"] is None and result.rules == []
    assert c.status_derivation.startswith("temporal resolution required")
    assert result.document_status == "review_required"
    assert not any("all rejected" in r for r in result.review_reasons)   # held, not reported as rejected


# --------------------------------------------------------- empty results

PROCEDURAL = "Bill H.99\nAn Act relative to municipal parking permits\nBill History\n3/12/2026 House Referred\n"


@pytest.mark.parametrize("justification, complete", [
    (None, False),
    (no_rules("The bill concerns parking permits.", "An Act relative to municipal parking permits"), True),
    (no_rules("The bill concerns parking permits.", "An Act relative to rent control"), False),     # not source text
])
def test_an_empty_result_is_complete_only_with_a_verified_justification(tmp_path, justification, complete):
    response = {"provisions": [], "global_scope": [], "rules": [], "no_rules_justification": justification}
    result, _ = run(synthetic_source(PROCEDURAL), response, tmp_path=tmp_path,
                    document={"posture": "bill_status_or_summary_page", "evidence": "Bill History"})
    assert (result.document_status == "complete") is complete
    if not complete:
        assert any("no source-grounded justification" in r for r in result.review_reasons)


# ------------------------------------------------- official summary records

STATUS_PAGE = ("Bill H.1234\nAn Act relative to limiting rent increases in residential tenancies\n"
               "Status: Referred to the committee on Housing\nBill History\n3/12/2026 House Referred\n")
TITLE = "An Act relative to limiting rent increases in residential tenancies"


def test_pending_record_from_an_official_summary_is_labelled_and_needs_status_evidence(tmp_path):
    summary = rule(TITLE, "H.1234", category="rent_increase_limits", source_basis="official_bill_summary",
                   enactment_status="pending", enactment_status_evidence="Status: Referred to the committee on Housing",
                   requirement="The bill would limit rent increases in residential tenancies.")
    response = {"provisions": [provision("H.1234", "in_scope", "rent_increase_limits", anchor=TITLE)],
                "global_scope": [], "rules": [summary], "no_rules_justification": None}
    result, _ = run(synthetic_source(STATUS_PAGE), response, tmp_path=tmp_path,
                    document={"posture": "bill_status_or_summary_page", "evidence": "Bill History"})
    c, record = result.candidates[0], result.rules[0]
    assert c.accepted and record["status"] == "pending" and c.source_basis == "official_bill_summary"
    assert record["requirement"] == BASIS_LABELS["official_bill_summary"] + summary["requirement"]
    assert record["quoted_span"] == TITLE and c.status_evidence.status == "exact_match"
    assert result.document_status == "complete" and result.posture["established"] == "bill_status_or_summary_page"


# ------------------------------------------------------- scope challenges

CAUSES = ("Part 2. Tenancy terminations\n"
          "2.1 An owner shall not terminate a tenancy without good cause stated in a written notice.\n"
          "2.2 \"Good cause\" includes grounds such as nonpayment of rent, a material lease violation, damage to "
          "the unit, and unlawful use of the unit.\n"
          "2.3 Before a termination notice, the owner shall give a written warning and a time to cure.\n"
          "2.4 \"Unit\" means a dwelling unit. \"Owner\" means a lessor. \"Tenant\" means a lessee.\n"
          "2.5 An owner shall maintain smoke detectors in each unit, including bedrooms, hallways, and kitchens.\n"
          "2.6 An owner who ends a tenancy for a no-fault reason shall pay relocation assistance.\n")
Q1 = "An owner shall not terminate a tenancy without good cause stated in a written notice."
Q2 = ("\"Good cause\" includes grounds such as nonpayment of rent, a material lease violation, damage to the unit, "
      "and unlawful use of the unit.")
Q3 = "Before a termination notice, the owner shall give a written warning and a time to cure."
Q6 = "An owner who ends a tenancy for a no-fault reason shall pay relocation assistance."


def causes_inventory(**p2):
    return [provision("2.1", "in_scope", "just_cause_eviction", anchor="2.1 An owner shall not terminate"),
            provision("2.2", "out_of_scope", id="P2", anchor="2.2 \"Good cause\" includes grounds",
                      **{"role": "definition", "reason": "definitions", **p2}),
            provision("2.3", "in_scope", "just_cause_eviction", id="P3", anchor="2.3 Before a termination notice"),
            provision("2.4", "out_of_scope", id="P4", anchor="2.4 \"Unit\" means a dwelling unit.", role="definition",
                      reason="definitions"),
            provision("2.5", "out_of_scope", id="P5", anchor="2.5 An owner shall maintain smoke detectors",
                      role="operative_rule", reason="habitability, not an official category"),
            provision("2.6", "in_scope", "just_cause_eviction", id="P6", anchor="2.6 An owner who ends a tenancy")]


def causes_primary(scope=(), **p2):
    jc = {"category": "just_cause_eviction"}
    return {"provisions": causes_inventory(**p2), "global_scope": list(scope), "no_rules_justification": None,
            "rules": [rule(Q1, "2.1", **jc), rule(Q3, "2.3", pid="P3", **jc), rule(Q6, "2.6", pid="P6", **jc)]}


def resolution(ref, scope="in_scope", reason=None, evidence=()):
    return {"ref": ref, "scope": scope, "reason": reason, "evidence_parts": parts(*evidence)}


def test_scope_challenge_targets_only_a_substantive_looking_out_of_scope_item(tmp_path):
    result, provider = run(synthetic_source(CAUSES), causes_primary(), {"target_resolutions": [], "rules": []},
                           tmp_path=tmp_path)
    by_ref = {c["ref"]: c for c in result.scope_challenges}
    assert [r for r, c in by_ref.items() if c["challenged"]] == ["2.2"]
    assert by_ref["2.2"]["signals"] == {"in_scope_neighbour": True, "enumeration": True, "normative_language": True,
                                        "grounds_or_conditions": True}
    assert by_ref["2.4"]["why_not"].startswith("glossary") and "operative_rule" in by_ref["2.5"]["why_not"]
    [target] = result.repair.targets
    assert (target["ref"], target["sources"], target["provision_id"]) == ("2.2", ["scope_challenge"], "P2")
    assert len(provider.calls) == 2 and "- 2.2 (provision id P2): declared out_of_scope" in provider.calls[1]["prompt"]
    assert result.rules and all(r["citation"] != "2.2" for r in result.rules)   # a challenge publishes nothing


def test_a_provision_stated_as_a_verified_scope_condition_is_not_challenged(tmp_path):
    s1 = {"id": "S1", "kind": "coverage_condition", "statement": "Good cause is required.", "citation": "2.2",
          "source_provision_id": "P2", "governed_provision_ids": ["P1"], "evidence_parts": parts(Q2)}
    result, provider = run(synthetic_source(CAUSES), causes_primary([s1]), tmp_path=tmp_path)
    assert not any(c["challenged"] for c in result.scope_challenges) and len(provider.calls) == 1


@pytest.mark.parametrize("repair, final, status", [
    ({"target_resolutions": [resolution("2.2")],
      "rules": [rule(Q2, "2.2", pid="P2", category="just_cause_eviction")]}, "resolved_by_accepted_rule", "complete"),
    ({"target_resolutions": [resolution("2.2", "out_of_scope", "It only illustrates good cause.", [Q2])],
      "rules": []}, "resolved_out_of_scope", "complete"),
    ({"target_resolutions": [resolution("2.2", "out_of_scope", "Not a rule.", [Q1])], "rules": []},
     "unresolved", "review_required"),                                     # evidence is not the item's own text
    ({"target_resolutions": [resolution("2.2", "uncertain", "Unclear.")], "rules": []}, "unresolved",
     "review_required"),
])
def test_scope_challenge_is_resolved_by_the_single_repair_pass(tmp_path, repair, final, status):
    result, provider = run(synthetic_source(CAUSES), causes_primary(), repair, tmp_path=tmp_path)
    assert len(provider.calls) == 2                                        # one primary + at most ONE repair
    assert result.repair.targets[0]["final_resolution"] == final and result.document_status == status
    if final == "resolved_by_accepted_rule":
        assert "2.2" in [r["citation"] for r in result.rules]


# ------------------------------------------- scope targeting by provision id

ANNOUNCEMENT = ("Key requirements of the Housing Stability Ordinance\n"
                "Rent increases are limited to 4% per year.\n"
                "The rent limit does not apply to buildings constructed after 2001.\n"
                "Owners must give tenants written notice of the ordinance at the start of a lease.\n")
RENT = "Rent increases are limited to 4% per year."
EXEMPT = "The rent limit does not apply to buildings constructed after 2001."
NOTICE = "Owners must give tenants written notice of the ordinance at the start of a lease."
TITLES = ["Housing Stability Ordinance, Rent Limit", "Housing Stability Ordinance, Rent Limit, Exemptions",
          "Housing Stability Ordinance, Notice"]


def announcement(rules, governed=("P1",)):
    s1 = {"id": "S1", "kind": "exemption", "statement": "Buildings constructed after 2001 are exempt.",
          "citation": TITLES[1], "source_provision_id": "P2", "governed_provision_ids": list(governed),
          "evidence_parts": parts(EXEMPT)}
    inventory = [provision(TITLES[0], "in_scope", "rent_increase_limits", anchor=RENT),
                 provision(TITLES[1], "out_of_scope", id="P2", anchor=EXEMPT, role="exemption"),
                 provision(TITLES[2], "in_scope", "rent_increase_limits", id="P3", anchor=NOTICE)]
    return {"provisions": inventory, "global_scope": [s1], "rules": rules, "no_rules_justification": None}


def test_exemption_follows_provision_ids_not_comma_titles(tmp_path):
    rules = [rule(RENT, TITLES[0], category="rent_increase_limits"),
             rule(NOTICE, TITLES[2], pid="P3", category="rent_increase_limits")]
    result, _ = run(synthetic_source(ANNOUNCEMENT), announcement(rules), tmp_path=tmp_path)
    assert result.rules[0]["exemptions"] == f"Buildings constructed after 2001 are exempt. [{TITLES[1]}]"
    assert result.rules[1]["exemptions"] is None                          # shares only a title prefix
    assert [p["id"] for p in result.candidates[1].propagated_scope] == []


def test_no_citation_or_substring_matching_in_scope_propagation(tmp_path):
    # Cites the governed provision's exact title, but links P3: the citation text is never compared.
    rules = [rule(NOTICE, TITLES[0] + " notice", pid="P3", category="rent_increase_limits")]
    result, _ = run(synthetic_source(ANNOUNCEMENT), announcement(rules), tmp_path=tmp_path)
    assert result.rules[0]["exemptions"] is None


def test_unknown_provision_ids_never_propagate_or_link(tmp_path):
    rules = [rule(RENT, TITLES[0], category="rent_increase_limits"),
             rule(NOTICE, TITLES[2], pid="P9", category="rent_increase_limits")]
    result, _ = run(synthetic_source(ANNOUNCEMENT), announcement(rules, governed=("P7",)), tmp_path=tmp_path)
    assert not result.global_scope[0]["propagated"] and "not in the inventory" in result.global_scope[0]["problem"]
    assert result.rules[0]["exemptions"] is None and not result.candidates[1].accepted
    assert any("S1" in r for r in result.review_reasons)


def test_legacy_governs_strings_map_only_on_exact_equality():
    v5 = {"provisions": [{"ref": TITLES[0], "summary": "", "scope": "in_scope", "category": "rent_increase_limits",
                          "reason": None, "rule_indices": [0]},
                         {"ref": TITLES[2], "summary": "", "scope": "in_scope", "category": "rent_increase_limits",
                          "reason": None, "rule_indices": [1]}],
          "global_scope": [{"id": "S1", "governs": TITLES[0]}, {"id": "S2", "governs": "Housing Stability Ordinance"},
                           {"id": "S3", "governs": None}],
          "rules": [{}, {}]}
    adapted, fills = adapt_v5(v5)
    assert [s["governed_provision_ids"] for s in adapted["global_scope"]] == [["P1"], [], None]
    assert [r["provision_ids"] for r in adapted["rules"]] == [["P1"], ["P2"]]
    assert "inventory roles" in fills["not_declared"]


LABELLED = ("§ 4 Pricing\n"
            "(a) A landlord shall not use a shared pricing algorithm to set rents.\n"
            "(b)\u00a0Nothing in this section limits remedies available under other law.\n"
            "(c) For purposes of this section, the following definitions apply:\n"
            "(1) \"Algorithm\" means a computational process.\n")


def test_region_includes_the_label_and_a_next_sibling_label_is_no_enumeration():
    source = synthetic_source(LABELLED)
    inventory = [provision("§ 4(a)", "in_scope", "algorithmic_rent_setting", anchor="A landlord shall not use"),
                 provision("§ 4(b)", "out_of_scope", id="P2", anchor="Nothing in this section limits", role="procedure"),
                 provision("§ 4(c)(1)", "out_of_scope", id="P3", anchor="\"Algorithm\" means", role="definition")]
    regions = provision_regions(inventory, source.body, source.view)
    assert source.body[regions["P2"]["raw_start"]:].startswith("(b)\u00a0Nothing")   # evidence may quote the label
    [b, _] = scope_challenges(inventory, regions, source.body, set())
    assert b["signals"]["enumeration"] is False and not b["challenged"]
