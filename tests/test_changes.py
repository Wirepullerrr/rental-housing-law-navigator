"""M6 Module C: deterministic change tests. Synthetic inputs plus an offline end-to-end build."""

from __future__ import annotations

import json

import pytest

from navigator.applicability.facts import PropertyFacts
from navigator.changes import KEYS, build

F = PropertyFacts("x", 1950, 10, 10, "recorded", "apartment", "")
ADDR = [{"address_id": "A1", "state_jurisdiction": "NJ", "local_jurisdiction": "Hoboken, NJ"},
        {"address_id": "A2", "state_jurisdiction": "NJ", "local_jurisdiction": "Jersey City, NJ"},
        {"address_id": "A3", "state_jurisdiction": "NJ", "local_jurisdiction": "Newark, NJ"},
        {"address_id": "A4", "state_jurisdiction": "CA", "local_jurisdiction": "Los Angeles, CA"},
        {"address_id": "A5", "state_jurisdiction": "MA", "local_jurisdiction": "Boston, MA"},
        {"address_id": "A6", "state_jurisdiction": None, "local_jurisdiction": None}]
FACTS = {a["address_id"]: F for a in ADDR}
MAP = {"HOB": {"jurisdiction": "Hoboken, NJ", "label": "Hoboken ban", "basis": "supplied_change_fact_only",
               "team_rule_ids": [], "source_doc_ids": ["D032"]},
       "JC": {"jurisdiction": "Jersey City, NJ", "label": "JC ban", "basis": "supplied_change_fact_only",
              "team_rule_ids": [], "source_doc_ids": ["D035"]},
       "NJ": {"jurisdiction": "NJ", "label": "FAIR", "basis": "x", "team_rule_ids": [], "source_doc_ids": ["D069"]},
       "CA": {"jurisdiction": "CA", "label": "AB 325", "basis": "verified_held_records", "team_rule_ids": ["r-ca"],
              "source_doc_ids": ["D022"]},
       "P1": {"jurisdiction": "MA", "label": "S.1", "basis": "published_records", "team_rule_ids": ["r-p"],
              "source_doc_ids": ["D046"]},
       "RENT": {"jurisdiction": "MA", "label": "ballot", "basis": "supplied_change_fact_only", "team_rule_ids": [],
                "source_doc_ids": ["D059"]}}
RULES = {"r-p": {"team_rule_id": "r-p", "jurisdiction": "MA", "status": "pending",
                 "category": "algorithmic_rent_setting"},
         "r-f": {"team_rule_id": "r-f", "jurisdiction": "Boston, MA", "status": "failed",
                 "category": "rent_increase_limits"}}
HELD = {"r-ca": {"team_rule_id": "r-ca", "citation": "B&P 16729(a)", "quoted_span": "q", "source_url": "u",
                 "source_doc_id": "D022", "coverage_conditions": None, "exemptions": None}}
TESTS = [
    {"test_id": "T1", "type": "as_of", "rule_ids": ["CA"], "as_of_before": "2025-12-31", "as_of_after": "2026-01-02",
     "states": ["CA"]},
    {"test_id": "T2", "type": "boundary", "rule_ids": ["HOB", "JC"], "as_of": "2026-10-01"},
    {"test_id": "T3", "type": "as_of", "rule_ids": ["NJ"], "as_of_before": "2026-10-01", "as_of_after": "2027-07-02",
     "states": ["NJ"], "conflict_with": ["JC", "HOB"]},
    {"test_id": "T4", "type": "pending", "rule_ids": ["P1"], "as_of": "2026-10-01", "states": ["MA"]},
    {"test_id": "T5", "type": "negative", "rule_ids": ["RENT"], "as_of": "2026-10-01", "states": ["MA"]},
]
LOOKUPS = {a["address_id"]: {} for a in ADDR} | {"A5": {"r-p": "pending"}}
EFF = {"NJ": {"date": "2027-07-01"}}


def run(lookups=LOOKUPS, eff=EFF):
    return build(TESTS, MAP, RULES, HELD, ADDR, FACTS, lookups, eff)


def test_before_and_after_effective_date():
    changes, audit = run()
    assert changes["T3"]["affected_address_ids"] == ["A1", "A2", "A3"]
    assert {(x["before"], x["after"]) for v in audit["T3"]["per_address"].values() for x in v} == {
        ("not_yet_effective", "applies")}
    assert changes["T1"]["affected_address_ids"] == ["A4"]           # supplied window (before, after]
    _, later = run(eff={"NJ": {"date": "2028-01-01"}})                # a later date: nothing changes yet
    assert later["T3"]["affected"] == []


def test_conflict_flags_only_inside_the_conflicting_cities():
    changes, _ = run()
    assert changes["T3"]["conflict_flag_address_ids"] == ["A1", "A2"]


def test_city_rules_do_not_bleed_across_hoboken_and_jersey_city():
    changes, audit = run()
    assert audit["T2"]["by_rule"] == {"HOB": ["A1"], "JC": ["A2"]}
    assert "A3" not in changes["T2"]["affected_address_ids"]


def test_pending_stays_pending_and_failed_stays_failed():
    changes, audit = run()
    assert changes["T4"]["affected_address_ids"] == ["A5"] and audit["T4"]["addresses_reporting_applies"] == []
    assert changes["T5"]["affected_address_ids"] == [] and audit["T5"]["failed_records_never_surfaced"] == ["r-f"]
    _, bad = run(lookups=LOOKUPS | {"A5": {"r-p": "applies"}})
    assert bad["T4"]["addresses_reporting_applies"] == ["A5"]


def test_supplied_change_applies_only_where_specified_and_unresolved_is_never_listed():
    changes, audit = run()
    assert all("A6" not in e["affected_address_ids"] for e in changes.values())
    assert audit["T1"]["undetermined_jurisdiction"] == ["A6"]
    assert "A4" not in changes["T3"]["affected_address_ids"]          # the NJ change does not touch CA


def test_every_test_once_with_official_keys():
    changes, _ = run()
    assert list(changes) == ["T1", "T2", "T3", "T4", "T5"]
    assert all(tuple(e) == KEYS for e in changes.values())


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    import sys
    from navigator.starter_pack import REPO_ROOT
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import build_changes
    out = tmp_path_factory.mktemp("m6")
    assert build_changes.main([str(out)]) == 0
    return out


def test_real_changes_json_matches_the_official_shape(built):
    from navigator.starter_pack import REPO_ROOT
    doc = json.loads((built / "changes.json").read_text(encoding="utf-8"))
    tests = json.loads((REPO_ROOT / "dev/change_tests.json").read_text(encoding="utf-8"))
    assert list(doc) == [t["test_id"] for t in tests]
    assert all(set(e) == {"affected_address_ids", "conflict_flag_address_ids", "notes"} for e in doc.values())
    assert doc["T5"]["affected_address_ids"] == []
    assert set(doc["T3"]["conflict_flag_address_ids"]) <= set(doc["T3"]["affected_address_ids"])
    t2 = set(doc["T2"]["affected_address_ids"])
    assert t2 and not t2 & set(doc["T4"]["affected_address_ids"])
