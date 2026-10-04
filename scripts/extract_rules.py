"""Extract rule records from ONE supplied corpus document (M2 vertical slice).

    uv run python scripts/extract_rules.py --doc-id D052                       # offline: cached response only
    uv run --env-file .env python scripts/extract_rules.py --doc-id D052 --live
    uv run --env-file .env python scripts/extract_rules.py --doc-id D052 --live --force   # bypass cache

Without --live this script never contacts a provider: it re-validates cached
responses or stops. With --live it makes at most two requests: the primary pass
and, only if coverage closure finds in-scope provisions with no candidate, one
targeted repair pass. There is deliberately no "all documents" mode.
Writes an audit artifact (default outputs/m2/<doc_id>_extraction.json).

Exit codes: 0 every candidate accepted and the document complete; 1 provider
error, run errors, rejected candidates or document review_required; 2 usage or
configuration error. Not legal advice.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator.extraction.cache import ResponseCache  # noqa: E402
from navigator.extraction.config import (CACHE_DIR, DEFAULT_AS_OF, DEFAULT_GEMINI_MODEL,  # noqa: E402
                                         GENERATION_SETTINGS, PROVIDER_GEMINI, THINKING_LEVELS, artifact_path)
from navigator.extraction.extractor import (CacheMiss, SourceNotAvailable, extract_document,  # noqa: E402
                                            load_source, write_artifact)
from navigator.extraction.models import ExtractionRun  # noqa: E402
from navigator.extraction.provider import MissingCredentialsError, ProviderError  # noqa: E402


def _usage(metadata: dict) -> str:
    u = metadata.get("usage") or {}
    return (f"in {u.get('input_tokens')}  out {u.get('output_tokens')}  thinking {u.get('thinking_tokens')}  "
            f"total {u.get('total_tokens')}") if u else "no usage reported"


def print_summary(run: ExtractionRun, out: Path, body: str) -> None:
    print("Rule extraction (Not legal advice)")
    print(f"  document   : {run.source.doc_id}  {run.source.jurisdiction}  {run.source.url}")
    print(f"  provider   : {run.provider} / {run.model} (reported: {run.provider_metadata.get('model', '-')})   "
          f"prompt {run.prompt_version}   as_of {run.as_of}   thinking {run.generation_settings.get('thinking_level')}")
    print(f"  primary    : cache {'HIT' if run.cache_hit else 'MISS (provider called)'} {run.cache_key[:16]}   "
          f"{_usage(run.provider_metadata)}")
    rp = run.repair
    if rp is None:
        print("  repair     : not needed (no in-scope provision without a candidate)")
    elif not rp.invoked:
        print(f"  repair     : NEEDED but not run: {'; '.join(rp.errors) or rp.reason}")
    else:
        print(f"  repair     : cache {'HIT' if rp.cache_hit else 'MISS (provider called)'} {rp.cache_key[:16]}   "
              f"{_usage(rp.provider_metadata)}")
        print(f"               requested {[r['ref'] for r in rp.requested]}")
        print(f"               candidates {len(rp.candidate_indices)}  accepted {rp.accepted_count}  "
              f"rejected {rp.rejected_count}  errors {rp.errors or '-'}")
    print(f"  source view: {run.source_view.get('artifacts_removed', 0)} page artifacts, "
          f"{len(run.source_view.get('segments', []))} segments   global scope: "
          f"{sum(g['propagated'] for g in run.global_scope)}/{len(run.global_scope)} verified")
    cov = run.coverage
    print(f"  inventory  : {cov.get('inventory_items', 0)} items, {cov.get('in_scope_items', 0)} in scope "
          f"({cov.get('in_scope_refs', 0)} refs)   uncertain {cov.get('uncertain', [])}")
    for stage in ("after_primary", "after_repair"):
        if stage in cov:
            print(f"  {stage:11}: uncovered {cov[stage]['uncovered']}  all-rejected {cov[stage]['unaccepted']}")
    accepted = [c for c in run.candidates if c.accepted]
    print(f"  quotes     : accepted single-part {sum(not c.citation.reconstructed for c in accepted)}, "
          f"reconstructed cross-page {sum(c.citation.reconstructed for c in accepted)}; published spans that are "
          f"raw substrings {sum(r['quoted_span'] in body for r in run.rules)}"
          f"/{len(run.rules)}")
    for origin in ("primary", "repair"):
        group = [c for c in run.candidates if c.origin == origin]
        if group:
            print(f"  {origin:11}: candidates {len(group)}  accepted {sum(c.accepted for c in group)}  "
                  f"rejected {sum(not c.accepted for c in group)}")
    for c in run.candidates:
        rule = c.rule or {}
        cite = c.citation.status + ("+xpage" if c.citation.reconstructed else "") if c.citation else "-"
        mark = "ACCEPT" if c.accepted else "REJECT"
        print(f"   [{c.index}]{'R' if c.origin == 'repair' else ' '} {mark} {rule.get('team_rule_id', '-'):13} "
              f"{rule.get('category', '-'):20} span={cite:22} status={rule.get('status')} "
              f"eff={rule.get('effective_date')}  {rule.get('citation', '')}  {rule.get('title', '')}")
        for msg in c.warnings:
            if not msg.startswith("quoted_span matched only after safe normalization"):
                print(f"         warning: {msg}")
    for label, items in (("warnings", run.warnings), ("errors", run.errors)):
        for msg in items:
            print(f"  {label[:-1]}: {msg}")
    print(f"  document   : {run.document_status.upper()}" + "".join(f"\n    - {r}" for r in run.review_reasons))
    print(f"  artifact   : {out}")



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract rules from one supplied corpus document.")
    parser.add_argument("--doc-id", required=True, help="exactly one manifest doc_id, e.g. D052")
    parser.add_argument("--live", action="store_true", help="allow a real provider call on cache miss")
    parser.add_argument("--force", action="store_true", help="bypass the cache (requires --live)")
    parser.add_argument("--model", default=DEFAULT_GEMINI_MODEL, help=f"Gemini model (default {DEFAULT_GEMINI_MODEL})")
    parser.add_argument("--thinking-level", choices=THINKING_LEVELS, default=GENERATION_SETTINGS["thinking_level"],
                        help="model thinking level (part of the cache identity)")
    parser.add_argument("--as-of", type=date.fromisoformat, default=DEFAULT_AS_OF, help="query date for status")
    parser.add_argument("--out", type=Path, help="audit artifact path")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # legacy Windows consoles cannot encode every character

    if args.force and not args.live:
        print("error: --force bypasses the cache, so it requires --live", file=sys.stderr)
        return 2
    try:
        source = load_source(args.doc_id)
    except SourceNotAvailable as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        settings = {**GENERATION_SETTINGS, "thinking_level": args.thinking_level}
        run = extract_document(source, provider_name=PROVIDER_GEMINI, model=args.model, provider=provider,
                               cache=ResponseCache(args.cache_dir), as_of=args.as_of, force=args.force,
                               settings=settings)
    except CacheMiss as exc:
        print(f"error: {exc}. Re-run with --live to call the provider.", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = args.out or artifact_path(args.doc_id)
    write_artifact(run, out)
    print_summary(run, out, source.body)
    return 0 if run.fully_accepted and run.document_status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
