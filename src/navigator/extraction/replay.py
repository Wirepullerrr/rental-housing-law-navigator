"""Code-only replay: re-evaluate a CACHED older primary response under the current
deterministic post-processing. No provider is contacted and no repair is requested.

A v6 response has every field except the v7 scope_mode / scope_quote of its document-level
conditions: those are left undeclared, and scope.py decides each condition's mode from its
verified evidence (with the lead-in of a lettered list item). A v4/v5 response (same
response contract) lacks more of what prompt v6 asks for. The adapter re-expresses what the
v5 response DID state and leaves the rest undetermined; it never invents content:

  inventory ids         P1, P2, ... by inventory position (the v5 order)
  rule provision links  from the v5 inventory's rule_indices (same links, other direction)
  scope targeting       v5 `governs`: null -> document-wide; a string EXACTLY equal (case
                        and whitespace aside) to one inventory ref -> that provision's id;
                        anything else -> untargeted (not propagated; review). No partial,
                        prefix or comma-based matching.
  posture               not declared (established posture: unknown)
  anchors, roles,       not declared (None): scope challenges cannot be evaluated
  source_basis,
  enactment_status_evidence
  no_rules_justification  not declared

Every fill is recorded in run.legacy_replay. The replayed artifact keeps the cached
response's identity (cache key, prompt version) and says it is a replay.

resolve_relative_only (for responses older than v4, e.g. prompt v2) applies ONLY the
relative-date resolver to each rule's verified effective-date evidence.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from navigator import starter_pack as sp
from navigator.extraction.extractor import (SourceDocument, _official_validator, _parse, _TOP_LEVEL_KEYS, _now,
                                            evaluate_primary, finalize)
from navigator.extraction.models import ExtractionRun, RepairPass
from navigator.extraction.normalize import derive_status
from navigator.extraction.quotes import verify_text
from navigator.extraction.temporal import find_base_dates, resolve_relative

REPLAYABLE = ("v4", "v5", "v6")


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def adapt_v5(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """v5 response -> v6-shaped payload (module docstring) and the audit of every fill."""
    provisions = payload.get("provisions") or []
    rules = [dict(r) for r in payload.get("rules") or []]
    links: dict[int, list[str]] = {}
    items = []
    for n, item in enumerate(provisions):
        pid = f"P{n + 1}"
        for i in item.get("rule_indices") or []:
            links.setdefault(i, []).append(pid)
        items.append({"id": pid, **{k: v for k, v in item.items() if k != "rule_indices"},
                      "anchor": None, "role": None})
    for i, r in enumerate(rules):
        r["provision_ids"] = links.get(i, [])
    scope, targeting = [], []
    for cond in payload.get("global_scope") or []:
        governs = cond.get("governs")
        if governs is None:
            ids, how = None, "document-wide (governs null)"
        else:
            hits = [it["id"] for it in items if _same(it["ref"], governs)]
            ids, how = (hits, "exact inventory ref") if len(hits) == 1 else ([], "no single inventory ref equals it")
        targeting.append({"id": cond.get("id"), "governs": governs, "governed_provision_ids": ids, "how": how})
        scope.append({**{k: v for k, v in cond.items() if k != "governs"}, "source_provision_id": None,
                      "governed_provision_ids": ids})
    adapted = {"document": None, "provisions": items, "global_scope": scope, "rules": rules,
               "no_rules_justification": None}
    fills = {"provision_ids": "P<n> by v5 inventory position",
             "rule_links": {str(i): links.get(i, []) for i in range(len(rules))},
             "scope_targeting": targeting,
             "not_declared": ["document posture", "inventory anchors", "inventory roles", "source_basis",
                              "enactment_status_evidence", "source_provision_id", "no_rules_justification"],
             "consequences": ["posture unknown: an enacted rule without a resolved effective date is held",
                              "no anchors: scope challenges cannot be evaluated",
                              "no repair request is made by a replay"]}
    return adapted, fills


def replay_artifact(source: SourceDocument, artifact: ExtractionRun, *, as_of: date,
                    schema_path: Path = sp.REPO_ROOT / sp.SCHEMA_PATH) -> ExtractionRun:
    """Re-evaluate the cached primary response of `artifact` (prompt v4-v6) offline."""
    if artifact.prompt_version not in REPLAYABLE:
        raise ValueError(f"{artifact.source.doc_id}: prompt {artifact.prompt_version} cannot be replayed "
                         f"(supported: {REPLAYABLE})")
    if artifact.source.content_sha256 != source.meta.content_sha256:
        raise ValueError(f"{artifact.source.doc_id}: the cached response was made for different source text")
    run = ExtractionRun(
        run_at=_now(), source=source.meta, provider=artifact.provider, model=artifact.model,
        prompt_version=artifact.prompt_version, prompt_sha256=artifact.prompt_sha256,
        response_schema_sha256=artifact.response_schema_sha256, generation_settings=artifact.generation_settings,
        as_of=as_of.isoformat(), cache_key=artifact.cache_key, cache_hit=True, cache_entry=artifact.cache_entry,
        provider_metadata=artifact.provider_metadata, raw_response_text=artifact.raw_response_text,
        source_view=artifact.source_view)
    payload = _parse(run.raw_response_text, _TOP_LEVEL_KEYS, run.errors, run.warnings)
    if payload is not None:
        if artifact.prompt_version == "v6":
            adapted, fills = payload, {"not_declared": ["scope_mode", "scope_quote"],
                                       "consequences": ["each condition's mode is decided from its verified "
                                                        "evidence", "no repair request is made by a replay"]}
        else:
            adapted, fills = adapt_v5(payload)
        run.legacy_replay = {"replayed_from_prompt_version": artifact.prompt_version,
                             "note": "cached response re-evaluated by the current post-processing; no provider "
                                     "request", **fills}
        _, targets = evaluate_primary(run, adapted, source, as_of, _official_validator(Path(schema_path)),
                                      legacy=True)
        if targets or run.scope_mappings:
            run.repair = RepairPass(reason="replay: a repair request is never made for a cached legacy response",
                                    targets=targets, mappings=run.scope_mappings)
    finalize(run, source)
    return run


def resolve_relative_only(source: SourceDocument, response_text: str, as_of: date) -> dict[str, Any]:
    """Resolver-level replay for responses older than v4: each rule's effective-date evidence
    is verified against the raw text and passed to the relative-date resolver alone."""
    payload = json.loads(response_text)
    base_dates = find_base_dates(source.body)
    rules = []
    for i, r in enumerate(payload.get("rules") or []):
        evidence = r.get("effective_date_evidence")
        check = verify_text(evidence, source.body, source.view) if isinstance(evidence, str) else None
        resolution = resolve_relative(check, source.body, base_dates)
        status, why = derive_status(r.get("enactment_status", "enacted"), resolution["resolved_date"], True, as_of)
        rules.append({"index": i, "citation": r.get("citation"), "enactment_status": r.get("enactment_status"),
                      "effective_date_evidence": evidence,
                      "evidence_status": check.status if check is not None else None,
                      "resolution": resolution, "status": status, "status_derivation": why})
    return {"doc_id": source.meta.doc_id, "as_of": as_of.isoformat(), "base_dates": base_dates, "rules": rules,
            "note": "resolver-level replay only: the cached response predates quote parts, inventory roles and "
                    "provision links, so the full pipeline is not replayed"}
