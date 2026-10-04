"""M5 deterministic applicability engine. Synthetic rules and facts; the last tests build the
real lookups offline into a temporary directory. No network, no LLM."""

from __future__ import annotations

import json
from datetime import date

import pytest

from navigator.applicability.conditions import (CERT_OCCUPANCY, JURISDICTION_UNRESOLVED, OWNER_OCCUPANCY,
                                                UNITS_MISSING, and3, classify, or3)
from navigator.applicability.engine import APPLIES, NOT_YET, PENDING, UNKNOWN, evaluate, scope_gaps
from navigator.applicability.facts import PropertyFacts, facts_from_row, unit_range

AS_OF = date(2026, 10, 1)
LA = {"state_jurisdiction": "CA", "local_jurisdiction": "Los Angeles, CA"}
NEWARK = {"state_jurisdiction": "NJ", "local_jurisdiction": "Newark, NJ"}
NONE = {"state_jurisdiction": None, "local_jurisdiction": None}


def rule(rid="r-1", jurisdiction="CA", status="in_force", cov=None, exe=None, eff=None):
    return {"team_rule_id": rid, "jurisdiction": jurisdiction, "level": "state" if len(jurisdiction) == 2 else "city",
            "status": status, "effective_date": eff, "coverage_conditions": cov, "exemptions": exe,
            "category": "rent_increase_limits", "title": "t", "requirement": "r", "conflict_flag": False}


def facts(year=1927, units=(32, 32), use_class="apartment"):
    lo, hi = units
    return PropertyFacts("A1", year, lo, hi, "recorded" if lo is not None else "missing", use_class, "")


# ----------------------------------------------------------------- jurisdiction

def test_state_and_city_rules_are_selected_by_resolved_jurisdiction_only():
    assert evaluate(rule(jurisdiction="CA"), facts(), LA, AS_OF).result == APPLIES
    assert evaluate(rule(jurisdiction="Los Angeles, CA"), facts(), LA, AS_OF).result == APPLIES
    other = evaluate(rule(jurisdiction="San Diego, CA"), facts(), LA, AS_OF)
    assert other.result is None and other.omitted_because == "other_jurisdiction"
    assert evaluate(rule(jurisdiction="NJ"), facts(), LA, AS_OF).result is None


def test_postal_city_is_never_an_input():
    row = {"address_id": "A1", "year_built": "1927", "units": "32", "use_code": "0500",
           "use_description": "Five or more apartments", "postal_city": "Van Nuys"}
    f = facts_from_row(row, {})
    assert not hasattr(f, "postal_city")
    # the jurisdiction comes only from the M4 resolution passed in
    assert evaluate(rule(jurisdiction="Los Angeles, CA"), f, LA, AS_OF).result == APPLIES


def test_unresolved_jurisdiction_is_unknown_for_every_rule():
    for j in ("CA", "Boston, MA", "NJ"):
        o = evaluate(rule(jurisdiction=j), facts(), NONE, AS_OF)
        assert o.result == UNKNOWN and o.reasons == [JURISDICTION_UNRESOLVED]


def test_review_required_geography_can_use_the_m4_jurisdiction(tmp_path):
    # M4 review rows keep their state/local jurisdiction; the engine treats them like resolved ones,
    # and the build script appends the geography note (checked in the end-to-end test below).
    assert evaluate(rule(jurisdiction="Newark, NJ"), facts(), NEWARK, AS_OF).result == APPLIES


# ----------------------------------------------------------------- temporal

def test_temporal_statuses():
    assert evaluate(rule(status="pending"), facts(), LA, AS_OF).result == PENDING
    assert evaluate(rule(status="failed"), facts(), LA, AS_OF).result is None
    assert evaluate(rule(status="not_yet_effective", eff="2027-07-01"), facts(), LA, AS_OF).result == NOT_YET
    assert evaluate(rule(eff="2027-07-01"), facts(), LA, AS_OF).result == NOT_YET          # in_force, future date
    assert evaluate(rule(eff="2026-09-01"), facts(), LA, AS_OF).result == APPLIES
    assert evaluate(rule(status="failed"), facts(), NONE, AS_OF).result is None             # never surfaced


# ----------------------------------------------------------------- conditions

