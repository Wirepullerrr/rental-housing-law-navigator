"""Corpus runner (M3): explicitly selected documents, processed one at a time,
resumable, with a hard budget gate on NEW provider requests.

Each selected document goes through the normal single-document pipeline
(extractor.extract_document: primary -> completeness checks -> at most one
repair). This module only orchestrates:

  selection   only the doc_ids given explicitly are processed, in the given order;
              discovery reads the manifest (no fixed corpus size is assumed).
  resume      an existing per-document artifact whose primary cache identity matches
              the current inputs is reused unchanged. One that does not match is never
              overwritten, unless replacement is requested for that doc_id explicitly;
              the old file is then kept under a ".superseded-<time>" name. Provider
              responses already obtained are reused through the content-addressed cache.
  budget      every provider request goes through MeteredProvider. It refuses to START a
              request once the stage's estimated new spend (the spend ledger, which
              survives interruptions) has reached the budget. It also refuses a second
              primary or repair request for one document.
  integrity   hard invariants are checked after every document; a violation stops the run.
  summary     per-document records and corpus totals, rewritten after every document.

A review_required document is a normal outcome and never stops the batch. A
document-level failure (e.g. no supplied text) is recorded and the batch continues.
A provider failure, the budget gate or an integrity violation stops the batch.
Spend figures are ESTIMATES from API-reported token usage, never a billing balance.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache, sha256_hex
from navigator.extraction.chunked import _sum_usage, expected_cache_key, extract_large_document, is_large
from navigator.extraction.config import PRICE_PER_MTOK
from navigator.extraction.extractor import (CacheMiss, SourceDocument, SourceNotAvailable, _official_validator,
                                            extract_document, load_source, write_artifact)
from navigator.extraction.models import ExtractionRun
from navigator.extraction.normalize import level_for
from navigator.extraction.prompt import REPAIR_SYSTEM_INSTRUCTION
from navigator.extraction.provider import ProviderError, ProviderResult, StructuredLLMProvider
from navigator.extraction.repair import target_identity
from navigator.extraction.scope import mapping_identity

DISCLAIMER = ("Spend figures are estimates computed from API-reported token usage at the configured rates; "
              "they are not the account's billing balance.")


class BudgetExhausted(ProviderError):
    """The stage budget was reached; no new provider request is started."""


class IntegrityViolation(RuntimeError):
    """A hard pipeline invariant was broken; the run must stop."""


def estimate_cost(usage: dict[str, Any] | None) -> float:
    """USD estimate for one request; thinking tokens are billed as output."""
    u = usage or {}
    output = (u.get("output_tokens") or 0) + (u.get("thinking_tokens") or 0)
    return ((u.get("input_tokens") or 0) * PRICE_PER_MTOK["input"] + output * PRICE_PER_MTOK["output"]) / 1e6


def supplied_documents(root: Path = sp.REPO_ROOT) -> list[dict[str, str]]:
    """Every manifest document with supplied local text (no fixed count assumed)."""
    _, rows = sp.read_csv(root / sp.MANIFEST_PATH)
    return [{"doc_id": r["doc_id"], "jurisdiction": r["jurisdictions"], "source_type": r["source_type"],
             "url": r["url"]} for r in rows if sp.classify_source(r) == sp.SUPPLIED_TEXT]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SpendLedger:
    """Every new provider request of a stage and its estimated cost, persisted after each request."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = (json.loads(path.read_text(encoding="utf-8"))["requests"]
                                              if path.is_file() else [])

    @property
    def total(self) -> float:
        return sum(e["estimated_cost_usd"] for e in self.entries)

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        _write_json(self.path, {"disclaimer": DISCLAIMER, "estimated_total_usd": round(self.total, 6),
                                "requests": self.entries})


_CHUNK_PROMPT = re.compile(r"this request covers EXCERPT (\d+) of (\d+) of the document")


