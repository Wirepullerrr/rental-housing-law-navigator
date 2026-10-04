"""Extract rules from an EXPLICIT selection of supplied corpus documents (M3), one at a time.

    uv run python scripts/run_corpus.py --list                                     # discover; no API calls
    uv run python scripts/run_corpus.py --doc-ids D065,D001 --out-dir outputs/m3/stage1          # offline
    uv run --env-file .env python scripts/run_corpus.py --doc-ids D065,D001 \\
        --out-dir outputs/m3/stage1 --live --budget 0.75

Each document runs the normal pipeline (primary, completeness checks, at most one
repair). Without --live no provider is contacted: cached responses are re-validated
and documents without them are reported. With --live, a new provider request is
started only while the stage's estimated new spend (spend_ledger.json in the stage
directory) is below --budget. Existing per-document artifacts are reused when their
cache identity matches, and never overwritten silently (see --replace). There is
deliberately no "all documents" option. Writes <out-dir>/documents/<doc>_extraction.json
and <out-dir>/<out-dir name>_summary.json.

Exit codes: 0 every selected document processed or resumed, without errors or a stop;
1 otherwise; 2 usage or configuration error. Not legal advice; spend is an estimate.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator.extraction.cache import ResponseCache  # noqa: E402
from navigator.extraction.config import (CACHE_DIR, DEFAULT_AS_OF, DEFAULT_GEMINI_MODEL,  # noqa: E402
                                         GENERATION_SETTINGS, PROVIDER_GEMINI, THINKING_LEVELS)
from navigator.extraction.corpus import run_stage, summary_path, supplied_documents  # noqa: E402
from navigator.extraction.extractor import load_source  # noqa: E402
from navigator.extraction.provider import MissingCredentialsError  # noqa: E402


def _ids(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def print_list() -> None:
    docs = supplied_documents()
    for d in docs:
        src = load_source(d["doc_id"])
        print(f"{d['doc_id']}  {d['jurisdiction']:18} {d['source_type']:28} {src.meta.body_chars:>7} chars  "
              f"{len(src.view.artifacts):>2} page artifacts  {d['url']}")
    print(f"{len(docs)} documents with supplied text")


def print_summary(summary: dict) -> None:
    print("Corpus extraction (Not legal advice)")
    print(f"  selection  : {', '.join(summary['selection'])}")
    print(f"  model      : {summary['model']}   thinking {summary['settings'].get('thinking_level')}   "
          f"budget ${summary['budget_usd']:.2f}")
    for r in summary["documents"]:
        if r["run_mode"] in ("processed", "resumed"):
            c = r["citations"]
            print(f"  {r['doc_id']} {r['run_mode']:9} {r['document_status']:15} cand {r['candidates']:>3} "
                  f"acc {r['accepted']:>3} rej {r['rejected']:>2}  calls p{r['primary_provider_calls']} "
                  f"r{r['repair_provider_calls']}  targets {r['repair_targets']} unresolved "
                  f"{len(r['unresolved_targets'])}  quotes {c['raw_substring']}/{c['accepted']} "
                  f"(xpage {c['reconstructed_cross_page']})  scope {r['global_scope']['verified']}/"
                  f"{r['global_scope']['proposed']}  est ${r['estimated_new_cost_usd']['total']:.4f}")
            print(f"        posture {r['posture']['declared']} -> {r['posture']['established']}  held {r['held']}  "
                  f"relative dates resolved {r['relative_dates_resolved']}  basis {r['source_basis']}  "
                  f"scope challenges {r['scope_challenges']}")
            for reason in r["review_reasons"]:
                print(f"        review: {reason}")
            for violation in r.get("integrity_violations", []):
                print(f"        INTEGRITY: {violation}")
        else:
            print(f"  {r['doc_id']} {r['run_mode']:9} {r['detail']}")
    t = summary["totals"]
    spend = summary["estimated_new_spend_usd"]
    print(f"  totals     : complete {t['complete']}  review_required {t['review_required']}  "
          f"accepted {t['accepted_rules']}  rejected {t['rejected_candidates']}  calls p{t['primary_provider_calls']} "
          f"r{t['repair_provider_calls']}")
    print(f"  est. spend : this run ${spend['this_invocation']:.4f}   stage ledger ${spend['stage_ledger_total']:.4f}"
          f"   (estimate from API-reported tokens, not a billing balance)")
    if summary["stop_reason"]:
        print(f"  STOPPED    : {summary['stop_reason']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract rules from an explicit selection of corpus documents.")
    parser.add_argument("--doc-ids", type=_ids, help="comma-separated doc_ids, processed in this order")
    parser.add_argument("--list", action="store_true", help="list documents with supplied text and exit")
    parser.add_argument("--out-dir", type=Path, help="stage directory, e.g. outputs/m3/stage1")
    parser.add_argument("--live", action="store_true", help="allow provider requests on cache misses")
    parser.add_argument("--budget", type=float, help="maximum estimated NEW spend for the stage, USD (required "
                                                     "with --live)")
    parser.add_argument("--replace", type=_ids, default=[], help="doc_ids whose existing artifacts may be replaced")
    parser.add_argument("--model", default=DEFAULT_GEMINI_MODEL)
    parser.add_argument("--thinking-level", choices=THINKING_LEVELS, default=GENERATION_SETTINGS["thinking_level"])
    parser.add_argument("--as-of", type=date.fromisoformat, default=DEFAULT_AS_OF)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if args.list:
        print_list()
        return 0
    if not args.doc_ids or args.out_dir is None:
        print("error: --doc-ids and --out-dir are required (there is no 'all documents' mode)", file=sys.stderr)
        return 2
    if args.live and (args.budget is None or args.budget <= 0):
        print("error: --live requires a positive --budget (USD)", file=sys.stderr)
        return 2
    provider = None
    if args.live:
        from navigator.extraction.gemini import GeminiProvider  # vendor SDK loaded only for live runs
        try:
            provider = GeminiProvider(model=args.model)
        except MissingCredentialsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        summary = run_stage(args.doc_ids, out_dir=args.out_dir, cache=ResponseCache(args.cache_dir),
                            provider_name=PROVIDER_GEMINI, model=args.model, provider=provider,
                            settings={**GENERATION_SETTINGS, "thinking_level": args.thinking_level},
                            budget_usd=args.budget or 0.0, as_of=args.as_of, replace=frozenset(args.replace))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print_summary(summary)
    print(f"  summary    : {summary_path(args.out_dir)}")
    ok = all(r["run_mode"] in ("processed", "resumed") for r in summary["documents"]) and not summary["stop_reason"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
