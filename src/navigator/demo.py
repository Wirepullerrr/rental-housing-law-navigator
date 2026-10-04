"""Read-only data layer for the LeaseLens demo (app.py). Offline: no Gemini, no Census, no database.

Reads the frozen outputs only:
  outputs/m3/full_v2/rules.json                 published rule records (M3)
  outputs/m4/jurisdiction_resolutions.json      Census jurisdictions (M4)
  outputs/m5/lookups.json                       submitted lookups at 2026-10-01 (M5)
  outputs/m5/rule_coverage_gaps.json            extraction scope gaps used by M5
  outputs/m6/changes.json, change_summary.json  change tests (M6)
  data/sample_addresses.csv, dev/change_tests.json, review/m6_change_test_map.json

Per-rule reasons and missing facts come from re-running the unchanged M5 engine
(navigator.applicability) on those inputs, in the same order as scripts/build_lookups.py. At the
default date the result must equal the submitted lookups.json (checked here and in the tests).
The large lookups_audit.json is not loaded. Not legal advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from navigator.applicability.engine import _REASON_TEXT, evaluate
from navigator.applicability.facts import PropertyFacts, code_labels, facts_from_row
from navigator.starter_pack import ADDRESSES_PATH, CHANGE_TESTS_PATH, REPO_ROOT, read_csv, read_json

DEFAULT_AS_OF = date(2026, 10, 1)
GEO_NOTE = " Jurisdiction flagged for review in M4."      # same suffix as scripts/build_lookups.py

# Predefined demo addresses (chosen for what they show, not for their results; nothing legal is hard-coded).
DEMO_EXAMPLES = {
    "A0500": "California: state + City of Los Angeles rules",
    "A0134": "Boston: postal city 'Dorchester', legal city Boston; MA bills pending",
    "A0495": "New Jersey: Jersey City (change tests T2 and T3)",
    "A0322": "Missing facts: San Diego (postal 'San Ysidro'), no year built -> unknown",
}

CATEGORY_LABELS = {
    "rent_increase_limits": "Rent increase limits",
    "just_cause_eviction": "Just-cause eviction",
    "security_deposits": "Security deposits",
    "screening_restrictions": "Tenant screening restrictions",
    "application_screening_fees": "Application & screening fees",
    "algorithmic_rent_setting": "Algorithmic rent setting",
}
RESULT_LABELS = {"applies": "Applies", "unknown": "Unknown", "pending": "Pending",
                 "not_yet_effective": "Not yet effective", "superseded": "Superseded"}


@dataclass
class Bundle:
    rules: dict[str, dict[str, Any]]
    order: list[str]
    resolutions: dict[str, dict[str, Any]]
    rows: dict[str, dict[str, str]]
    facts: dict[str, PropertyFacts]
    gaps: dict[str, list[dict[str, str]]]
    lookups: dict[str, Any]
    changes: dict[str, dict[str, Any]]
    change_tests: list[dict[str, Any]]
    change_map: dict[str, dict[str, Any]]
    change_summary: dict[str, Any]


def load_bundle(root: Path = REPO_ROOT) -> Bundle:
    rules = read_json(root / "outputs/m3/full_v2/rules.json")["rules"]
    by_id = {r["team_rule_id"]: r for r in rules}
    order = sorted(by_id, key=lambda i: (by_id[i]["level"] != "state", by_id[i]["category"],
                                         by_id[i]["jurisdiction"], i))
    _, rows = read_csv(root / ADDRESSES_PATH)
    labels = code_labels(rows)
    return Bundle(
        rules=by_id, order=order,
        resolutions={r["address_id"]: r for r in read_json(root / "outputs/m4/jurisdiction_resolutions.json")
                     ["resolutions"]},
        rows={r["address_id"]: r for r in rows},
        facts={r["address_id"]: facts_from_row(r, labels) for r in rows},
        gaps=read_json(root / "outputs/m5/rule_coverage_gaps.json")["extraction_scope_gaps"],
        lookups=read_json(root / "outputs/m5/lookups.json"),
        changes=read_json(root / "outputs/m6/changes.json"),
        change_tests=read_json(root / CHANGE_TESTS_PATH),
        change_map=read_json(root / "review/m6_change_test_map.json")["rules"],
        change_summary=read_json(root / "outputs/m6/change_summary.json"),
    )


def rule_results(b: Bundle, address_id: str, as_of: date) -> list[dict[str, Any]]:
    """Surfaced rules for one address on `as_of`, with the engine's reasons. Omitted rules are left out."""
    res = b.resolutions[address_id]
    jur = {"state_jurisdiction": res["state_jurisdiction"], "local_jurisdiction": res["local_jurisdiction"]}
    geo_review = res["resolution_status"] == "review_required"
    out = []
    for rid in b.order:
        o = evaluate(b.rules[rid], b.facts[address_id], jur, as_of, b.gaps.get(rid))
        if o.result is None:
            continue
        out.append({"rule": b.rules[rid], "team_rule_id": rid, "result": o.result,
                    "explanation": o.explanation + (GEO_NOTE if geo_review else ""),
                    "missing": [_REASON_TEXT.get(x, x) for x in o.reasons],
                    "causes": o.causes})
    return out


def matches_submission(b: Bundle, address_id: str, results: list[dict[str, Any]]) -> bool:
    """True when these results equal the submitted lookups.json row (meaningful only at its as_of)."""
    submitted = [(e["team_rule_id"], e["result"], e["explanation"]) for e in b.lookups["lookups"][address_id]]
    return submitted == [(r["team_rule_id"], r["result"], r["explanation"]) for r in results]


def address_label(b: Bundle, address_id: str) -> str:
    row = b.rows[address_id]
    return f"{address_id} · {row['street_address']}, {row['postal_city']}, {row['state']} {row['zip']}"


def map_points(b: Bundle, address_ids: list[str] | None = None,
               flagged: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Plot-ready points from the coordinates M4 already recorded (no geocoding). Addresses without
    M4 coordinates (unresolved, most overrides) are skipped."""
    flagged_set = set(flagged)
    out = []
    for aid in (address_ids if address_ids is not None else sorted(b.rows)):
        res, row = b.resolutions[aid], b.rows[aid]
        if res.get("latitude") is None or res.get("longitude") is None:
            continue
        out.append({"address_id": aid, "lat": res["latitude"], "lon": res["longitude"],
                    "address": f"{row['street_address']}, {row['postal_city']}, {row['state']}",
                    "jurisdiction": res["local_jurisdiction"] or res["state_jurisdiction"] or "unresolved",
                    "conflict_flag": "yes" if aid in flagged_set else "no"})
    return out


def change_status(b: Bundle, address_id: str) -> dict[str, str]:
    """Per change test: is this address affected / conflict-flagged in the submitted changes.json?"""
    out = {}
    for tid, e in b.changes.items():
        if address_id in e["conflict_flag_address_ids"]:
            out[tid] = "affected + conflict flag"
        elif address_id in e["affected_address_ids"]:
            out[tid] = "affected"
        else:
            out[tid] = "not affected"
    return out
