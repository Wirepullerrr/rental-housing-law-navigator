"""M6: run the supplied change tests (dev/change_tests.json) -> outputs/m6/changes.json.

Usage:
    uv run python scripts/build_changes.py

Inputs: dev/change_tests.json, submission_templates/changes.json, outputs/m3/full_v2
(rules + artifacts), outputs/m4/jurisdiction_resolutions.json, outputs/m5/lookups.json,
review/m6_change_test_map.json, outputs/m6/targeted/ (D069 artifact). Offline; no LLM.
Not legal advice.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator.applicability.facts import code_labels, facts_from_row  # noqa: E402
from navigator.changes import KEYS, build, effective_from_artifact  # noqa: E402
from navigator.starter_pack import (ADDRESSES_PATH, CHANGE_TESTS_PATH, REPO_ROOT, TEMPLATES_DIR, read_csv,  # noqa: E402
                                    read_json)

OUT = Path("outputs/m6")


def dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    root = REPO_ROOT
    out = Path(argv[0]) if argv else root / OUT
    tests = read_json(root / CHANGE_TESTS_PATH)
    template = read_json(root / TEMPLATES_DIR / "changes.json")
    mapping = read_json(root / "review/m6_change_test_map.json")["rules"]
    rules = {r["team_rule_id"]: r for r in read_json(root / "outputs/m3/full_v2/rules.json")["rules"]}
    held: dict[str, dict[str, Any]] = {}
    for m in mapping.values():
        if m["basis"] == "verified_held_records":
            for doc in m["source_doc_ids"]:
                art = read_json(root / f"outputs/m3/full_v2/documents/{doc}_extraction.json")
                for c in art["candidates"]:
                    rid = (c.get("rule") or {}).get("team_rule_id")
                    if rid in m["team_rule_ids"]:
                        if not (c["held"] and c["citation"]["status"] in ("exact_match", "normalized_match")):
                            raise SystemExit(f"{rid}: not a held record with a verified quote")
                        held[rid] = c["rule"]
    effective = {}
    for oid, m in mapping.items():
        if m.get("effective_date_artifact"):
            eff = effective_from_artifact(read_json(root / m["effective_date_artifact"]))
            if eff is None:
                raise SystemExit(f"{oid}: no verified effective date in {m['effective_date_artifact']}")
            effective[oid] = eff
    resolutions = read_json(root / "outputs/m4/jurisdiction_resolutions.json")["resolutions"]
    _, rows = read_csv(root / ADDRESSES_PATH)
    labels = code_labels(rows)
    facts = {r["address_id"]: facts_from_row(r, labels) for r in rows}
    addresses = [{"address_id": r["address_id"], "state_jurisdiction": r["state_jurisdiction"],
                  "local_jurisdiction": r["local_jurisdiction"], "status": r["resolution_status"]}
                 for r in resolutions]
    lookups = read_json(root / "outputs/m5/lookups.json")["lookups"]
    lookup_results = {aid: {e["team_rule_id"]: e["result"] for e in es} for aid, es in lookups.items()}

    changes, audit = build(tests, mapping, rules, held, addresses, facts, lookup_results, effective)

    # ---- validation
    problems = []
    ids = {r["address_id"] for r in rows}
    if [t["test_id"] for t in tests] != list(changes) or len(set(changes)) != len(tests):
        problems.append("test ids differ from dev/change_tests.json")
    template_keys = {k for v in template.values() for k in v}
    for tid, entry in changes.items():
        if set(entry) != set(KEYS) or not set(entry) >= template_keys - {"conflict_flag_address_ids"}:
            problems.append(f"{tid}: keys {sorted(entry)}")
        for k in ("affected_address_ids", "conflict_flag_address_ids"):
            if not set(entry[k]) <= ids or len(entry[k]) != len(set(entry[k])):
                problems.append(f"{tid}: {k} has unknown or duplicate ids")
        if not set(entry["conflict_flag_address_ids"]) <= set(entry["affected_address_ids"]):
            problems.append(f"{tid}: conflict flags outside the affected set")
    used = {i for m in mapping.values() for i in m.get("team_rule_ids", [])}
    for i in used:
        rec = rules.get(i) or held.get(i)
        if rec is None or not rec.get("citation") or not rec.get("quoted_span"):
            problems.append(f"rule {i} missing or without citation")
    if audit["T4"]["addresses_missing_pending_record"] or audit["T4"]["addresses_reporting_applies"]:
        problems.append("T4: a pending bill is not reported as pending everywhere")
    if audit["T5"]["rent_caps_surfaced_in_states"]:
        problems.append("T5: a rent cap is surfaced in MA")
    if problems:
        print("VALIDATION FAILED:", problems, file=sys.stderr)
        return 1

    out.mkdir(parents=True, exist_ok=True)
    dump(out / "changes.json", changes)
    dump(out / "changes_audit.json", {"not_legal_advice": True, "engine": "changes/m6-v1",
                                      "rule_id_map": "review/m6_change_test_map.json",
                                      "held_records_used": held, "tests": audit})
    status_of = {a["address_id"]: a["status"] for a in addresses}
    summary = {"not_legal_advice": True, "tests": {
        tid: {"type": audit[tid]["test"]["type"], "affected": len(e["affected_address_ids"]),
              "conflict_flags": len(e["conflict_flag_address_ids"]),
              "affected_with_review_geography": sum(status_of[a] == "review_required"
                                                    for a in e["affected_address_ids"]),
              "affected_by_local_jurisdiction": dict(Counter(
                  next(x["local_jurisdiction"] for x in addresses if x["address_id"] == a)
                  for a in e["affected_address_ids"])),
              "bases": sorted({r["mapping"]["basis"] for r in audit[tid]["rules"]})}
        for tid, e in changes.items()},
        "validation": {"tests_once": True, "keys": list(KEYS), "address_ids_valid": True,
                       "rule_ids_and_citations_valid": sorted(used)}}
    dump(out / "change_summary.json", summary)
    for tid, s in summary["tests"].items():
        print(tid, s["type"], "affected", s["affected"], "conflicts", s["conflict_flags"], s["bases"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