class MeteredProvider:
    """Wraps a provider. Before every request: refuses a second primary request for the
    current document (in large-document mode: a second request for the same excerpt) and a
    second repair request, and refuses to start any request once the ledger total has
    reached the budget. After every request: records its API-reported usage and cost."""

    def __init__(self, inner: StructuredLLMProvider, ledger: SpendLedger, budget_usd: float) -> None:
        self.inner, self.ledger, self.budget_usd = inner, ledger, budget_usd
        self.name, self.model = inner.name, inner.model
        self.doc_id: str | None = None
        self.requests: dict[str, Counter] = defaultdict(Counter)

    def generate(self, *, system_instruction: str, prompt: str, response_json_schema: dict[str, Any],
                 settings: dict[str, Any]) -> ProviderResult:
        kind = "repair" if system_instruction == REPAIR_SYSTEM_INSTRUCTION else "primary"
        chunk = _CHUNK_PROMPT.search(prompt) if kind == "primary" else None
        slot = f"primary:excerpt-{chunk.group(1)}" if chunk else kind
        if self.requests[self.doc_id][slot]:
            raise IntegrityViolation(f"a second {slot} provider request was attempted for {self.doc_id}")
        if self.ledger.total >= self.budget_usd:
            raise BudgetExhausted(f"budget gate: estimated new spend ${self.ledger.total:.4f} has reached "
                                  f"${self.budget_usd:.2f}; no new {slot} request started for {self.doc_id}")
        self.requests[self.doc_id][slot] += 1
        if slot != kind:
            self.requests[self.doc_id][kind] += 1      # total primary requests of the document
        result = self.inner.generate(system_instruction=system_instruction, prompt=prompt,
                                     response_json_schema=response_json_schema, settings=settings)
        usage = (result.metadata or {}).get("usage") or {}
        self.ledger.record({"doc_id": self.doc_id, "pass": kind, "chunk": int(chunk.group(1)) if chunk else None,
                            "at": _now(), "usage": usage, "estimated_cost_usd": estimate_cost(usage)})
        return result