def test_missing_fact_is_unknown_never_false():
    exe = "Dwelling units in owner-occupied premises of not more than four dwelling units are excluded."
    o = evaluate(rule(jurisdiction="NJ", exe=exe), facts(units=(None, None)), NEWARK, AS_OF)
    assert o.result == UNKNOWN and set(o.reasons) == {UNITS_MISSING, OWNER_OCCUPANCY}
    assert evaluate(rule(jurisdiction="NJ", exe=exe), facts(units=(8, 8)), NEWARK, AS_OF).result == APPLIES


def test_false_coverage_is_omitted():
    o = evaluate(rule(cov="Applies to residential buildings built before January 1, 1980."), facts(year=1990), LA,
                 AS_OF)
    assert o.result is None and o.omitted_because.startswith("coverage_false")
    assert evaluate(rule(cov="Applies to buildings built before January 1, 1980."), facts(year=None), LA,
                    AS_OF).result == UNKNOWN


def test_true_exemption_is_omitted_and_unknown_exemption_is_unknown():
    exe = "The fee limits do not apply to a dwelling unit located in a one-family or two-family dwelling."
    assert evaluate(rule(exe=exe), facts(units=(2, 2)), LA, AS_OF).omitted_because.startswith("exempt")
    assert evaluate(rule(exe=exe), facts(units=(32, 32)), LA, AS_OF).result == APPLIES
    assert evaluate(rule(exe=exe), facts(units=(None, None)), LA, AS_OF).result == UNKNOWN


def test_three_valued_logic():
    assert and3([True, None]) is None and and3([False, None]) is False and and3([True, True]) is True
    assert or3([False, None]) is None and or3([True, None]) is True and or3([False, False]) is False
    # owner-occupied duplex exemption: units decide it only when they rule it out
    exe = "A duplex in which the owner occupied one of the units as their principal residence is exempt."
    assert evaluate(rule(exe=exe), facts(units=(32, 32)), LA, AS_OF).result == APPLIES
    assert evaluate(rule(exe=exe), facts(units=(2, 2)), LA, AS_OF).result == UNKNOWN


def test_certificate_of_occupancy_age_uses_the_readme_year_built_proxy():
    # README challenge-data proxy (M5.1): cutoff = 2026-10-01 - 15 years -> 2011.
    exe = "Housing issued a certificate of occupancy within the previous 15 years is exempt from this section."
    assert evaluate(rule(exe=exe), facts(year=1927), LA, AS_OF).result == APPLIES          # older: not exempt
    assert evaluate(rule(exe=exe), facts(year=2024), LA, AS_OF).omitted_because.startswith("exempt")
    cutoff = evaluate(rule(exe=exe), facts(year=2011), LA, AS_OF)
    assert cutoff.result == UNKNOWN and cutoff.reasons == ["year_built_in_cutoff_year"]
    missing = evaluate(rule(exe=exe), facts(year=None), LA, AS_OF)
    assert missing.result == UNKNOWN and missing.reasons == ["year_built_missing"]
    # the cutoff moves with the query date
    assert evaluate(rule(exe=exe), facts(year=2011), LA, date(2025, 10, 1)).omitted_because.startswith("exempt")
    assert evaluate(rule(exe=exe), facts(year=2011), LA, date(2027, 10, 1)).result == APPLIES


def test_certificate_dates_without_an_age_test_stay_unknown():
    exe = "Housing that received a certificate of occupancy for new construction is exempt."
    o = evaluate(rule(exe=exe), facts(year=1927), LA, AS_OF)
    assert o.result == UNKNOWN and o.reasons == [CERT_OCCUPANCY]


def test_built_cutoff_year_and_replacement_alternative_stay_unknown():
    cov = ("The Rent Stabilization Ordinance applies to rental properties first built on or before October 1, 1978, "
           "replacement units under LAMC Section 151.28, and qualifying property types.")
    assert evaluate(rule(jurisdiction="Los Angeles, CA", cov=cov), facts(year=1927), LA, AS_OF).result == APPLIES
    assert evaluate(rule(jurisdiction="Los Angeles, CA", cov=cov), facts(year=1978), LA, AS_OF).result == UNKNOWN
    assert evaluate(rule(jurisdiction="Los Angeles, CA", cov=cov), facts(year=1990), LA, AS_OF).result == UNKNOWN


