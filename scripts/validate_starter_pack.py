"""Validate the official starter pack and print a concise summary.

Usage:
    python scripts/validate_starter_pack.py [--root PATH] [--json REPORT.json]

Exit status is 1 if any ERROR-level finding is reported, else 0.
Read-only: never modifies starter-pack files.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator.starter_pack import REPO_ROOT  # noqa: E402
from navigator.validation import ERROR, INFO, WARNING, Report, validate_starter_pack  # noqa: E402


def _fmt(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items())


def print_summary(report: Report) -> None:
    c = report.counts
    print("Starter-pack validation  (Not legal advice; structural checks only)")
    print("=" * 68)
    if "schema" in c:
        s = c["schema"]
        print(f"schema         : {s['required_fields']} required / {s['properties']} properties; "
              f"{len(s['categories'] or [])} categories; statuses={s['statuses']}")
    if "rules_template" in c:
        print(f"rules template : {c['rules_template']['records']} record(s), wrapper {c['rules_template']['wrapper']}")
    if "manifest" in c:
        m = c["manifest"]
        print(f"manifest       : {m['rows']} docs; {_fmt(m['by_availability'])}")
        print(f"                 source_type: {_fmt(m['by_source_type'])}")
    if "corpus_text" in c:
        t = c["corpus_text"]
        print(f"corpus/text    : {t['files_on_disk']} files on disk, {t['referenced']} referenced, "
              f"{t['body_characters']:,} body chars")
    if "links_only" in c:
        print(f"links_only.csv : {c['links_only']['rows']} rows")
    if "addresses" in c:
        a = c["addresses"]
        print(f"addresses      : {a['rows']} rows; by state {_fmt(a['by_state'])}")
        print(f"                 missing year_built={a['missing_year_built']}, units={a['missing_units']}, "
              f"zip={a['missing_zip']}; out-of-state zip={a['out_of_state_zip']}")
        for ds, v in a["by_source_dataset"].items():
            print(f"                 - {ds}: {v['rows']} rows, no year={v['missing_year_built']}, "
                  f"no units={v['missing_units']}")
    if "change_tests" in c:
        ct = c["change_tests"]
        print(f"change tests   : {ct['tests']} ({', '.join(ct['ids'])}); types {_fmt(ct['by_type'])}")
    if "lookups_template" in c:
        print(f"lookups templ. : {_fmt(c['lookups_template'])}")
    if "changes_template" in c:
        print(f"changes templ. : tests {c['changes_template']['tests']}")

    for sev in (ERROR, WARNING, INFO):
        items = report.by_severity(sev)
        if items:
            print(f"\n{sev} ({len(items)})")
            for f in items:
                print(f"  [{f.check}] {f.message}")

    n_err, n_warn = len(report.by_severity(ERROR)), len(report.by_severity(WARNING))
    print(f"\nRESULT: {'PASS' if report.ok else 'FAIL'} ({n_err} errors, {n_warn} warnings)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="starter-pack root (default: repo root)")
    parser.add_argument("--json", type=Path, help="also write the full report as JSON")
    args = parser.parse_args(argv)

    report = validate_starter_pack(args.root)
    print_summary(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ok": report.ok, "counts": report.counts, "findings": [asdict(f) for f in report.findings]}
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
