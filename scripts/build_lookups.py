"""M5: deterministic rule applicability for every sample address -> outputs/m5/.

Usage:
    uv run python scripts/build_lookups.py [--as-of 2026-10-01]

Inputs: outputs/m3/full_v2/rules.json (+ its document artifacts), outputs/m4/
jurisdiction_resolutions.json, data/sample_addresses.csv. No network, no LLM.
Writes lookups.json (starter-pack wrapper), lookups_audit.json, lookup_summary.json and
rule_coverage_gaps.json. Not legal advice.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator.applicability.conditions import classify, split_clauses  # noqa: E402
from navigator.applicability.engine import (ENGINE, RESULTS, UNKNOWN, evaluate, scope_gaps)  # noqa: E402
from navigator.applicability.facts import code_labels, facts_from_row  # noqa: E402
from navigator.starter_pack import (ADDRESSES_PATH, LOOKUP_RESULT_VALUES, REPO_ROOT, TEMPLATES_DIR,  # noqa: E402
                                    read_csv, read_json)

RULES = Path("outputs/m3/full_v2/rules.json")
ARTIFACTS = "outputs/m3/full_v2/documents/*_extraction.json"
RESOLUTIONS = Path("outputs/m4/jurisdiction_resolutions.json")
OUT = Path("outputs/m5")
GEO_NOTE = " Jurisdiction flagged for review in M4."


def dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def condition_inventory(rules: list[dict[str, Any]]) -> dict[str, Any]:
    kinds, atoms, rules_by_kind = Counter(), Counter(), defaultdict(set)
    unsupported = []
    for r in rules:
        for role, field in (("coverage", "coverage_conditions"), ("exemption", "exemptions")):
            for text in split_clauses(r.get(field)):
                c = classify(text, role)
                kinds[f"{role}:{c.kind}"] += 1
                rules_by_kind[f"{role}:{c.kind}"].add(r["team_rule_id"])
                for a in c.atoms:
                    atoms[f"{role}:{a.kind}"] += 1
                if c.kind == "unsupported":
                    unsupported.append({"team_rule_id": r["team_rule_id"], "role": role, "clause": text})
    return {"rules": len(rules),
            "rules_with_coverage_text": sum(bool(r.get("coverage_conditions")) for r in rules),
            "rules_with_exemption_text": sum(bool(r.get("exemptions")) for r in rules),
            "clauses_by_kind": dict(sorted(kinds.items())),
            "rules_by_clause_kind": {k: len(v) for k, v in sorted(rules_by_kind.items())},
            "atoms": dict(sorted(atoms.items())), "unsupported_clauses": unsupported}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--as-of", default="2026-10-01")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    as_of = date.fromisoformat(args.as_of)
    root = REPO_ROOT
    rules = read_json(root / RULES)["rules"]
    by_id = {r["team_rule_id"]: r for r in rules}
    resolutions = {r["address_id"]: r for r in read_json(root / RESOLUTIONS)["resolutions"]}
    _, rows = read_csv(root / ADDRESSES_PATH)
    labels = code_labels(rows)
    gaps = scope_gaps([read_json(Path(p)) for p in sorted(glob.glob(str(root / ARTIFACTS)))])
    gaps = {rid: g for rid, g in gaps.items() if rid in by_id}             # published records only
    template = read_json(root / TEMPLATES_DIR / "lookups.json")
    entry_keys = list(next(iter(template["lookups"].values()))[0].keys())

    order = {r["team_rule_id"]: (r["level"] != "state", r["category"], r["jurisdiction"], r["team_rule_id"])
             for r in rules}
    lookups: dict[str, list[dict[str, Any]]] = {}
    clause_ids: dict[str, int] = {}
    audit: list[dict[str, Any]] = []
    omitted = Counter()
    for row in rows:
        aid = row["address_id"]
        res = resolutions[aid]
        facts = facts_from_row(row, labels)
        jur = {"state_jurisdiction": res["state_jurisdiction"], "local_jurisdiction": res["local_jurisdiction"]}
        geo_review = res["resolution_status"] == "review_required"
        entries, details = [], []
        for rid in sorted(by_id, key=order.get):
            o = evaluate(by_id[rid], facts, jur, as_of, gaps.get(rid))
            if o.result is None:
                omitted[o.omitted_because.split(":")[0]] += 1
                if o.omitted_because.startswith(("coverage_false", "exempt")):
                    details.append({"team_rule_id": rid, "result": None, "omitted_because": o.omitted_because})
                continue
            rule = by_id[rid]
            explanation = o.explanation + (GEO_NOTE if geo_review else "")
            entries.append({"team_rule_id": rid, "result": o.result, "explanation": explanation,
                            "conflict_flag": bool(rule.get("conflict_flag"))})
            details.append({"team_rule_id": rid, "result": o.result, "category": rule["category"],
                            "reason": explanation, "unknown_reasons": o.reasons, "missing_facts": o.missing_facts,
                            "causes": [{"role": c["role"], "clause": clause_ids.setdefault(c["clause"], len(clause_ids)),
                                        "reasons": c["reasons"]} for c in o.causes]})
        lookups[aid] = [{k: e[k] for k in entry_keys} for e in entries]
        audit.append({"address_id": aid, "jurisdiction_status": res["resolution_status"],
                      "state_jurisdiction": res["state_jurisdiction"], "local_jurisdiction": res["local_jurisdiction"],
                      "jurisdiction_review_required": geo_review, "jurisdiction_override": bool(res.get("override")),
                      "geography_warnings": res["warnings"], "facts": {
                          "year_built": facts.year_built, "units_min": facts.units_min, "units_max": facts.units_max,
                          "units_source": facts.units_source, "use_class": facts.use_class,
                          "use_description": facts.use_description, "notes": list(facts.notes)},
                      "results": details})

    out_doc = {"as_of": as_of.isoformat(), "lookups": lookups}
    # ---- validation against the starter-pack contract
    ids = [r["address_id"] for r in rows]
    problems = []
    if set(out_doc) != set(template):
        problems.append(f"wrapper keys {sorted(out_doc)} != template {sorted(template)}")
    if sorted(lookups) != sorted(ids) or len(ids) != len(set(ids)):
        problems.append("address ids differ from data/sample_addresses.csv")
    for aid, entries in lookups.items():
        seen = set()
        for e in entries:
            if list(e) != entry_keys:
                problems.append(f"{aid}: entry keys {list(e)}")
            if e["result"] not in LOOKUP_RESULT_VALUES:
                problems.append(f"{aid}: result {e['result']}")
            if e["team_rule_id"] not in by_id:
                problems.append(f"{aid}: unknown rule {e['team_rule_id']}")
            if e["team_rule_id"] in seen:
                problems.append(f"{aid}: duplicate rule {e['team_rule_id']}")
            seen.add(e["team_rule_id"])
    if problems:
        print("VALIDATION FAILED:", problems[:10], file=sys.stderr)
        return 1

    out = root / args.out
    out.mkdir(parents=True, exist_ok=True)
    dump(out / "lookups.json", out_doc)
    rule_table = {rid: {k: r.get(k) for k in ("title", "category", "jurisdiction", "level", "status", "requirement",
                                              "citation", "quoted_span", "source_url", "source_doc_id",
                                              "effective_date")} for rid, r in sorted(by_id.items())}
    dump(out / "lookups_audit.json", {
        "engine": ENGINE, "as_of": as_of.isoformat(), "not_legal_advice": True,
        "note": "results[].causes[].clause indexes `clauses`; rule text, citation and quote are in `rules`.",
        "rules": rule_table, "clauses": [c for c, _ in sorted(clause_ids.items(), key=lambda kv: kv[1])],
        "addresses": audit})

    # ---- summary
    surfaced = [d | {"_a": a} for a in audit for d in a["results"] if d["result"]]
    res_counts = Counter(d["result"] for d in surfaced)
    status_of = Counter(a["jurisdiction_status"] for a in audit)
    unknown = [d for d in surfaced if d["result"] == UNKNOWN]
    by_reason = Counter(x for d in unknown for x in d["unknown_reasons"])
    by_fact = Counter(x for d in unknown for x in d["missing_facts"])
    rule_level = {r["team_rule_id"]: r["level"] for r in rules}
    state_matches = Counter(d["_a"]["state_jurisdiction"] for d in surfaced
                            if rule_level[d["team_rule_id"]] == "state" and d["_a"]["state_jurisdiction"])
    local_matches = Counter(d["_a"]["local_jurisdiction"] for d in surfaced
                            if rule_level[d["team_rule_id"]] == "city" and d["_a"]["local_jurisdiction"])
    local_rules = {r["jurisdiction"] for r in rules if r["level"] == "city" and r["status"] != "failed"}
    no_local = Counter(a["local_jurisdiction"] for a in audit
                       if a["local_jurisdiction"] and a["local_jurisdiction"] not in local_rules)
    summary = {
        "engine": ENGINE, "as_of": as_of.isoformat(), "not_legal_advice": True,
        "addresses": {"total": len(rows), "lookup_rows": len(lookups),
                      "resolved_jurisdiction": status_of["resolved"],
                      "review_jurisdiction": status_of["review_required"],
                      "unresolved_jurisdiction": status_of["unresolved"],
                      "with_override": sum(a["jurisdiction_override"] for a in audit),
                      "with_no_surfaced_rule": sum(not v for v in lookups.values())},
        "rule_results": {**{k: res_counts.get(k, 0) for k in RESULTS},
                         "omitted_definitely_not_applicable": omitted["coverage_false"] + omitted["exempt"],
                         "omitted_coverage_false": omitted["coverage_false"], "omitted_exempt": omitted["exempt"],
                         "omitted_failed_status": omitted["status_failed"],
                         "not_candidates_other_jurisdiction": omitted["other_jurisdiction"]},
        "unknown": {"addresses_with_unknown": sum(any(d["result"] == UNKNOWN for d in a["results"]) for a in audit),
                    "by_reason": dict(by_reason.most_common()), "missing_facts": dict(by_fact.most_common()),
                    "unknown_only_from_jurisdiction": sum(d["unknown_reasons"] == ["jurisdiction_unresolved"]
                                                          for d in unknown)},
        "facts": {"year_built_missing": sum(a["facts"]["year_built"] is None for a in audit),
                  "units_by_source": dict(Counter(a["facts"]["units_source"] for a in audit)),
                  "use_class": dict(Counter(str(a["facts"]["use_class"]) for a in audit))},
        "jurisdiction": {"state_rule_results_by_state": dict(sorted(state_matches.items())),
                         "local_rule_results_by_city": dict(sorted(local_matches.items())),
                         "addresses_in_cities_without_published_local_rules": dict(sorted(no_local.items()))},
        "category": {cat: dict(sorted(Counter(d["result"] for d in surfaced
                                              if d["category"] == cat).items()))
                     for cat in sorted({d["category"] for d in surfaced})},
        "results_by_rule": {rid: dict(Counter(d["result"] for d in surfaced if d["team_rule_id"] == rid))
                            for rid in sorted(by_id)},
        "condition_inventory": condition_inventory(rules),
        "extraction_scope_gaps": gaps,
        "validation": {"wrapper_matches_template": True, "entry_keys": entry_keys, "all_ids_once": True,
                       "results_in_official_enum": True, "rule_ids_exist": True},
    }
    dump(out / "lookup_summary.json", summary)

    corpus_locals = sorted({a["local_jurisdiction"] for a in audit if a["local_jurisdiction"]})
    gaps_doc = {
        "not_legal_advice": True,
        "local_jurisdictions_without_published_rules": [
            {"jurisdiction": j, "addresses": no_local[j],
             "note": "No published local rule in outputs/m3/full_v2/rules.json; only state rules are evaluated. "
                     "No local rule is fabricated."} for j in corpus_locals if j in no_local],
        "known_extraction_gaps": [
            {"doc": "D067", "jurisdiction": "NJ", "categories": ["just_cause_eviction", "security_deposits"],
             "note": "NJ Anti-Eviction Act grounds and most NJ security-deposit law exist only in D067, whose 83 "
                     "records are held (official explanatory guide without effective dates). Not published, "
                     "not used."},
            {"doc": "D069", "jurisdiction": "NJ", "categories": ["algorithmic_rent_setting"],
             "note": "Rejected record (quote not verifiable without inserted brackets). Not used."},
            {"doc": "D027", "jurisdiction": "CA", "categories": ["screening_restrictions"],
             "note": "7 published records; unresolved scope conditions (S1 government rent subsidy, S3 income "
                     "inquiry carve-out) make the affected records unknown.",
             "affected_rules": sorted(r for r in gaps if by_id.get(r, {}).get("source_doc_id") == "D027")},
        ],
        "extraction_scope_gaps": gaps,
        "change_test_note": "T2 (Hoboken vs Jersey City algorithmic ordinances) has no published local rule; it is "
                            "an M6 prerequisite.",
    }
    dump(out / "rule_coverage_gaps.json", gaps_doc)
    s = summary
    print(f"{s['addresses']['lookup_rows']} lookup rows; results {s['rule_results']}")
    print(f"unknown reasons: {s['unknown']['by_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
