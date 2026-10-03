"""Extract rule records from ONE supplied corpus document (M2 vertical slice).

    uv run python scripts/extract_rules.py --doc-id D052                       # offline: cached response only
    uv run --env-file .env python scripts/extract_rules.py --doc-id D052 --live
    uv run --env-file .env python scripts/extract_rules.py --doc-id D052 --live --force   # bypass cache

Without --live this script never contacts a provider: it re-validates a cached
response or stops. There is deliberately no "all documents" mode.
Writes an audit artifact (default outputs/m2/<doc_id>_extraction.json).

Exit codes: 0 every candidate accepted; 1 provider error, run errors or rejected
candidates; 2 usage or configuration error. Not legal advice.
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
                                         PROVIDER_GEMINI, artifact_path)
from navigator.extraction.extractor import (CacheMiss, SourceNotAvailable, extract_document,  # noqa: E402
                                            load_source, write_artifact)
from navigator.extraction.models import ExtractionRun  # noqa: E402
from navigator.extraction.provider import MissingCredentialsError, ProviderError  # noqa: E402


def print_summary(run: ExtractionRun, out: Path) -> None:
    print("Rule extraction (Not legal advice)")
    print(f"  document   : {run.source.doc_id}  {run.source.jurisdiction}  {run.source.url}")
    print(f"  provider   : {run.provider} / {run.model}   prompt {run.prompt_version}   as_of {run.as_of}")
    print(f"  cache      : {'HIT' if run.cache_hit else 'MISS (provider called)'}  {run.cache_key[:16]}")
    print(f"  candidates : {run.candidate_count}   accepted: {run.accepted_count}")
    for c in run.candidates:
        rule = c.rule or {}
        cite = c.citation.status if c.citation else "-"
        mark = "ACCEPT" if c.accepted else "REJECT"
        print(f"   [{c.index}] {mark} {rule.get('team_rule_id', '-'):13} {rule.get('category', '-'):26} "
              f"span={cite:16} status={rule.get('status')}  {rule.get('title', '')}")
    for label, items in (("warnings", run.warnings), ("errors", run.errors)):
        for msg in items:
            print(f"  {label[:-1]}: {msg}")
    print(f"  artifact   : {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract rules from one supplied corpus document.")
    parser.add_argument("--doc-id", required=True, help="exactly one manifest doc_id, e.g. D052")
    parser.add_argument("--live", action="store_true", help="allow a real provider call on cache miss")
    parser.add_argument("--force", action="store_true", help="bypass the cache (requires --live)")
    parser.add_argument("--model", default=DEFAULT_GEMINI_MODEL, help=f"Gemini model (default {DEFAULT_GEMINI_MODEL})")
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
        run = extract_document(source, provider_name=PROVIDER_GEMINI, model=args.model, provider=provider,
                               cache=ResponseCache(args.cache_dir), as_of=args.as_of, force=args.force)
    except CacheMiss as exc:
        print(f"error: {exc}. Re-run with --live to call the provider.", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = args.out or artifact_path(args.doc_id)
    write_artifact(run, out)
    print_summary(run, out)
    return 0 if run.fully_accepted else 1


if __name__ == "__main__":
    sys.exit(main())