def integrity_violations(run: ExtractionRun, source: SourceDocument, cache: ResponseCache, provider_name: str,
                         model: str, settings: dict[str, Any], schema_path: Path) -> list[str]:
    """Hard invariants. Any violation stops the run (a review_required status does not)."""
    v: list[str] = []
    body = source.body
    if run.source.content_sha256 != sha256_hex(body):
        v.append("raw provenance: source hash differs from the supplied text")
    pieces = sorted([(s["raw_start"], s["raw_end"]) for s in run.source_view.get("segments", [])]
                    + [(a["raw_start"], a["raw_end"]) for a in run.source_view.get("artifacts", [])])
    if body and (not pieces or pieces[0][0] != 0 or pieces[-1][1] != len(body)
                 or any(a[1] != b[0] for a, b in zip(pieces, pieces[1:]))):
        v.append("raw provenance: view segments and page artifacts do not tile the raw text")
    if any(body[a["raw_start"]:a["raw_end"]] != a["text"] for a in run.source_view.get("artifacts", [])):
        v.append("raw provenance: a recorded page artifact differs from the raw text")
    validator = _official_validator(Path(schema_path))
    verified_scope = {g["id"] for g in run.global_scope
                      if g["propagated"] and g["evidence_check"]["status"] != "failed"}
    accepted = [c for c in run.candidates if c.accepted]
    if [c.rule for c in accepted] != run.rules:
        v.append("published rules differ from the accepted candidates")
    for c in accepted:
        if c.citation is None or c.citation.status == "failed" or c.rule["quoted_span"] not in body:
            v.append(f"candidate {c.index}: accepted quote is not verified raw source text")
        if any(True for _ in validator.iter_errors(c.rule)):
            v.append(f"candidate {c.index}: published rule is invalid under the official schema")
        if bad := [p["id"] for p in c.propagated_scope if p["applied"] and p["id"] not in verified_scope]:
            v.append(f"candidate {c.index}: unverified global scope propagated: {bad}")
        if bad := [p["id"] for p in c.propagated_scope if p["applied"] and p.get("governed_provision_ids") is not None
                   and not set(p["governed_provision_ids"]) & set(c.provision_ids)]:
            v.append(f"candidate {c.index}: scope condition applied without a shared provision id: {bad}")
        mapped = {(m["condition_id"], m["provision_id"]) for m in run.scope_mappings if m.get("final") == "applies"}
        if bad := [p["id"] for p in c.propagated_scope if p["applied"] and p.get("mode") == "named_subject"
                   and not any((p["id"], pid) in mapped for pid in c.provision_ids)]:
            v.append(f"candidate {c.index}: named-subject condition applied without a verified mapping: {bad}")
    if any(c.held and c.accepted for c in run.candidates):
        v.append("a held candidate was published")
    if any(c.historical and c.accepted for c in run.candidates):
        v.append("a historical (expired) candidate was published")
    as_of = run.as_of
    if bad := [c.index for c in accepted if (c.validity.get("end_exclusive") or "9999") <= as_of]:
        v.append(f"candidates {bad}: published although their verified validity ended on or before {as_of}")
    if bad := [c.index for c in accepted if c.duplicate_of is not None]:
        v.append(f"candidates {bad}: published although suppressed as same-source duplicates")
    spans = [(c.rule["category"], c.rule["quoted_span"]) for c in accepted]
    if len(spans) != len(set(spans)):
        v.append("the same source passage is published twice under one category")
    if run.cache_key != expected_cache_key(source, provider_name, model, settings):
        v.append("cache identity: the primary response does not match the current inputs")
    if run.chunking is not None and run.chunking.get("tiling") != "exact":
        v.append("large-document mode: the chunks do not tile the raw text exactly")
    rp = run.repair
    if rp is not None and rp.invoked and not rp.errors:
        entry = cache.get(rp.cache_key)
        if (entry is None or entry["key_fields"].get("primary_cache_key") != run.cache_key
                or entry["key_fields"].get("repair_targets") != target_identity(rp.targets)
                or entry["key_fields"].get("scope_mappings", []) != mapping_identity(rp.mappings)):
            v.append("cache identity: the repair response does not match this run's primary and target set")
    if sum(c.origin == "repair" for c in run.candidates) and (rp is None or not rp.invoked):
        v.append("repair candidates without a repair request")
    return v


