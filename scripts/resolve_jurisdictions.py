"""M4: resolve every sample address to its legal jurisdiction with the U.S. Census Geocoder.

Usage:
    uv run python scripts/resolve_jurisdictions.py           # offline: cached Census responses only
    uv run python scripts/resolve_jurisdictions.py --live    # fetch cache misses from Census (no key)

Writes outputs/m4/: jurisdiction_resolutions.json/.csv, jurisdiction_summary.json and
jurisdiction_review_queue.json. Geography only: no rule applicability is decided.
Reads data/sample_addresses.csv (never modified) and the corpus jurisdictions from the
manifest and the published rules. Not legal advice.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator.jurisdiction.census import BENCHMARK, BATCH_URL, POINT_LAYERS, POINT_URL, VINTAGE, CacheMiss, \
    CensusGeocoder  # noqa: E402
from navigator.jurisdiction.crosswalk import Crosswalk, corpus_jurisdictions  # noqa: E402
from navigator.jurisdiction.resolve import (POSTAL_SAME, POSTAL_UNRESOLVED, RESOLVED, RESOLVER, REVIEW,  # noqa: E402
                                            UNRESOLVED, resolve_addresses)
from navigator.starter_pack import ADDRESSES_PATH, MANIFEST_PATH, REPO_ROOT, read_csv, read_json  # noqa: E402

DEFAULT_RULES = Path("outputs/m3/full_v2/rules.json")
DEFAULT_REVIEW = Path("review/m4_jurisdiction_review.json")
CSV_COLUMNS = ("address_id", "street_address", "postal_city", "state", "zip", "resolution_status",
               "state_jurisdiction", "local_jurisdiction", "local_in_corpus", "census_place_name",
               "census_place_geoid", "county", "county_subdivision", "match_indicator", "match_type", "attempt_used",
               "matched_address", "latitude", "longitude", "block_geoid", "postal_city_category", "input_flags",
               "review_reasons", "warnings", "override")


def dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def csv_row(r: dict[str, Any]) -> dict[str, Any]:
    i = r["input_address"]
    return {"address_id": r["address_id"], **i, "resolution_status": r["resolution_status"],
            "state_jurisdiction": r["state_jurisdiction"] or "", "local_jurisdiction": r["local_jurisdiction"] or "",
            "local_in_corpus": "" if r["local_in_corpus"] is None else str(r["local_in_corpus"]).lower(),
            "census_place_name": r["census_place_name"] or "", "census_place_geoid": r["census_place_geoid"] or "",
            "county": (r["county"] or {}).get("name", ""),
            "county_subdivision": (r["county_subdivision"] or {}).get("name", ""),
            "match_indicator": r["match_indicator"] or "", "match_type": r["match_type"] or "",
            "attempt_used": r["attempt_used"] or "", "matched_address": r["matched_address"] or "",
            "latitude": "" if r["latitude"] is None else r["latitude"],
            "longitude": "" if r["longitude"] is None else r["longitude"], "block_geoid": r["block_geoid"] or "",
            "postal_city_category": r["postal_city_audit"]["category"], "input_flags": ";".join(r["input_flags"]),
            "review_reasons": " | ".join(r["review_reasons"]), "warnings": " | ".join(r["warnings"]),
            "override": "yes" if r["override"] else ""}


def review_targets(r: dict[str, Any]) -> list[str]:
    """Section 9: every address that must be inspected by hand."""
    t = []
    if r["resolution_status"] != RESOLVED or r["override"]:
        t.append(r["override"]["census_status"] if r["override"] else r["resolution_status"])
    if r["postal_city_audit"]["category"] not in (POSTAL_SAME, POSTAL_UNRESOLVED):
        t.append("postal_city_differs_from_legal_jurisdiction")
    if r["match_type"] == "Non_Exact":
        t.append("non_exact_match")
    if "zip_outside_state" in r["input_flags"]:
        t.append("zip_outside_state")
    if r["attempt_used"] not in (None, "A"):
        t.append(f"fallback_attempt_{r['attempt_used']}")
    if (r.get("boundary_check") or {}).get("different_place_nearby"):
        t.append("near_place_boundary")
    return t


def summarize(res: list[dict[str, Any]], meta: dict[str, Any], corpus, crosswalk: Crosswalk, rules_path: Path,
              geocoder: CensusGeocoder, overrides: list) -> dict[str, Any]:
    status = Counter(r["resolution_status"] for r in res)
    attempts_run = Counter()
    indicators: dict[str, Counter] = defaultdict(Counter)
    skipped = Counter()
    for r in res:
        for a in r["attempts"]:
            if a["skipped"]:
                skipped[a["attempt"]] += 1
            else:
                attempts_run[a["attempt"]] += 1
                indicators[a["attempt"]][a["indicator"] + (f"/{a['match_type']}" if a["match_type"] else "")] += 1
    postal = Counter(r["postal_city_audit"]["category"] for r in res)
    local = defaultdict(Counter)
    for r in res:
        local[r["local_jurisdiction"] or "(none)"][r["resolution_status"]] += 1
    by_state = defaultdict(Counter)
    for r in res:
        by_state[r["state_jurisdiction"] or "(none)"][r["resolution_status"]] += 1
    coverage = {}
    for name, j in corpus.items():
        rows = [r for r in res if (r["local_jurisdiction"] if j.local else r["state_jurisdiction"]) == name]
        coverage[name] = {"level": "local" if j.local else "state", "published_rules": j.published_rules,
                          "manifest_docs": len(j.manifest_docs),
                          "addresses_resolved": sum(r["resolution_status"] == RESOLVED for r in rows),
                          "addresses_review_required": sum(r["resolution_status"] == REVIEW for r in rows)}
    names = set(corpus)
    joined = [r for r in res if r["resolution_status"] != UNRESOLVED]
    join = {
        "rule_jurisdictions": sorted({n for n, j in corpus.items() if j.published_rules}),
        "state_jurisdictions_not_in_corpus": sorted({r["state_jurisdiction"] for r in joined
                                                     if r["state_jurisdiction"] not in names}),
        "local_jurisdictions_not_in_corpus": sorted({r["local_jurisdiction"] for r in joined
                                                     if r["local_jurisdiction"] and r["local_jurisdiction"] not in names}),
        "in_corpus_flag_consistent": all((r["local_jurisdiction"] in names) == bool(r["local_in_corpus"])
                                         for r in joined if r["local_jurisdiction"]),
    }
    return {
        "resolver": RESOLVER, "not_legal_advice": True,
        "census": {"benchmark": BENCHMARK, "vintage": VINTAGE, "batch_url": BATCH_URL, "point_url": POINT_URL,
                   "point_layers": POINT_LAYERS, "batch_requests": meta["batch_requests"],
                   "point_requests": meta["point_requests"], "tie_lookups": meta["tie_lookups"],
                   "point_failures": meta["point_failures"],
                   "http_requests_this_run": geocoder.stats.requests, "cache_hits_this_run": geocoder.stats.cache_hits,
                   "failures_this_run": geocoder.stats.failures},
        "rules_source": rules_path.as_posix(),
        "totals": {"addresses": len(res), RESOLVED: status[RESOLVED], REVIEW: status[REVIEW],
                   UNRESOLVED: status[UNRESOLVED], "overrides": len(overrides)},
        "match": {"exact": sum(r["match_type"] == "Exact" for r in res),
                  "non_exact": sum(r["match_type"] == "Non_Exact" for r in res),
                  "no_usable_match": sum(r["attempt_used"] is None for r in res),
                  "attempt_used": dict(sorted(Counter(r["attempt_used"] or "none" for r in res).items())),
                  "attempts_run": dict(sorted(attempts_run.items())), "attempts_skipped": dict(sorted(skipped.items())),
                  "indicators_by_attempt": {k: dict(sorted(v.items())) for k, v in sorted(indicators.items())}},
        "input_flags": dict(sorted(Counter(f for r in res for f in r["input_flags"]).items())),
        "review_reason_codes": dict(sorted(Counter(x.split(" ")[0].split(":")[0] for r in res
                                                   for x in r["review_reasons"]).items())),
        "postal_city": {"categories": dict(sorted(postal.items())),
                        "differs_from_legal_jurisdiction": sum(v for k, v in postal.items()
                                                               if k not in (POSTAL_SAME, POSTAL_UNRESOLVED)),
                        "by_pair": dict(sorted(Counter(f"{r['input_address']['postal_city']} -> "
                                                       f"{r['local_jurisdiction'] or '(none)'}"
                                                       for r in res).items()))},
        "boundary_check": {
            "points_checked": sum(r.get("boundary_check") is not None for r in res),
            "different_place_within_about_80m": sum(bool((r.get("boundary_check") or {}).get("different_place_nearby"))
                                                    for r in res),
            "by_pair": dict(sorted(Counter(
                f"{r['local_jurisdiction']} ~ " + " / ".join(sorted({n['census_place_name'] or
                                                                    f"no incorporated place ({n['county_subdivision_name']})"
                                                                    for n in r['boundary_check']['neighbors']
                                                                    if n.get('census_place_geoid') != r['census_place_geoid']
                                                                    and 'error' not in n}))
                for r in res if (r.get("boundary_check") or {}).get("different_place_nearby")).items()))},
        "overrides": [{"address_id": o["address_id"], "corrected_jurisdiction": o["corrected_jurisdiction"],
                       "census_status": next(r["override"]["census_status"] for r in res
                                             if r["address_id"] == o["address_id"])} for o in overrides],
        "by_state": {k: dict(sorted(v.items())) for k, v in sorted(by_state.items())},
        "by_local_jurisdiction": {k: dict(sorted(v.items())) for k, v in sorted(local.items())},
        "coverage": coverage,
        "rule_jurisdictions_with_zero_addresses": sorted(n for n, c in coverage.items()
                                                         if c["published_rules"] and not (c["addresses_resolved"]
                                                                                          + c["addresses_review_required"])),
        "corpus_jurisdictions_with_addresses_but_no_published_rules": sorted(
            n for n, c in coverage.items() if not c["published_rules"]
            and c["addresses_resolved"] + c["addresses_review_required"]),
        "join_check": join,
        "crosswalk": crosswalk.table(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="fetch Census responses that are not cached")
    ap.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    ap.add_argument("--review", type=Path, default=DEFAULT_REVIEW, help="audited overrides and manual-review notes")
    ap.add_argument("--out", type=Path, default=Path("outputs/m4"))
    ap.add_argument("--cache", type=Path, default=Path("cache/census"))
    args = ap.parse_args(argv)
    root = REPO_ROOT
    _, rows = read_csv(root / ADDRESSES_PATH)
    _, manifest = read_csv(root / MANIFEST_PATH)
    rules = read_json(root / args.rules)
    rules = rules["rules"] if isinstance(rules, dict) else rules
    review = read_json(root / args.review) if (root / args.review).is_file() else {"overrides": [], "notes": {}}
    corpus = corpus_jurisdictions(manifest, rules)
    crosswalk = Crosswalk(corpus)
    geocoder = CensusGeocoder(root / args.cache, live=args.live)
    try:
        res, meta = resolve_addresses(rows, geocoder, crosswalk, review.get("overrides", []))
    except CacheMiss as exc:
        print(f"OFFLINE: {exc}", file=sys.stderr)
        return 2
    out = root / args.out
    out.mkdir(parents=True, exist_ok=True)
    dump(out / "jurisdiction_resolutions.json",
         {"resolver": RESOLVER, "not_legal_advice": True, "census": {"benchmark": BENCHMARK, "vintage": VINTAGE},
          "inputs": {"addresses": ADDRESSES_PATH.as_posix(), "manifest": MANIFEST_PATH.as_posix(),
                     "rules": args.rules.as_posix()},
          "resolutions": res})
    with open(out / "jurisdiction_resolutions.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(csv_row(r) for r in res)
    summary = summarize(res, meta, corpus, crosswalk, args.rules, geocoder, review.get("overrides", []))
    dump(out / "jurisdiction_summary.json", summary)
    notes, target_notes = review.get("notes", {}), review.get("target_notes", {})
    queue = [{"address_id": r["address_id"], "targets": t, "resolution_status": r["resolution_status"],
              "input_address": r["input_address"], "local_jurisdiction": r["local_jurisdiction"],
              "census_place_name": r["census_place_name"], "matched_address": r["matched_address"],
              "match_type": r["match_type"], "attempt_used": r["attempt_used"],
              "postal_city_category": r["postal_city_audit"]["category"], "review_reasons": r["review_reasons"],
              "address_differences": r["address_differences"], "warnings": r["warnings"],
              "reviewer_note": (r["override"] or {}).get("reviewer_note") or notes.get(r["address_id"])
              or {x: target_notes[x] for x in t if x in target_notes} or None,
              "override": bool(r["override"]), "boundary_check": r.get("boundary_check")}
             for r in res if (t := review_targets(r))]
    dump(out / "jurisdiction_review_queue.json",
         {"not_legal_advice": True, "count": len(queue),
          "by_target": dict(sorted(Counter(x for q in queue for x in q["targets"]).items())), "queue": queue})
    t = summary["totals"]
    print(f"{t['addresses']} addresses: {t[RESOLVED]} resolved, {t[REVIEW]} review_required, "
          f"{t[UNRESOLVED]} unresolved; overrides {t['overrides']}")
    print(f"match: {summary['match']['exact']} exact, {summary['match']['non_exact']} non-exact; "
          f"attempt used {summary['match']['attempt_used']}")
    print(f"census this run: {geocoder.stats.requests} HTTP requests, {geocoder.stats.cache_hits} cache hits, "
          f"{len(geocoder.stats.failures)} failures")
    print(f"postal city: {summary['postal_city']['categories']}")
    print(f"review queue: {len(queue)} addresses -> {out.relative_to(root).as_posix()}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
