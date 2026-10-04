"""Code-only replay of CACHED extraction responses under the current post-processing (M3.1).
No provider is contacted; nothing is spent.

    uv run python scripts/replay_cached.py --from-dir outputs/m3/stage1/documents \\
        --doc-ids D065,D011,D022,D085 --out-dir outputs/m3/m3_1/replay
    uv run python scripts/replay_cached.py --resolver-only outputs/m2_5/D069_extraction.json \\
        --out-dir outputs/m3/m3_1/replay

A prompt-v5 artifact is replayed through the full deterministic pipeline (replay.py
documents what a v5 response cannot supply). An older artifact can only be replayed
through the relative-date resolver (--resolver-only). Writes <doc>_replay.json per
document and replay_summary.json. Not legal advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator.extraction.config import DEFAULT_AS_OF  # noqa: E402
from navigator.extraction.extractor import load_source, write_artifact  # noqa: E402
from navigator.extraction.models import ExtractionRun  # noqa: E402
from navigator.extraction.replay import replay_artifact, resolve_relative_only  # noqa: E402


def summarize(run: ExtractionRun, before: ExtractionRun) -> dict:
    by_rule = []
    for c in run.candidates:
        res = c.temporal.get("relative_resolution") or {}
        by_rule.append({"index": c.index, "citation": (c.raw or {}).get("citation"), "provision_ids": c.provision_ids,
                        "accepted": c.accepted, "held": c.held, "status": (c.rule or {}).get("status"),
                        "effective_date": (c.rule or {}).get("effective_date"),
                        "relative_resolution": res.get("outcome"), "applied_scope": [p["id"] for p in c.propagated_scope
                                                                                     if p["applied"]],
                        "rejection_reasons": [] if c.accepted else c.rejection_reasons})
    return {"doc_id": run.source.doc_id, "replayed_prompt_version": run.prompt_version, "cache_key": run.cache_key,
            "before": {"document_status": before.document_status, "accepted": before.accepted_count,
                       "candidates": before.candidate_count},
            "after": {"document_status": run.document_status, "accepted": run.accepted_count,
                      "held": sum(c.held for c in run.candidates),
                      "rejected": sum(not c.accepted and not c.held for c in run.candidates),
                      "candidates": run.candidate_count},
            "posture": run.posture, "base_dates": run.base_dates,
            "scope_targeting": (run.legacy_replay or {}).get("scope_targeting"),
            "global_scope": [{"id": g["id"], "propagated": g["propagated"], "problem": g.get("problem")}
                             for g in run.global_scope],
            "quotes_unchanged": [c.citation.source_span for c in run.candidates if c.citation] ==
                                [c.citation.source_span for c in before.candidates if c.citation],
            "rules": by_rule, "review_reasons": run.review_reasons}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay cached extraction responses offline.")
    parser.add_argument("--from-dir", type=Path, help="directory with <doc>_extraction.json artifacts (prompt v5)")
    parser.add_argument("--doc-ids", type=lambda s: [x.strip() for x in s.split(",") if x.strip()], default=[])
    parser.add_argument("--resolver-only", type=Path, action="append", default=[],
                        help="artifact older than v5: replay only the relative-date resolver")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=DEFAULT_AS_OF)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.out_dir / "replay_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    summary.update({"note": "Code-only replay of cached responses; no provider request, zero spend. Not legal "
                            "advice.", "as_of": args.as_of.isoformat()})
    docs = summary.setdefault("documents", {})
    for doc_id in args.doc_ids:
        before = ExtractionRun.model_validate_json((args.from_dir / f"{doc_id}_extraction.json").read_text(
            encoding="utf-8"))
        run = replay_artifact(load_source(doc_id), before, as_of=args.as_of)
        write_artifact(run, args.out_dir / f"{doc_id}_replay.json")
        docs[doc_id] = summarize(run, before)
        a = docs[doc_id]["after"]
        print(f"{doc_id}: {before.document_status} {before.accepted_count}/{before.candidate_count} -> "
              f"{run.document_status} accepted {a['accepted']} held {a['held']} rejected {a['rejected']}")
        for reason in run.review_reasons:
            print(f"    review: {reason}")
    for path in args.resolver_only:
        before = ExtractionRun.model_validate_json(path.read_text(encoding="utf-8"))
        result = resolve_relative_only(load_source(before.source.doc_id), before.raw_response_text, args.as_of)
        result["replayed_prompt_version"] = before.prompt_version
        (args.out_dir / f"{before.source.doc_id}_resolver_replay.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        docs[before.source.doc_id] = {"resolver_only": True, "rules": [
            {k: r[k] for k in ("citation", "status")} | {"resolved_date": r["resolution"]["resolved_date"],
                                                         "outcome": r["resolution"]["outcome"]}
            for r in result["rules"]]}
        print(f"{before.source.doc_id} (resolver only, prompt {before.prompt_version}): "
              + "; ".join(f"{r['citation']} -> {r['resolution']['resolved_date']} {r['status']}"
                          for r in result["rules"]))
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