def document_record(run: ExtractionRun, source: SourceDocument, mode: str, calls: Counter,
                    artifact: Path) -> dict[str, Any]:
    """One summary row. Tokens and cost count only requests made by THIS invocation."""
    rp = run.repair
    accepted = [c for c in run.candidates if c.accepted]
    targets = rp.targets if rp is not None else []
    usage = {"primary": run.provider_metadata.get("usage") or {},
             "repair": (rp.provider_metadata.get("usage") or {}) if rp is not None else {}}
    new_usage = {k: (usage[k] if calls.get(k) else {}) for k in usage}
    if run.chunking is not None and calls.get("primary"):    # only the excerpts requested by this run
        new_usage["primary"] = _sum_usage(run.chunking["chunks"], new_only=True)
    cost = {k: round(estimate_cost(new_usage[k]), 6) for k in usage}
    return {
        "doc_id": run.source.doc_id, "jurisdiction": run.source.jurisdiction,
        "level": level_for(run.source.jurisdiction), "source_type": run.source.source_type,
        "body_chars": run.source.body_chars, "page_artifacts": run.source_view.get("artifacts_removed", 0),
        "model": run.model, "reported_model": run.provider_metadata.get("model"),
        "thinking_level": run.generation_settings.get("thinking_level"), "prompt_version": run.prompt_version,
        "run_mode": mode, "primary_cache_hit": run.cache_hit,
        "repair_cache_hit": rp.cache_hit if rp is not None else None,
        "primary_provider_calls": calls.get("primary", 0), "repair_provider_calls": calls.get("repair", 0),
        "candidates": run.candidate_count, "accepted": run.accepted_count,
        "rejected": sum(not (c.accepted or c.held or c.historical or c.duplicate_of is not None)
                        for c in run.candidates),
        "historical": sum(c.historical for c in run.candidates),
        "suppressed_duplicates": sum(c.duplicate_of is not None for c in run.candidates),
        "primary_candidates": sum(c.origin == "primary" for c in run.candidates),
        "repair_candidates": sum(c.origin == "repair" for c in run.candidates),
        "repair_targets": len(targets),
        "repair_classifications": dict(Counter(str(t.get("repair_scope")) for t in targets)),
        "repair_resolution": run.coverage.get("repair_targets"),
        "unresolved_targets": [t["ref"] for t in targets if t.get("final_resolution") == "unresolved"],
        "document_status": run.document_status, "review_reasons": run.review_reasons,
        "posture": {"declared": run.posture.get("declared"), "established": run.posture.get("established")},
        "held": sum(c.held for c in run.candidates),
        "relative_dates_resolved": sum((c.temporal.get("relative_resolution") or {}).get("outcome") == "resolved"
                                       for c in run.candidates),
        "source_basis": dict(Counter(str(c.source_basis) for c in accepted)),
        "scope_challenges": [c["ref"] for c in run.scope_challenges if c["challenged"]],
        "scope_modes": {g["id"]: (g.get("scope") or {}).get("mode") for g in run.global_scope},
        "scope_mappings": [f"{m['condition_id']}->{m['provision_id']}: {m.get('final')}" for m in run.scope_mappings],
        "legislative": [{"index": c.index, "status": c.legislative.get("status"),
                         "basis": c.legislative.get("status_basis")} for c in run.candidates if c.legislative],
        "empty_result": run.empty_result,
        "citations": {"accepted": len(accepted),
                      "exact_match": sum(c.citation.status == "exact_match" for c in accepted),
                      "normalized_match": sum(c.citation.status == "normalized_match" for c in accepted),
                      "layout_normalized_match": sum(c.citation.status == "layout_normalized_match"
                                                     for c in accepted),
                      "raw_substring": sum(r["quoted_span"] in source.body for r in run.rules),
                      "reconstructed_cross_page": sum(c.citation.reconstructed for c in accepted)},
        "global_scope": {"proposed": len(run.global_scope),
                         "verified": sum(g["propagated"] for g in run.global_scope)},
        "errors": run.errors,
        "tokens_reported": usage, "new_tokens": new_usage,
        "estimated_new_cost_usd": {**cost, "total": round(sum(cost.values()), 6)},
        "artifact": _display(artifact),
    }


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(sp.REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _failure(doc_id: str, status: str, detail: str, source: SourceDocument | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"doc_id": doc_id, "run_mode": status, "detail": detail, "primary_provider_calls": 0,
                           "repair_provider_calls": 0, "estimated_new_cost_usd": {"primary": 0.0, "repair": 0.0,
                                                                                  "total": 0.0}}
    if source is not None:
        row.update(jurisdiction=source.meta.jurisdiction, source_type=source.meta.source_type)
    return row


def run_stage(doc_ids: list[str], *, out_dir: Path, cache: ResponseCache, provider_name: str, model: str,
              settings: dict[str, Any], provider: StructuredLLMProvider | None = None,
              budget_usd: float = 0.0, as_of: date | None = None, replace: frozenset[str] = frozenset(),
              schema_path: Path = sp.REPO_ROOT / sp.SCHEMA_PATH) -> dict[str, Any]:
    """Process `doc_ids` sequentially. Returns (and writes) the stage summary."""
    if len(set(doc_ids)) != len(doc_ids):
        raise ValueError("doc_ids must be unique")
    ledger = SpendLedger(out_dir / "spend_ledger.json")
    spent_before = ledger.total
    metered = MeteredProvider(provider, ledger, budget_usd) if provider is not None else None
    kwargs = {"as_of": as_of} if as_of is not None else {}
    records: list[dict[str, Any]] = []
    stop: str | None = None

    def write_summary() -> dict[str, Any]:
        summary = stage_summary(records, doc_ids=doc_ids, out_dir=out_dir, model=model, settings=settings,
                                budget_usd=budget_usd, ledger=ledger, spent_before=spent_before, stop=stop)
        _write_json(summary_path(out_dir), summary)
        return summary

    for doc_id in doc_ids:      # sequential: no parallel provider requests
        if stop is not None:
            records.append(_failure(doc_id, "not_run", f"not started: the run stopped ({stop})"))
        else:
            record, stop = _process(doc_id, out_dir / "documents", cache, provider_name, model, settings,
                                    metered, replace, schema_path, kwargs)
            records.append(record)
        write_summary()         # progress survives an interruption
    return write_summary()


def summary_path(out_dir: Path) -> Path:
    return out_dir / f"{out_dir.name}_summary.json"


def _process(doc_id: str, docs_dir: Path, cache: ResponseCache, provider_name: str, model: str,
             settings: dict[str, Any], metered: MeteredProvider | None, replace: frozenset[str], schema_path: Path,
             kwargs: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    path = docs_dir / f"{doc_id}_extraction.json"
    try:
        source = load_source(doc_id)
    except SourceNotAvailable as exc:
        return _failure(doc_id, "error", str(exc)), None
    if path.exists():
        existing = ExtractionRun.model_validate_json(path.read_text(encoding="utf-8"))
        if doc_id not in replace:
            if existing.cache_key == expected_cache_key(source, provider_name, model, settings):
                return document_record(existing, source, "resumed", Counter(), path), None
            return _failure(doc_id, "error", "an existing artifact has a different cache identity; it was not "
                                             "overwritten (request replacement explicitly)", source), None
        path.rename(path.with_name(f"{path.stem}.superseded-{datetime.now():%Y%m%dT%H%M%S}.json"))
    if metered is not None:
        metered.doc_id = doc_id
    extract = extract_large_document if is_large(source) else extract_document
    try:
        run = extract(source, provider_name=provider_name, model=model, cache=cache, provider=metered,
                      settings=settings, schema_path=schema_path, **kwargs)
    except BudgetExhausted as exc:
        return _failure(doc_id, "not_run", str(exc), source), "budget gate reached"
    except IntegrityViolation as exc:
        return _failure(doc_id, "error", f"integrity: {exc}", source), f"integrity violation on {doc_id}"
    except ProviderError as exc:
        return _failure(doc_id, "error", f"provider: {exc}", source), f"provider failure on {doc_id}"
    except CacheMiss as exc:
        return _failure(doc_id, "error", f"{exc} (offline run)", source), None
    except Exception as exc:  # isolate an unexpected per-document failure; the batch continues
        return _failure(doc_id, "error", f"{type(exc).__name__}: {exc}", source), None
    calls = metered.requests[doc_id] if metered is not None else Counter()
    violations = integrity_violations(run, source, cache, provider_name, model, settings, schema_path)
    write_artifact(run, path)
    record = document_record(run, source, "processed", calls, path)
    if violations:
        record["integrity_violations"] = violations
        return record, f"integrity violation on {doc_id}"
    if run.chunking is not None and run.chunking.get("stopped"):
        return record, run.chunking["stopped"]
    repair_failure = [e for e in (run.repair.errors if run.repair else []) if e.startswith("repair request failed")]
    if repair_failure:
        return record, ("budget gate reached" if "budget gate" in repair_failure[0]
                        else f"provider failure during repair of {doc_id}")
    return record, None


def stage_summary(records: list[dict[str, Any]], *, doc_ids: list[str], out_dir: Path, model: str,
                  settings: dict[str, Any], budget_usd: float, ledger: SpendLedger, spent_before: float,
                  stop: str | None) -> dict[str, Any]:
    done = [r for r in records if r["run_mode"] in ("processed", "resumed")]
    new_cost = [r["estimated_new_cost_usd"]["total"] for r in records if r["run_mode"] == "processed"]
    repaired = [r for r in done if r["repair_targets"]]
    classes: Counter = Counter()
    for r in done:
        classes.update(r["repair_classifications"])
    tokens = {kind: {k: sum((r.get("new_tokens", {}).get(kind) or {}).get(k) or 0 for r in records)
                     for k in ("input_tokens", "output_tokens", "thinking_tokens")} for kind in ("primary", "repair")}
    accepted = sum(r["citations"]["accepted"] for r in done)
    return {
        "disclaimer": "Not legal advice. " + DISCLAIMER,
        "generated_at": _now(), "stage_dir": _display(out_dir), "selection": doc_ids, "model": model,
        "settings": settings, "pricing_usd_per_million_tokens": PRICE_PER_MTOK, "budget_usd": budget_usd,
        "stop_reason": stop,
        "estimated_new_spend_usd": {"this_invocation": round(ledger.total - spent_before, 6),
                                    "stage_ledger_total": round(ledger.total, 6)},
        "totals": {
            "selected": len(doc_ids), "processed": sum(r["run_mode"] == "processed" for r in records),
            "resumed": sum(r["run_mode"] == "resumed" for r in records),
            "not_run": sum(r["run_mode"] == "not_run" for r in records),
            "errors": sum(r["run_mode"] == "error" for r in records),
            "complete": sum(r["document_status"] == "complete" for r in done),
            "review_required": sum(r["document_status"] == "review_required" for r in done),
            "candidates": sum(r["candidates"] for r in done), "accepted_rules": accepted,
            "rejected_candidates": sum(r["rejected"] for r in done),
            "historical_candidates": sum(r.get("historical", 0) for r in done),
            "suppressed_duplicates": sum(r.get("suppressed_duplicates", 0) for r in done),
            "held_candidates": sum(r.get("held", 0) for r in done),
            "accepted_quotes_raw_substring": sum(r["citations"]["raw_substring"] for r in done),
            "exact_citation_rate": (sum(r["citations"]["raw_substring"] for r in done) / accepted) if accepted else None,
            "reconstructed_cross_page_quotes": sum(r["citations"]["reconstructed_cross_page"] for r in done),
            "documents_requiring_repair": len(repaired),
            "repair_targets": sum(r["repair_targets"] for r in done),
            "repair_classifications": dict(classes),
            "repair_resolved_by_rule": sum((r["repair_resolution"] or {}).get("resolved_by_accepted_rule", 0)
                                           for r in done),
            "repair_resolved_out_of_scope": sum((r["repair_resolution"] or {}).get("resolved_out_of_scope", 0)
                                                for r in done),
            "unresolved_targets": sum(len(r["unresolved_targets"]) for r in done),
            "primary_provider_calls": sum(r["primary_provider_calls"] for r in records),
            "repair_provider_calls": sum(r["repair_provider_calls"] for r in records),
            "primary_cache_hits": sum(bool(r.get("primary_cache_hit")) for r in done),
            "repair_cache_hits": sum(bool(r.get("repair_cache_hit")) for r in done),
            "new_tokens": tokens,
            "estimated_new_cost_usd": round(sum(new_cost), 6),
            "new_cost_per_processed_document_usd": {
                "mean": round(sum(new_cost) / len(new_cost), 6) if new_cost else None,
                "median": round(median(new_cost), 6) if new_cost else None,
                "max": round(max(new_cost), 6) if new_cost else None},
        },
        "documents": records,
    }