def test_situational_and_unsupported_clauses():
    assert classify("Applies when terminating a tenancy for owner occupancy.", "coverage").kind == "situational"
    assert classify("Applies to residential real property subject to this section.", "coverage").kind == "generic"
    assert classify("Unless otherwise\nrequired by law", "exemption").kind == "unsupported"
    assert evaluate(rule(exe="Unless otherwise\nrequired by law"), facts(), LA, AS_OF).result == UNKNOWN
    product = "The restriction does not apply to a product used for establishing rent limits for affordable housing."
    assert classify(product, "exemption").kind == "situational"


def test_facility_exemptions_are_ruled_out_only_for_apartment_buildings():
    exe = "Residences in any hospital, skilled nursing facility, or health facility are exempt."
    assert evaluate(rule(exe=exe), facts(), LA, AS_OF).result == APPLIES
    assert evaluate(rule(exe=exe), facts(use_class=None), LA, AS_OF).result == UNKNOWN


def test_unit_facts():
    assert unit_range("Five or more apartments") == (5, None)
    assert unit_range("Apartment 5 to 14 Units") == (5, 14)
    assert unit_range("APT 7-30 UNITS") == (7, 30)
    assert unit_range(">8-UNIT-APT") == (9, None)
    assert unit_range("3S-F-D-6U-NH") == (None, None)          # coded NJ descriptions are not parsed
    row = {"address_id": "A0227", "year_built": "", "units": "2", "use_code": "4C", "use_description": "13B-93U-2C-G"}
    assert facts_from_row(row, {}).units_source == "conflict"


def test_extraction_scope_gap_makes_the_record_unknown():
    art = {"scope_mappings": [{"condition_id": "S1", "provision_id": "P17", "kind": "coverage_condition",
                               "statement": "Applies with a government rent subsidy.", "final": "unresolved"}],
           "candidates": [{"rule": {"team_rule_id": "r-1"}, "provision_ids": ["P17"]}]}
    gaps = scope_gaps([art])
    assert evaluate(rule(), facts(), LA, AS_OF, gaps["r-1"]).result == UNKNOWN


# ----------------------------------------------------------------- end to end (real inputs, offline)

@pytest.fixture(scope="module")
def built(tmp_path_factory):
    import sys
    from navigator.starter_pack import REPO_ROOT
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import build_lookups
    out = tmp_path_factory.mktemp("m5")
    assert build_lookups.main(["--out", str(out)]) == 0
    return out


def test_all_500_ids_exactly_once_and_template_shape(built):
    import csv
    from navigator.starter_pack import REPO_ROOT
    doc = json.loads((built / "lookups.json").read_text(encoding="utf-8"))
    template = json.loads((REPO_ROOT / "submission_templates/lookups.json").read_text(encoding="utf-8"))
    with open(REPO_ROOT / "data/sample_addresses.csv", encoding="utf-8", newline="") as f:
        ids = [r["address_id"] for r in csv.DictReader(f)]
    assert set(doc) == set(template) and doc["as_of"] == "2026-10-01"
    assert sorted(doc["lookups"]) == sorted(ids) and len(ids) == len(set(ids)) == 500
    keys = list(next(iter(template["lookups"].values()))[0])
    allowed = {"applies", "unknown", "superseded", "not_yet_effective", "pending"}
    for entries in doc["lookups"].values():
        assert all(list(e) == keys and e["result"] in allowed for e in entries)
        assert len({e["team_rule_id"] for e in entries}) == len(entries)


def test_review_geography_note_and_unresolved_rows(built):
    audit = json.loads((built / "lookups_audit.json").read_text(encoding="utf-8"))["addresses"]
    review = [a for a in audit if a["jurisdiction_review_required"] and a["local_jurisdiction"]]
    assert review and all(d["reason"].endswith("Jurisdiction flagged for review in M4.")
                          for a in review for d in a["results"] if d["result"])
    unresolved = [a for a in audit if not a["state_jurisdiction"]]
    assert unresolved and all(d["result"] == "unknown" and d["unknown_reasons"] == ["jurisdiction_unresolved"]
                              for a in unresolved for d in a["results"])
