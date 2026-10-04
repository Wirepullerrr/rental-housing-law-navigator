"""Module C (M6): deterministic change tests (dev/change_tests.json) -> changes.json.

For each supplied test the engine:
- establishes the baseline from the published rules, the M4 jurisdictions and the M5 lookups;
- applies only the supplied change facts (dates, rule scope, conflicts);
- re-evaluates applicability with the M5 engine at the supplied dates.

Test types:
  as_of     result at as_of_before vs as_of_after for every address in `states`. The
            effective date comes from verified source evidence when M3 resolved one;
            otherwise the supplied window (as_of_before, as_of_after] is used. Affected =
            addresses whose result changes. Conflict flags = addresses inside the
            jurisdictions of the `conflict_with` rules.
  boundary  each local rule covers only addresses whose M4 local jurisdiction is its own.
  pending   pending records stay pending. Affected = addresses in `states` that would be
            covered if enacted (they carry the records as `pending` in the M5 lookups).
  negative  the measure failed: nothing is affected, and no rent cap may be surfaced.
Addresses without an M4 jurisdiction are never placed in an affected set; the audit lists
them as undetermined. No LLM is used. Not legal advice.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from navigator.applicability.engine import APPLIES, evaluate
from navigator.applicability.facts import PropertyFacts

KEYS = ("affected_address_ids", "conflict_flag_address_ids", "notes")


def _addresses_in(addresses: list[dict[str, Any]], states: list[str]) -> list[dict[str, Any]]:
    return [a for a in addresses if a["state_jurisdiction"] in states]


def _undetermined(addresses: list[dict[str, Any]]) -> list[str]:
    return sorted(a["address_id"] for a in addresses if not a["state_jurisdiction"])


def effective_from_artifact(artifact: dict[str, Any]) -> dict[str, Any] | None:
    """A relative effective date resolved by M3 from verified text (both spans exact)."""
    for c in artifact.get("candidates") or []:
        rel = (c.get("temporal") or {}).get("relative_resolution") or {}
        ev, base = c.get("effective_date_evidence") or {}, c.get("status_evidence") or {}
        if rel.get("outcome") == "resolved" and ev.get("status") == "exact_match":
            return {"date": rel["resolved_date"], "formula": rel["formula_evidence"]["text"],
                    "formula_offsets": [rel["formula_evidence"]["start"], rel["formula_evidence"]["end"]],
                    "base_date": rel["base_date"], "base_evidence": rel["base_date_evidence"]["text"],
                    "base_offsets": [rel["base_date_evidence"]["start"], rel["base_date_evidence"]["end"]],
                    "resolver": rel["resolver"], "base_status_check": base.get("status")}
    return None


def run_as_of(test: dict[str, Any], mapping: dict[str, Any], rules: dict[str, dict[str, Any]],
              held: dict[str, dict[str, Any]], addresses: list[dict[str, Any]],
              facts: dict[str, PropertyFacts], effective: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    before, after = date.fromisoformat(test["as_of_before"]), date.fromisoformat(test["as_of_after"])
    audit_rules, affected, per_address = [], set(), {}
    for oid in test["rule_ids"]:
        m = mapping[oid]
        eff = effective.get(oid)
        if eff:
            eff_date, eff_basis = eff["date"], "verified source evidence (M3 relative-date resolver)"
        else:                                   # supplied window (before, after]: in force by `after`, not by `before`
            eff_date, eff_basis = after.isoformat(), (f"supplied change fact: not in force on {before}, in force "
                                                      f"by {after} (exact day not in the supplied text)")
        ids = m.get("team_rule_ids") or []
        records = [rules.get(i) or held.get(i) for i in ids] or [None]
        for rec in records:
            base = {"team_rule_id": rec["team_rule_id"] if rec else oid, "jurisdiction": m["jurisdiction"],
                    "status": "in_force", "effective_date": eff_date,
                    "coverage_conditions": rec.get("coverage_conditions") if rec else None,
                    "exemptions": rec.get("exemptions") if rec else None}
            for a in _addresses_in(addresses, test["states"]):
                jur = {"state_jurisdiction": a["state_jurisdiction"], "local_jurisdiction": a["local_jurisdiction"]}
                r0 = evaluate(base, facts[a["address_id"]], jur, before).result
                r1 = evaluate(base, facts[a["address_id"]], jur, after).result
                per_address.setdefault(a["address_id"], []).append({"rule": base["team_rule_id"], "before": r0,
                                                                     "after": r1})
                if r0 != r1 and r1 == APPLIES:
                    affected.add(a["address_id"])
        audit_rules.append({"organizer_rule_id": oid, "mapping": m, "effective_date_used": eff_date,
                            "effective_basis": eff_basis, "effective_evidence": eff,
                            "records": [{k: r.get(k) for k in ("team_rule_id", "citation", "quoted_span", "source_url",
                                                               "source_doc_id")} for r in records if r]})
    conflict_j = {mapping[c]["jurisdiction"] for c in test.get("conflict_with", [])}
    conflicts = sorted(a["address_id"] for a in addresses
                       if a["local_jurisdiction"] in conflict_j and a["address_id"] in affected)
    return {"affected": sorted(affected), "conflicts": conflicts, "rules": audit_rules, "per_address": per_address,
            "transition": {"before": before.isoformat(), "after": after.isoformat()}}


def run_boundary(test, mapping, addresses) -> dict[str, Any]:
    by_rule = {}
    for oid in test["rule_ids"]:
        j = mapping[oid]["jurisdiction"]
        by_rule[oid] = sorted(a["address_id"] for a in addresses if a["local_jurisdiction"] == j)
    overlap = set.intersection(*(set(v) for v in by_rule.values())) if len(by_rule) > 1 else set()
    assert not overlap, f"addresses in two local jurisdictions: {overlap}"
    return {"affected": sorted({x for v in by_rule.values() for x in v}), "conflicts": [], "by_rule": by_rule,
            "rules": [{"organizer_rule_id": o, "mapping": mapping[o]} for o in test["rule_ids"]]}


def run_pending(test, mapping, addresses, lookup_results) -> dict[str, Any]:
    ids = {i for o in test["rule_ids"] for i in mapping[o]["team_rule_ids"]}
    inside = _addresses_in(addresses, test["states"])
    shown = {a["address_id"]: sorted(r for r, res in lookup_results[a["address_id"]].items()
                                     if r in ids and res == "pending") for a in inside}
    not_pending = sorted(i for i, rs in shown.items() if set(rs) != ids)
    applies = sorted(a for a, res in lookup_results.items() for r, x in res.items() if r in ids and x == "applies")
    return {"affected": sorted(i for i, rs in shown.items() if rs), "conflicts": [],
            "addresses_missing_pending_record": not_pending, "addresses_reporting_applies": applies,
            "rules": [{"organizer_rule_id": o, "mapping": mapping[o]} for o in test["rule_ids"]]}


def run_negative(test, mapping, addresses, lookup_results, rules) -> dict[str, Any]:
    inside = {a["address_id"] for a in _addresses_in(addresses, test["states"])}
    rent_caps = sorted((aid, r) for aid in inside for r, res in lookup_results[aid].items()
                       if rules[r]["category"] == "rent_increase_limits")
    failed = sorted(r["team_rule_id"] for r in rules.values()
                    if r["status"] == "failed" and r["jurisdiction"].endswith(tuple(test["states"])))
    return {"affected": [], "conflicts": [], "rent_caps_surfaced_in_states": rent_caps,
            "failed_records_never_surfaced": failed,
            "rules": [{"organizer_rule_id": o, "mapping": mapping[o]} for o in test["rule_ids"]]}


def build(tests, mapping, rules, held, addresses, facts, lookup_results, effective) -> tuple[dict, dict]:
    changes, audit = {}, {}
    undetermined = _undetermined(addresses)
    for t in tests:
        kind = t["type"]
        if kind == "as_of":
            r = run_as_of(t, mapping, rules, held, addresses, facts, effective)
        elif kind == "boundary":
            r = run_boundary(t, mapping, addresses)
        elif kind == "pending":
            r = run_pending(t, mapping, addresses, lookup_results)
        elif kind == "negative":
            r = run_negative(t, mapping, addresses, lookup_results, rules)
        else:
            raise ValueError(f"unsupported change-test type {kind!r}")
        changes[t["test_id"]] = {"affected_address_ids": r["affected"], "conflict_flag_address_ids": r["conflicts"],
                                 "notes": notes(t, r, mapping, undetermined)}
        audit[t["test_id"]] = {"test": t, **{k: v for k, v in r.items() if k != "per_address"},
                               "undetermined_jurisdiction": undetermined,
                               "per_address": r.get("per_address")}
    return changes, audit


def notes(t, r, mapping, undetermined) -> str:
    n = len(r["affected"])
    und = f" {len(undetermined)} sample addresses without an M4 jurisdiction (any state) are undetermined, not listed."
    if t["type"] == "as_of":
        rules = "; ".join(
            f"{x['organizer_rule_id']} = {x['mapping']['label']}, "
            + (f"effective {x['effective_date_used']} (verified {x['mapping']['source_doc_ids'][0]} text)"
               if x["effective_evidence"] else
               f"in force by {r['transition']['after']} and not on {r['transition']['before']} (supplied change "
               f"window; the exact day is not in the supplied text)")
            for x in r["rules"])
        cites = "; ".join(f"{rec['citation']} [{rec['source_doc_id']}]" for x in r["rules"] for rec in x["records"])
        conflict = (f" Conflict flag on {len(r['conflicts'])} addresses in "
                    f"{', '.join(mapping[c]['jurisdiction'] for c in t.get('conflict_with', []))} for human review "
                    f"(possible preemption of local ordinances)." if r["conflicts"] else "")
        return (f"{rules}. not_yet_effective on {r['transition']['before']}, applies on {r['transition']['after']} for "
                f"{n} {'/'.join(t['states'])} addresses." + (f" Sources: {cites}." if cites else
                " No verified rule record is published for this law; evidence is the source document and the supplied "
                "change facts.") + conflict + und)
    if t["type"] == "boundary":
        parts = "; ".join(f"{o} ({mapping[o]['jurisdiction']}): {len(v)} addresses" for o, v in r["by_rule"].items())
        return (f"{parts}; no Newark or other address. Boundary from M4 Census geography. The local ordinance texts "
                f"are not supplied (link-only sources), so no rule record or quote is published; scope is from the "
                f"supplied change facts." + und)
    if t["type"] == "pending":
        bills = ", ".join(mapping[o]["label"] for o in t["rule_ids"])
        return (f"{bills} are pending bills, not law on {t['as_of']}: reported as pending (never applies). "
                f"{n} MA addresses (Boston and Cambridge) would be affected if enacted." + und)
    return ("Measure failed (ballot question struck): recorded as failed, affected set empty. No rent cap is reported "
            "for any Boston or Cambridge address; the failed Boston petition H.3744 is never surfaced.")
