"""Large-document mode (M3.3): one document too large for a single rule-generation request.

A document whose raw text exceeds LARGE_DOCUMENT_CHARS is not sent whole: the projected
completion (output + thinking) of a one-shot request would exceed the model's completion
limit (observed up to 2.3 tokens per source character on large documents). Instead:

  1. STRUCTURAL CHUNKING (plan_chunks, CHUNK_RESOLVER). Deterministic, from the raw text:
     chunks tile the raw text exactly (every character in exactly one chunk, nothing
     discarded or overlapped) and each is at most CHUNK_MAX_CHARS. A cut is made at the
     best boundary between CHUNK_MIN_CHARS and CHUNK_MAX_CHARS after the chunk start, in
     this order of preference: a heading line, a paragraph start (after a blank line), a
     line after a sentence end, any line start; the latest such boundary wins. A cut is
     never made inside a line (so never inside a table row) or inside a removed page
     artifact. Only if a window has no line start at all is a chunk cut at
     CHUNK_MAX_CHARS (recorded as a "hard" cut). Each chunk keeps its raw offsets, a
     stable id (<doc>:cNN) and the nearest heading before it.
  2. EXTRACTION. One request per chunk with the unchanged v7 instruction and schema; the
     excerpt is framed as such (prompt.render_chunk_prompt) and keeps the document's own
     segment numbers, so every quote is verified against the full raw text as usual.
  3. DOCUMENT-LEVEL MERGE (merge_payloads). Inventories, rules and scope conditions are
     combined with chunk-namespaced ids (c03.P5, c03.S1). A chunk's "whole document"
     (governed_provision_ids null) is narrowed to that chunk's own provisions: a condition
     reaches another chunk's provisions only where the document-level structural or
     explicit-reference logic (scope.py) verifies it, or a named-subject mapping does.
     Then the normal single-document pipeline runs ONCE on the merged response: posture,
     candidate evaluation, scope propagation, completeness checks (closure, subdivision
     guard, scope challenges), named-subject mappings, deduplication.
  4. REPAIR. At most one document-level repair request, built from the excerpts (chunks)
     that contain its targets and mapping pairs, not the whole document.
  5. FAILURE MODE. A chunk that cannot be extracted (provider failure, budget gate,
     unparseable response) leaves the document review_required; every individually
     validated record of the other chunks is kept.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache, cache_key, sha256_hex
from navigator.extraction.config import DEFAULT_AS_OF, GENERATION_SETTINGS
from navigator.extraction.extractor import (_TOP_LEVEL_KEYS, PreparedRequest, SourceDocument, _display_path, _fetch,
                                            _now, _official_validator, _parse, _prepared, _repair_pass,
                                            evaluate_primary, finalize, prepare_request)
from navigator.extraction.models import ExtractionRun, RepairPass, RepairResponse, generation_json_schema
from navigator.extraction.prompt import (CHUNK_MODE_VERSION, EXTRACTION_PROMPT_VERSION, REPAIR_PROMPT_VERSION,
                                        render_chunk_prompt, render_repair_prompt)
from navigator.extraction.provider import ProviderError, StructuredLLMProvider
from navigator.extraction.repair import canonical_order, target_identity
from navigator.extraction.scope import mapping_identity
from navigator.extraction.source_view import PAGE_BREAK_MARKER, SEGMENT_LABEL, SourceView

CHUNK_RESOLVER = "structural-chunks/v1"
# One-shot above this size is projected to exceed the 65,536-token completion limit: about
# 100k chars x the observed large-document mean of ~1.25 output+thinking tokens per char.
LARGE_DOCUMENT_CHARS = 100_000
# About 4.1k + 0.216 x 20k = 8.4k input tokens per chunk (observed calibration); projected
# completion ~25k tokens at the observed mean ratio, ~46k at the observed maximum ratio.
CHUNK_MAX_CHARS = 20_000
CHUNK_MIN_CHARS = 10_000
LEVELS = ("hard", "line", "sentence", "paragraph", "heading")   # ascending preference

_LEGAL_HEADING = re.compile(r"^\s*(?:§|Section\b|SECTION\b|Sec\.|SEC\.|Article\b|ARTICLE\b|Chapter\b|CHAPTER\b|"
                            r"Part\b|PART\b|Division\b|DIVISION\b|Subchapter\b|SUBCHAPTER\b)")


def is_large(source: SourceDocument) -> bool:
    return len(source.body) > LARGE_DOCUMENT_CHARS


@dataclass(frozen=True)
class Chunk:
    id: str             # stable: <doc_id>:cNN
    tag: str            # cNN, the id namespace in the merged response
    number: int         # 1-based
    raw_start: int
    raw_end: int
    boundary: str       # how the chunk START was chosen: document_start | heading | paragraph | ...
    heading: str | None  # nearest heading line before raw_start (context for the excerpt)

    def audit(self) -> dict[str, Any]:
        return {"id": self.id, "tag": self.tag, "number": self.number, "raw_start": self.raw_start,
                "raw_end": self.raw_end, "chars": self.raw_end - self.raw_start, "boundary": self.boundary,
                "heading_context": self.heading}


_CONTINUES = re.compile(r"(?:(?:or|and|the|of|to|by|in|for)|[,;:(\-])$", re.IGNORECASE)


def _heading(lines: list[str], i: int) -> bool:
    """A heading line: it follows a blank line or a finished sentence, is short, and does not
    itself read as running text (sentence or citation end, trailing connective)."""
    s = lines[i].strip()
    prev = lines[i - 1].strip() if i > 0 else ""
    if prev and prev[-1] not in ".:;!?":
        return False
    if not s or s.endswith((").", ");", "),")) or _CONTINUES.search(s):
        return False
    if _LEGAL_HEADING.match(lines[i]):
        return len(s) <= 120
    return 3 <= len(s) <= 80 and s[-1] not in ".,;:" and s[0].isalpha() and s[0].isupper()


def boundaries(raw: str, view: SourceView | None = None) -> list[tuple[int, str, str | None]]:
    """(raw offset, level, heading text) for every line start except 0, outside page artifacts."""
    lines = raw.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    blocked = [(a.raw_start, a.raw_end) for a in (view.artifacts if view else ())]
    found = []
    for i in range(1, len(lines)):
        pos = starts[i]
        if any(s < pos < e for s, e in blocked):
            continue
        prev = lines[i - 1].strip()
        if _heading(lines, i):
            level = "heading"
        elif not prev:
            level = "paragraph"
        elif prev[-1] in ".;:!?":
            level = "sentence"
        else:
            level = "line"
        found.append((pos, level, lines[i].strip() if level == "heading" else None))
    return found


def plan_chunks(doc_id: str, raw: str, view: SourceView | None = None, max_chars: int = CHUNK_MAX_CHARS,
                min_chars: int = CHUNK_MIN_CHARS) -> list[Chunk]:
    """Deterministic structural chunks that tile raw exactly (module docstring)."""
    bounds = boundaries(raw, view)
    headings = [(pos, text) for pos, level, text in bounds if level == "heading"]
    first_line = raw.splitlines()[0].strip() if raw.strip() else None
    cuts: list[tuple[int, str]] = [(0, "document_start")]
    pos = 0
    while len(raw) - pos > max_chars:
        window = [(p, lv) for p, lv, _ in bounds if pos + min_chars <= p <= pos + max_chars]
        if window:
            best = max(window, key=lambda b: (LEVELS.index(b[1]), b[0]))
            cut = best
        else:
            cut = (pos + max_chars, "hard")
        cuts.append(cut)
        pos = cut[0]
    chunks = []
    for n, (start, level) in enumerate(cuts, start=1):
        end = cuts[n][0] if n < len(cuts) else len(raw)
        before = [text for p, text in headings if p < start]   # the chunk shows its own first heading
        heading = before[-1] if before else (first_line if start > 0 else None)
        chunks.append(Chunk(id=f"{doc_id}:c{n:02d}", tag=f"c{n:02d}", number=n, raw_start=start, raw_end=end,
                            boundary=level, heading=heading))
    return chunks


def chunk_view(view: SourceView, raw: str, start: int, end: int) -> str:
    """The model-facing text of raw[start:end]: the document's own segment labels (repeated at
    the chunk start for a segment that began earlier) and page-break markers."""
    parts: list[str] = []
    pieces = sorted([(s.raw_start, s.raw_start + s.length, "segment", s.id) for s in view.segments]
                    + [(a.raw_start, a.raw_end, "artifact", None) for a in view.artifacts])
    for s, e, kind, seg_id in pieces:
        lo, hi = max(s, start), min(e, end)
        if hi <= lo:
            continue
        if kind == "segment":
            parts.append(SEGMENT_LABEL.format(seg_id) + "\n" + raw[lo:hi])
        else:
            parts.append(PAGE_BREAK_MARKER + "\n")
    return "".join(parts)


def prepare_chunk_request(source: SourceDocument, chunk: Chunk, total: int, provider_name: str, model: str,
                          settings: dict[str, Any]) -> PreparedRequest:
    body = chunk_view(source.view, source.body, chunk.raw_start, chunk.raw_end)
    system, user = render_chunk_prompt(source.meta, body, chunk.number, total, chunk.raw_start, chunk.raw_end,
                                       len(source.body), chunk.heading)
    return _prepared(source, provider_name, model, settings, system, user, generation_json_schema(),
                     **{"pass": "primary", "chunk_mode": CHUNK_MODE_VERSION, "chunk_resolver": CHUNK_RESOLVER,
                        "chunk_id": chunk.id, "chunk_raw_range": [chunk.raw_start, chunk.raw_end]})


def composite_key(keys: list[str]) -> str:
    return cache_key({"chunked_primary_keys": keys, "chunk_mode": CHUNK_MODE_VERSION})


def expected_cache_key(source: SourceDocument, provider_name: str, model: str, settings: dict[str, Any]) -> str:
    """The primary cache identity of a document under the current inputs, in either mode."""
    if not is_large(source):
        return prepare_request(source, provider_name, model, settings).key
    chunks = plan_chunks(source.meta.doc_id, source.body, source.view)
    return composite_key([prepare_chunk_request(source, c, len(chunks), provider_name, model, settings).key
                          for c in chunks])


# ------------------------------------------------------------------------------- merge

def _ns(tag: str, value: Any) -> Any:
    return f"{tag}.{value}" if isinstance(value, str) and value else value


def merge_payloads(parts: list[tuple[Chunk, dict[str, Any]]]) -> dict[str, Any]:
    """One response-shaped payload with chunk-namespaced ids (module docstring, step 3)."""
    merged: dict[str, Any] = {"document": None, "provisions": [], "global_scope": [], "rules": [],
                              "no_rules_justification": None}
    for chunk, payload in parts:
        tag = chunk.tag
        if merged["document"] is None and payload.get("document") is not None:
            merged["document"] = payload["document"]
        own = []
        for item in payload.get("provisions") or []:
            if isinstance(item, dict) and "id" in item:
                item = {**item, "id": _ns(tag, item["id"])}
                own.append(item["id"])
            merged["provisions"].append(item)
        for item in payload.get("global_scope") or []:
            if isinstance(item, dict):
                governed = item.get("governed_provision_ids")
                item = {**item, "id": _ns(tag, item.get("id")),
                        "source_provision_id": _ns(tag, item.get("source_provision_id")),
                        # a chunk's "whole document" is only this chunk (see module docstring)
                        "governed_provision_ids": sorted(own) if governed is None else
                        [_ns(tag, g) for g in governed] if isinstance(governed, list) else governed}
            merged["global_scope"].append(item)
        for item in payload.get("rules") or []:
            if isinstance(item, dict):
                item = dict(item)
                if isinstance(item.get("provision_ids"), list):
                    item["provision_ids"] = [_ns(tag, p) for p in item["provision_ids"]]
                if isinstance(item.get("scope_carve_outs"), list):
                    item["scope_carve_outs"] = [{**co, "scope_id": _ns(tag, co.get("scope_id"))}
                                                if isinstance(co, dict) else co for co in item["scope_carve_outs"]]
            merged["rules"].append(item)
        if merged["no_rules_justification"] is None and payload.get("no_rules_justification") is not None:
            merged["no_rules_justification"] = payload["no_rules_justification"]
    if merged["rules"]:
        merged["no_rules_justification"] = None
    return merged


# ------------------------------------------------------------------------------- repair excerpts

def repair_chunks(chunks: list[Chunk], targets: list[dict[str, Any]], mappings: list[dict[str, Any]]) -> list[Chunk]:
    """The chunks that contain a repair target or a mapping pair (by id namespace or raw offset)."""
    tags, offsets = set(), []
    for t in targets:
        if t.get("provision_id"):
            tags.add(str(t["provision_id"]).split(".")[0])
        if t.get("raw_start") is not None:
            offsets.append(t["raw_start"])
    for m in mappings:
        tags.update(str(m[k]).split(".")[0] for k in ("condition_id", "provision_id"))
    return [c for c in chunks if c.tag in tags or any(c.raw_start <= o < c.raw_end for o in offsets)]


def prepare_chunked_repair_request(source: SourceDocument, chunks: list[Chunk], provider_name: str, model: str,
                                   settings: dict[str, Any], primary_key: str, scope: list[dict[str, Any]],
                                   targets: list[dict[str, Any]], mappings: list[dict[str, Any]]) -> PreparedRequest:
    selected = repair_chunks(chunks, targets, mappings)
    body = "\n".join(chunk_view(source.view, source.body, c.raw_start, c.raw_end) for c in selected)
    names = ", ".join(f"excerpt {c.number} (raw {c.raw_start}-{c.raw_end})" for c in selected)
    ordered = sorted(mappings, key=lambda m: (m["condition_id"], m["provision_id"]))
    system, user = render_repair_prompt(source.meta, body, scope, canonical_order(targets), ordered, excerpts=names)
    return _prepared(source, provider_name, model, settings, system, user, generation_json_schema(RepairResponse),
                     **{"pass": "repair", "primary_cache_key": primary_key, "repair_prompt_version": REPAIR_PROMPT_VERSION,
                        "repair_targets": target_identity(targets), "scope_mappings": mapping_identity(list(mappings)),
                        "chunk_mode": CHUNK_MODE_VERSION, "repair_chunks": [c.id for c in selected]})


# ------------------------------------------------------------------------------- driver

def extract_large_document(source: SourceDocument, *, provider_name: str, model: str, cache: ResponseCache,
                           provider: StructuredLLMProvider | None = None, as_of: date = DEFAULT_AS_OF,
                           force: bool = False, settings: dict[str, Any] = GENERATION_SETTINGS,
                           schema_path: Path = sp.REPO_ROOT / sp.SCHEMA_PATH, repair: bool = True,
                           max_chars: int = CHUNK_MAX_CHARS, min_chars: int = CHUNK_MIN_CHARS) -> ExtractionRun:
    """Large-document mode (module docstring). Never raises for one failed chunk: the failure is
    recorded, the other chunks are kept, and the document is review_required."""
    if provider is not None and (provider.name, provider.model) != (provider_name, model):
        raise ValueError(f"provider is {provider.name}/{provider.model}, expected {provider_name}/{model}")
    chunks = plan_chunks(source.meta.doc_id, source.body, source.view, max_chars, min_chars)
    requests = [prepare_chunk_request(source, c, len(chunks), provider_name, model, settings) for c in chunks]
    key = composite_key([r.key for r in requests])
    audit: dict[str, Any] = {"resolver": CHUNK_RESOLVER, "mode": CHUNK_MODE_VERSION, "max_chars": max_chars,
                             "min_chars": min_chars, "raw_chars": len(source.body), "chunks": [], "stopped": None,
                             "tiling": "exact" if chunks[0].raw_start == 0 and chunks[-1].raw_end == len(source.body)
                             and all(a.raw_end == b.raw_start for a, b in zip(chunks, chunks[1:])) else "BROKEN"}
    parts: list[tuple[Chunk, dict[str, Any]]] = []
    failed: list[str] = []
    for chunk, req in zip(chunks, requests):
        row = {**chunk.audit(), "cache_key": req.key, "cache_entry": _display_path(cache.path(req.key)),
               "prompt_sha256": req.key_fields["prompt_sha256"]}
        audit["chunks"].append(row)
        if audit["stopped"]:
            row["status"] = f"not requested: {audit['stopped']}"
            failed.append(chunk.id)
            continue
        try:
            entry, row["cache_hit"] = _fetch(req, cache, provider, force, settings)
        except ProviderError as exc:
            row["status"] = f"provider failure: {exc}"
            failed.append(chunk.id)
            if "budget gate" in str(exc):
                audit["stopped"] = "budget gate reached"
            continue
        if entry is None:
            row["status"] = "no cached response and no live provider"
            failed.append(chunk.id)
            continue
        row["provider_metadata"] = entry.get("provider_metadata") or {}
        row["raw_response_text"] = entry["response_text"]
        errors: list[str] = []
        payload = _parse(entry["response_text"], _TOP_LEVEL_KEYS, errors, [])
        if payload is None:
            row["status"] = f"unparseable response: {errors}"
            failed.append(chunk.id)
            continue
        row["status"] = "extracted"
        row["counts"] = {k: len(payload.get(k) or []) for k in ("provisions", "global_scope", "rules")}
        parts.append((chunk, payload))

    merged = merge_payloads(parts)
    run = ExtractionRun(
        run_at=_now(), source=source.meta, provider=provider_name, model=model,
        prompt_version=EXTRACTION_PROMPT_VERSION, prompt_sha256=sha256_hex("".join(r.key_fields["prompt_sha256"]
                                                                                    for r in requests)),
        response_schema_sha256=requests[0].key_fields["response_schema_sha256"], generation_settings=settings,
        as_of=as_of.isoformat(), cache_key=key, cache_hit=all(r.get("cache_hit") for r in audit["chunks"]),
        cache_entry="(one cache entry per chunk; see chunking.chunks)",
        provider_metadata={"usage": _sum_usage(audit["chunks"]), "chunks": len(chunks)},
        raw_response_text=json.dumps(merged, ensure_ascii=False),
        source_view={"marker": PAGE_BREAK_MARKER, "segment_label": SEGMENT_LABEL,
                     "view_sha256": sha256_hex(source.view.text),
                     "segments": [{"id": s.id, "raw_start": s.raw_start, "raw_end": s.raw_end}
                                  for s in source.view.segments],
                     "artifacts_removed": len(source.view.artifacts),
                     "artifacts": [{"raw_start": a.raw_start, "raw_end": a.raw_end, "text": a.text}
                                   for a in source.view.artifacts]},
        chunking=audit)
    validator = _official_validator(Path(schema_path))
    if parts:
        ctx, targets = evaluate_primary(run, merged, source, as_of, validator)
        if targets or run.scope_mappings:
            if repair and not audit["stopped"]:
                req = prepare_chunked_repair_request(source, chunks, provider_name, model, settings, key, ctx.scope,
                                                     targets, run.scope_mappings)
                run.chunking["repair_chunks"] = req.key_fields["repair_chunks"]
                run.repair = _repair_pass(run, source, key, ctx, targets, provider_name=provider_name, model=model,
                                          cache=cache, provider=provider, force=force, settings=settings,
                                          as_of=as_of, validator=validator, request=req)
            else:
                run.repair = RepairPass(reason=("repair not requested: " + audit["stopped"]) if audit["stopped"]
                                        else "repair disabled for this run", targets=targets,
                                        mappings=run.scope_mappings)
    else:
        run.errors.append("large-document mode: no excerpt could be extracted")
    finalize(run, source)
    if failed:
        run.review_reasons.append(f"large-document mode: excerpt(s) not extracted ({', '.join(failed)}); coverage "
                                  "of those raw ranges is unknown")
        run.document_status = "review_required"
    return run


def _sum_usage(rows: list[dict[str, Any]], new_only: bool = False) -> dict[str, int]:
    """API-reported usage summed over the chunk responses (only those not served from cache)."""
    keys = ("input_tokens", "output_tokens", "thinking_tokens")
    return {k: sum(((r.get("provider_metadata") or {}).get("usage") or {}).get(k) or 0 for r in rows
                   if not (new_only and r.get("cache_hit"))) for k in keys}
