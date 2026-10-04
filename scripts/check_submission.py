"""Check the final submission package in outputs/submission/. Read-only; offline.

Usage:
    uv run python scripts/check_submission.py

Checks:
- rules.json, lookups.json, changes.json are byte-identical to the authoritative outputs
  (outputs/m3/full_v2, outputs/m5, outputs/m6);
- rules: every record validates against schema/rule_record.schema.json; ids unique; source
  document in the corpus manifest; a citation and a quote on every record;
- lookups: wrapper and entry keys match the template; all sample address ids exactly once;
  results in the official enum; every rule id exists in rules.json; no duplicate rule per address;
- changes: T1-T5 exactly once and in order; official keys; address ids valid and unique;
  conflict flags inside the affected set;
- no API key in tracked or to-be-committed files (Google key pattern, a filled GEMINI_API_KEY=
  assignment, and the live key value when GEMINI_API_KEY is set; the value is never printed).
Exit status 1 on any problem. Not legal advice.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator.starter_pack import (ADDRESSES_PATH, CHANGE_TESTS_PATH, LOOKUP_RESULT_VALUES, MANIFEST_PATH,  # noqa: E402
                                    REPO_ROOT, SCHEMA_PATH, TEMPLATES_DIR, read_csv, read_json)
from navigator.validation import make_rule_validator, rule_records  # noqa: E402

SUBMISSION = Path("outputs/submission")
AUTHORITATIVE = {"rules.json": Path("outputs/m3/full_v2/rules.json"), "lookups.json": Path("outputs/m5/lookups.json"),
                 "changes.json": Path("outputs/m6/changes.json")}
KEY_PATTERNS = (re.compile(rb"AIza[0-9A-Za-z_\-]{35}"), re.compile(rb"GEMINI_API_KEY\s*=\s*['\"]?[A-Za-z0-9_\-]{20,}"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(root: Path = REPO_ROOT) -> tuple[list[str], dict]:
    problems: list[str] = []
    sub = root / SUBMISSION
    for name, src in AUTHORITATIVE.items():
        if not (sub / name).exists():
            problems.append(f"{name}: missing from {SUBMISSION}")
        elif sha(sub / name) != sha(root / src):
            problems.append(f"{name}: differs from {src}")
    if problems:
        return problems, {}

    # ---- rules
    rules = rule_records(read_json(sub / "rules.json")) or []
    validator = make_rule_validator(read_json(root / SCHEMA_PATH))
    _, manifest = read_csv(root / MANIFEST_PATH)
    docs = {r["doc_id"] for r in manifest}
    ids = [r.get("team_rule_id") for r in rules]
    problems += [f"rules: duplicate id {i}" for i, n in Counter(ids).items() if n > 1]
    for r in rules:
        problems += [f"rules {r.get('team_rule_id')}: {e.message}" for e in validator.iter_errors(r)]
        if r.get("source_doc_id") not in docs:
            problems.append(f"rules {r['team_rule_id']}: source_doc_id not in the manifest")
        if not r.get("citation") or not r.get("quoted_span"):
            problems.append(f"rules {r['team_rule_id']}: no citation or quote")
    rule_ids = set(ids)

    # ---- lookups
    lookups = read_json(sub / "lookups.json")
    template = read_json(root / TEMPLATES_DIR / "lookups.json")
    entry_keys = list(next(iter(template["lookups"].values()))[0])
    _, rows = read_csv(root / ADDRESSES_PATH)
    address_ids = [r["address_id"] for r in rows]
    if set(lookups) != set(template):
        problems.append(f"lookups: wrapper keys {sorted(lookups)}")
    if sorted(lookups["lookups"]) != sorted(address_ids) or len(set(address_ids)) != len(address_ids):
        problems.append("lookups: address ids are not the sample ids exactly once")
    results = Counter()
    for aid, entries in lookups["lookups"].items():
        seen = [e.get("team_rule_id") for e in entries]
        if len(seen) != len(set(seen)):
            problems.append(f"lookups {aid}: duplicate rule")
        for e in entries:
            results[e.get("result")] += 1
            if list(e) != entry_keys:
                problems.append(f"lookups {aid}: entry keys {list(e)}")
            if e.get("result") not in LOOKUP_RESULT_VALUES:
                problems.append(f"lookups {aid}: result {e.get('result')}")
            if e.get("team_rule_id") not in rule_ids:
                problems.append(f"lookups {aid}: unknown rule {e.get('team_rule_id')}")

    # ---- changes
    changes = read_json(sub / "changes.json")
    tests = [t["test_id"] for t in read_json(root / CHANGE_TESTS_PATH)]
    if list(changes) != tests:
        problems.append(f"changes: tests {list(changes)} != {tests}")
    valid = set(address_ids)
    for tid, e in changes.items():
        if set(e) != {"affected_address_ids", "conflict_flag_address_ids", "notes"}:
            problems.append(f"changes {tid}: keys {sorted(e)}")
        for k in ("affected_address_ids", "conflict_flag_address_ids"):
            if not set(e[k]) <= valid or len(e[k]) != len(set(e[k])):
                problems.append(f"changes {tid}: {k} has unknown or duplicate ids")
        if not set(e["conflict_flag_address_ids"]) <= set(e["affected_address_ids"]):
            problems.append(f"changes {tid}: conflict flags outside the affected set")

    # ---- secrets
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
                           capture_output=True, check=True).stdout.decode().split("\0")
    live = os.environ.get("GEMINI_API_KEY", "").encode()
    leaks = []
    for f in filter(None, files):
        p = root / f
        if not p.is_file():
            continue
        data = p.read_bytes()
        if any(rx.search(data) for rx in KEY_PATTERNS) or (len(live) > 10 and live in data):
            leaks.append(f)
    problems += [f"possible API key in {f}" for f in leaks]

    counts = {"rules": len(rules), "lookup_addresses": len(lookups["lookups"]), "lookup_results": dict(results),
              "change_tests": {t: len(e["affected_address_ids"]) for t, e in changes.items()},
              "files_scanned_for_keys": len([f for f in files if f])}
    return problems, counts


def main() -> int:
    problems, counts = check()
    for k, v in counts.items():
        print(f"{k}: {v}")
    if problems:
        print("SUBMISSION CHECK FAILED:", *problems[:20], sep="\n  ", file=sys.stderr)
        return 1
    print("SUBMISSION CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
