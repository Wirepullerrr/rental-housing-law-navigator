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
# The first one is the app's default.
DEMO_EXAMPLES = {
    "A0134": "Boston (mailing city Dorchester)",
    "A0500": "Los Angeles: state + city rules",
    "A0495": "Jersey City: change tests T2, T3",
    "A0322": "San Diego (mailing city San Ysidro)",
}

CATEGORY_LABELS = {
    "rent_increase_limits": "Rent increase limits",
    "just_cause_eviction": "Just-cause eviction",
    "security_deposits": "Security deposits",
    "screening_restrictions": "Tenant screening restrictions",
    "application_screening_fees": "Application & screening fees",
    "algorithmic_rent_setting": "Algorithmic rent setting",
}


@dataclass(frozen=True)
class StatusInfo:
    key: str            # lookups.json result value
    label: str          # card, chip and filter label
    meaning: str        # one-line microcopy under the count
    help: str           # hover help
    one: str            # at-a-glance sentence for a count of 1 ({n})
    many: str           # at-a-glance sentence for any other count ({n})


# The ONE status table: the status cards, the filter chips and the at-a-glance summary all take their wording
# from here and their numbers from status_counts(), so a count can never sit next to another status's label.
STATUSES = (
    StatusInfo("applies", "Applies", "Current rule for this property",
               "In force on this date, and it covers this property.",
               "{n} rule applies now.", "{n} rules apply now."),
    StatusInfo("unknown", "Unknown", "Needs a missing property fact",
               "Whether it covers this property depends on a fact the supplied property data doesn't include, "
               "so LeaseLens doesn't guess.",
               "{n} rule remains unknown.", "{n} rules remain unknown."),
    StatusInfo("pending", "Pending", "Proposal, not current law",
               "A bill or proposal. It isn't law on this date.",
               "{n} pending proposal may matter later.", "{n} pending proposals may matter later."),
    StatusInfo("not_yet_effective", "Not yet effective", "Enacted, starts later",
               "Enacted, but it takes effect after this date.",
               "{n} enacted rule takes effect later.", "{n} enacted rules take effect later."),
    StatusInfo("superseded", "Superseded", "A stricter rule governs",
               "Covers this property, but a stricter rule at another level governs.",
               "{n} rule is superseded by a stricter one.", "{n} rules are superseded by stricter ones."),
)
STATUS = {s.key: s for s in STATUSES}
RESULT_LABELS = {s.key: s.label for s in STATUSES}

# Short, user-facing names for the engine's unknown reasons (M5 reason codes). Display only.
MISSING_FACT_LABELS = {
    "owner_occupancy_unknown": "Owner occupancy",
    "subsidized_status_unknown": "Subsidy or deed restriction",
    "local_ordinance_coverage_unknown": "Local ordinance coverage",
    "local_program_coverage_unknown": "Local program coverage (RSO, JCO or rent control)",
    "year_built_missing": "Year built",
    "year_built_in_cutoff_year": "Exact build date (built in the cutoff year)",
    "units_missing": "Number of units",
    "owner_type_unknown": "Owner type",
    "certificate_of_occupancy_unknown": "Certificate-of-occupancy date",
    "rent_history_unknown": "Rent history",
    "replacement_unit_status_unknown": "Replacement-unit status",
    "property_type_uncertain": "Property type",
    "operative_condition_unresolved": "A rule condition the source doesn't confirm",
    "coverage_condition_unsupported": "A coverage condition LeaseLens can't evaluate",
    "exemption_condition_unsupported": "An exemption LeaseLens can't evaluate",
    "extraction_scope_condition_unresolved": "A scope condition the source leaves open",
    "jurisdiction_unresolved": "Legal jurisdiction (Census couldn't place the address)",
}


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
                    "missing": [_REASON_TEXT.get(x, x) for x in o.reasons], "reasons": list(o.reasons),
                    "causes": o.causes})
    return out


def status_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """The single status-count object behind the status cards, the filter chips and the summary."""
    return {s.key: sum(r["result"] == s.key for r in results) for s in STATUSES}


def glance_lines(counts: dict[str, int]) -> list[tuple[str, str]]:
    """(status key, sentence) for every non-zero status, in status order. The wording and the number come from
    the same status key: {"applies": 43, "unknown": 44} gives "43 rules apply now." and "44 rules remain
    unknown: ..."."""
    return [(s.key, (s.one if counts.get(s.key) == 1 else s.many).format(n=counts[s.key]))
            for s in STATUSES if counts.get(s.key)]


def missing_fact_counts(results: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Missing facts behind this address's Unknown results: (short label, number of rules), most common first."""
    counts: dict[str, int] = {}
    for r in results:
        if r["result"] == "unknown":
            for code in r["reasons"]:
                label = MISSING_FACT_LABELS.get(code, _REASON_TEXT.get(code, code))
                counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


_REVIEW_REASON_TEXT = {
    "non_exact_meaningful_difference": "Census matched a slightly different address",
    "tie_in_attempt_A": "Census returned more than one candidate",
    "tie_in_attempt_C": "Census returned more than one candidate",
    "tie_candidates_share_place": "the candidates are all in the same city",
    "tie_candidates_disagree": "the candidates fall in different cities",
    "no_match_after_bounded_fallbacks": "Census found no match",
}


def review_reason_text(reason: str) -> str:
    """Plain wording for an M4 review reason, e.g. 'street_directional_differs (5TH -> N 5TH)'."""
    code, _, detail = reason.partition(":")
    base = _REVIEW_REASON_TEXT.get(code.strip(), code.strip().replace("_", " "))
    detail = detail.strip().replace("_", " ").replace("->", "\u2192")
    return f"{base}: {detail}" if detail else base


def review_signals(b: Bundle, address_id: str) -> list[dict[str, str]]:
    """Review signals that already exist in the outputs for this address (no new review logic): the M4
    jurisdiction status and the change-test conflict flags."""
    res, out = b.resolutions[address_id], []
    if res["resolution_status"] == "review_required":
        out.append({"kind": "jurisdiction", "title": "Census match needs review",
                    "detail": "; ".join(review_reason_text(x) for x in res["review_reasons"])
                              + ". Results use this jurisdiction."})
    elif res["resolution_status"] == "unresolved":
        out.append({"kind": "jurisdiction", "title": "Jurisdiction unresolved",
                    "detail": "Census couldn't place this address, so LeaseLens doesn't guess a city. "
                              "Every rule stays Unknown."})
    titles = {t["test_id"]: t["title"] for t in b.change_tests}
    for tid, e in b.changes.items():
        if address_id in e["conflict_flag_address_ids"]:
            out.append({"kind": "conflict", "title": f"Conflict flag in change scenario {tid}",
                        "detail": f"{titles.get(tid, tid)}. The state law may preempt a local ordinance here. "
                                  f"LeaseLens flags this for human review and doesn't resolve it."})
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
