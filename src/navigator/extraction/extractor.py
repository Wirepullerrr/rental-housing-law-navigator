"""Extraction orchestration for ONE supplied corpus document.

    supplied text -> canonical view (page artifacts marked, segments numbered) -> prompt
      -> [cache | provider] -> raw JSON
      -> document posture (posture.py) and enactment dates in the raw text (temporal.py)
      -> provision inventory (ids, anchors, roles) and document-level scope conditions
         (evidence verified; scope mode decided from their verified scope words, scope.py)
      -> per candidate: Pydantic ExtractedRule -> provision links -> quote parts verified
         against the RAW text (a cross-page quote is reconstructed as one exact raw span)
         -> status evidence -> legislative status of proposals (legislative.py) ->
         effective-date evidence classification and deterministic relative-date
         resolution -> status derivation (posture-aware) -> validity window (validity.py:
         a future end keeps the rule current; an end on or before as_of makes it
         historical, never published) -> structural / explicit-reference
         scope propagation + operative conditions -> trusted metadata +
         team_rule_id -> RuleRecord -> official JSON Schema
      -> completeness: inventory coverage closure + subdivision guard + scope
         challenges of out_of_scope items (coverage.py)
      -> repair targets = their merged union (repair.py), plus scope_mapping_challenges
         for every named-subject (condition, provision) pair (scope.py)
      -> at most ONE repair request for those targets; each target is classified
         in_scope / out_of_scope / uncertain, and its candidates go through exactly
         the same evaluation
      -> named-subject conditions propagate only where the repair said "applies"
      -> exact same-source duplicates suppressed (same category and raw passage)
      -> completeness rerun; every target and mapping resolved, or review_required
      -> ExtractionRun audit record, document_status complete | review_required

A candidate is accepted only if every stage passes. A candidate whose only problem is an
unresolved time of application is HELD: preserved with its evidence, never published. A
candidate that passes every stage but whose verified validity ended on or before as_of is
HISTORICAL: preserved with its evidence and temporal state, never published as current.
Rejected candidates are never retried or repaired. A document with no candidate at all is
complete only with a verified justification that it has no in-scope legal content.
Vendor-neutral: depends only on the StructuredLLMProvider protocol.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache, cache_key, canonical_json, sha256_hex
from navigator.extraction.config import DEFAULT_AS_OF, GENERATION_SETTINGS
from navigator.extraction.coverage import (closure, provision_regions, same_ref, scope_challenges,
                                           unrecorded_subdivisions)
from navigator.extraction.legislative import decide as legislative_status
from navigator.extraction.legislative import find_session
from navigator.extraction.models import (CandidateResult, CitationCheck, ExtractedRule, ExtractionRun,
                                         LegacyExtractedRule,
                                         LegacyProvisionNote, LegacyScopeCondition, NoRulesJustification,
                                         ProvisionNote, QuotePart,
                                         RepairPass, RepairResponse, RuleRecord, ScopeCondition, SourceMeta,
                                         generation_json_schema)
from navigator.extraction.normalize import (TEMPORAL_UNRESOLVED, build_record, compose_scope, derive_status,
                                           split_trusted)
from navigator.extraction.posture import EXPECTED_STATUS, establish_posture, no_date_in_force
from navigator.extraction.prompt import (EXTRACTION_PROMPT_VERSION, REPAIR_PROMPT_VERSION, render_prompt,
                                        render_repair_prompt)
from navigator.extraction.provider import ProviderError, StructuredLLMProvider
from navigator.extraction.repair import (attach_resolutions, build_targets, canonical_order,
                                        repair_scope_rejection, resolve_targets, target_identity)
from navigator.extraction.quotes import verify_quote_parts, verify_text
from navigator.extraction.scope import (attach_mapping_decisions, finalize_mappings, mapping_challenges,
                                        mapping_identity, named_targets, resolve_condition, scope_words)
from navigator.extraction.review import unsupported_figures
from navigator.extraction.source_view import PAGE_BREAK_MARKER, SEGMENT_LABEL, SourceView, build_view
from navigator.extraction.temporal import enclosing_note_span, find_base_dates, resolve_effective_date
from navigator.extraction.validity import decide as decide_window
from navigator.extraction.validity import resolve_window
from navigator.validation import make_rule_validator

_QUOTE_PARTS = TypeAdapter(list[QuotePart])
_TOP_LEVEL_KEYS = {"document", "provisions", "global_scope", "rules", "no_rules_justification"}
_REPAIR_KEYS = {"target_resolutions", "scope_mappings", "rules"}


class SourceNotAvailable(ValueError):
    """The doc id is unknown, or has no supplied local text."""


class CacheMiss(RuntimeError):
    """No cached response for these inputs and no live provider was given."""


@dataclass(frozen=True)
class SourceDocument:
    meta: SourceMeta
    body: str  # raw source text: citations are always verified against this

    @cached_property
    def view(self) -> SourceView:
        """What the model reads: page artifacts replaced by markers, segments numbered."""
        return build_view(self.body)


@dataclass
class DocContext:
    """Document-level facts every candidate is evaluated against."""

    scope: list[dict[str, Any]] = field(default_factory=list)       # verified global scope conditions
    posture: dict[str, Any] = field(default_factory=dict)           # posture.establish_posture audit
    base_dates: list[dict[str, Any]] = field(default_factory=list)  # temporal.find_base_dates
    inventory_ids: set[str] | None = None   # ids of a valid inventory; None: links cannot be checked
    session: dict[str, Any] | None = None   # the bill's legislative session (legislative.py)
    legacy: bool = False                    # replay of an older response (replay.py)


@dataclass(frozen=True)
class PreparedRequest:
    system_instruction: str
    prompt: str
    response_json_schema: dict[str, Any]
    key_fields: dict[str, Any]
    key: str


def load_source(doc_id: str, root: Path = sp.REPO_ROOT) -> SourceDocument:
    _, rows = sp.read_csv(root / sp.MANIFEST_PATH)
    row = next((r for r in rows if r["doc_id"] == doc_id), None)
    if row is None:
        raise SourceNotAvailable(f"{doc_id!r} is not a doc_id in corpus_manifest.csv")
    kind = sp.classify_source(row)
    if kind != sp.SUPPLIED_TEXT:
        raise SourceNotAvailable(f"{doc_id} has no supplied local text ({kind}); extraction uses supplied text only")
    doc = sp.parse_corpus_text((root / sp.CORPUS_DIR / row["text_file"]).read_text(encoding="utf-8"))
    if doc.source_url != row["url"]:
        raise SourceNotAvailable(f"{doc_id}: text header URL does not match the manifest")
    meta = SourceMeta(doc_id=doc_id, jurisdiction=row["jurisdictions"], url=row["url"],
                      source_type=row["source_type"], retrieved_at=row["retrieved_at"],
                      text_file=row["text_file"], content_sha256=sha256_hex(doc.body), body_chars=len(doc.body))
    return SourceDocument(meta=meta, body=doc.body)


def _prepared(source: SourceDocument, provider_name: str, model: str, settings: dict[str, Any], system: str,
              user: str, schema: dict[str, Any], **extra: Any) -> PreparedRequest:
    key_fields = {
        **extra,
        "source_doc_id": source.meta.doc_id,
        "source_sha256": source.meta.content_sha256,
        "view_sha256": sha256_hex(source.view.text),
        "provider": provider_name,
        "model": model,
        "prompt_version": EXTRACTION_PROMPT_VERSION,
        "prompt_sha256": sha256_hex(system + "\x00" + user),
        "response_schema_sha256": sha256_hex(canonical_json(schema)),
        "generation_settings": settings,
    }
    return PreparedRequest(system, user, schema, key_fields, cache_key(key_fields))


def prepare_request(source: SourceDocument, provider_name: str, model: str,
                    settings: dict[str, Any] = GENERATION_SETTINGS) -> PreparedRequest:
    system, user = render_prompt(source.meta, source.view.text)
    return _prepared(source, provider_name, model, settings, system, user, generation_json_schema(),
                     **{"pass": "primary"})


def prepare_repair_request(source: SourceDocument, provider_name: str, model: str, settings: dict[str, Any],
                           primary_key: str, scope: list[dict[str, Any]], targets: list[dict[str, Any]],
                           mappings: list[dict[str, Any]] = ()) -> PreparedRequest:
    """The key names the complete, sorted target set and scope-mapping set explicitly (as well
    as through the prompt hash): a different set never reuses a cached repair response."""
    ordered = sorted(mappings, key=lambda m: (m["condition_id"], m["provision_id"]))
    system, user = render_repair_prompt(source.meta, source.view.text, scope, canonical_order(targets), ordered)
    return _prepared(source, provider_name, model, settings, system, user, generation_json_schema(RepairResponse),
                     **{"pass": "repair", "primary_cache_key": primary_key,
                        "repair_prompt_version": REPAIR_PROMPT_VERSION, "repair_targets": target_identity(targets),
                        "scope_mappings": mapping_identity(list(mappings))})


@lru_cache(maxsize=None)
def _official_validator(schema_path: Path):
    return make_rule_validator(sp.read_json(schema_path))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(sp.REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _fetch(req: PreparedRequest, cache: ResponseCache, provider: StructuredLLMProvider | None, force: bool,
           settings: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """(cache entry, cache_hit). Calls the provider only on a miss (or with force); None if it cannot."""
    entry = None if force else cache.get(req.key)
    if entry is not None:
        return entry, True
    if provider is None:
        return None, False
    result = provider.generate(system_instruction=req.system_instruction, prompt=req.prompt,
                               response_json_schema=req.response_json_schema, settings=settings)
    entry = {"key": req.key, "key_fields": req.key_fields, "created_at": _now(),
             "response_text": result.text, "provider_metadata": result.metadata}
    cache.put(req.key, entry)
    return entry, False


def extract_document(source: SourceDocument, *, provider_name: str, model: str,
                     cache: ResponseCache, provider: StructuredLLMProvider | None = None,
                     as_of: date = DEFAULT_AS_OF, force: bool = False,
                     settings: dict[str, Any] = GENERATION_SETTINGS,
                     schema_path: Path = sp.REPO_ROOT / sp.SCHEMA_PATH, repair: bool = True) -> ExtractionRun:
    """Extract rules from one document. Calls `provider` only on a cache miss (or with force=True):
    once for the primary pass and, if the completeness checks find repair targets, ONCE for repair."""
    if provider is not None and (provider.name, provider.model) != (provider_name, model):
        raise ValueError(f"provider is {provider.name}/{provider.model}, expected {provider_name}/{model}")
    req = prepare_request(source, provider_name, model, settings)
    entry, cache_hit = _fetch(req, cache, provider, force, settings)
    if entry is None:
        raise CacheMiss(f"no cached response for {source.meta.doc_id} with these inputs (key {req.key[:12]})")

    view = source.view
    run = ExtractionRun(
        run_at=_now(), source=source.meta, provider=provider_name, model=model,
        prompt_version=EXTRACTION_PROMPT_VERSION, prompt_sha256=req.key_fields["prompt_sha256"],
        response_schema_sha256=req.key_fields["response_schema_sha256"], generation_settings=settings,
        as_of=as_of.isoformat(), cache_key=req.key, cache_hit=cache_hit,
        cache_entry=_display_path(cache.path(req.key)), provider_metadata=entry.get("provider_metadata") or {},
        raw_response_text=entry["response_text"],
        source_view={"marker": PAGE_BREAK_MARKER, "segment_label": SEGMENT_LABEL,
                     "view_sha256": req.key_fields["view_sha256"],
                     "segments": [{"id": s.id, "raw_start": s.raw_start, "raw_end": s.raw_end} for s in view.segments],
                     "artifacts_removed": len(view.artifacts),
                     "artifacts": [{"raw_start": a.raw_start, "raw_end": a.raw_end, "text": a.text}
                                   for a in view.artifacts]},
    )
    validator = _official_validator(Path(schema_path))
    payload = _parse(run.raw_response_text, _TOP_LEVEL_KEYS, run.errors, run.warnings)
    if payload is not None:
        ctx, targets = evaluate_primary(run, payload, source, as_of, validator)
        if targets or run.scope_mappings:   # every deterministic check runs BEFORE repair
            if repair:
                run.repair = _repair_pass(run, source, req.key, ctx, targets, provider_name=provider_name,
                                          model=model, cache=cache, provider=provider, force=force,
                                          settings=settings, as_of=as_of, validator=validator)
            else:
                run.repair = RepairPass(reason="repair disabled for this run", targets=targets,
                                        mappings=run.scope_mappings)
    finalize(run, source)
    return run


def evaluate_primary(run: ExtractionRun, payload: dict[str, Any], source: SourceDocument, as_of: date, validator,
                     legacy: bool = False) -> tuple[DocContext, list[dict[str, Any]]]:
    """Everything deterministic about one primary response, up to the repair targets."""
    run.base_dates = find_base_dates(source.body)
    run.posture = establish_posture(payload.get("document"), source.body, source.view, run.base_dates)
    if payload.get("document") is None and not legacy:
        run.warnings.append("review: response has no document posture")
    _record_inventory(run, payload.get("provisions"), legacy)
    ids = {p["id"] for p in run.provision_inventory} if run.coverage["inventory_valid"] else None
    run.legislative_session = find_session(source.body, source.meta.jurisdiction)
    ctx = DocContext(posture=run.posture, base_dates=run.base_dates, inventory_ids=ids,
                     session=run.legislative_session, legacy=legacy)
    ctx.scope = _verify_global_scope(run, payload.get("global_scope"), source, ids, legacy)
    run.candidates = [evaluate_candidate(i, raw, source, as_of, validator, ctx) for i, raw in enumerate(payload["rules"])]
    _dedupe(run.candidates)
    run.scope_mappings = _scope_mapping_challenges(run, ctx)
    _record_empty_result(run, payload.get("no_rules_justification"), source)
    primary, gaps = _completeness(run, source)
    regions = provision_regions(run.provision_inventory, source.body, source.view)
    sources = {g["source_provision_id"] for g in run.global_scope if g["propagated"] and g.get("source_provision_id")}
    run.scope_challenges = scope_challenges(run.provision_inventory, regions, source.body, sources)
    run.coverage["anchors_located"] = f"{len(regions)}/{len(run.provision_inventory)}"
    if run.provision_inventory and len(regions) < len(run.provision_inventory):
        run.warnings.append(f"review: {len(run.provision_inventory) - len(regions)} inventory anchor(s) are not "
                            "source text; those provisions cannot be scope-challenged")
    run.coverage["after_primary"] = _summary(primary, gaps, run.scope_challenges)
    return ctx, build_targets(primary, gaps, run.scope_challenges)


def _publishable(c: CandidateResult) -> bool:
    """Validated (accepted, historical or a suppressed exact duplicate), or failing only on an
    unresolved time of application (a hold)."""
    reason = c.temporal.get("unresolved_reason")
    return c.validated or (bool(reason) and c.rejection_reasons == [reason])


def _scope_mapping_challenges(run: ExtractionRun, ctx: DocContext) -> list[dict[str, Any]]:
    """Every (named-subject condition, provision) pair to be verified by the repair pass."""
    linked = {pid for c in run.candidates if _publishable(c) for pid in c.provision_ids}
    quotes: dict[str, str] = {}
    for c in run.candidates:
        if _publishable(c):
            for pid in c.provision_ids:
                quotes.setdefault(pid, c.rule["quoted_span"])
    named = []
    for cond in ctx.scope:
        if cond["scope"]["mode"] == "named_subject":
            cond["scope"]["mapping_targets"] = named_targets(cond, linked)
            named.append(cond)
    for g in run.global_scope:   # the audit copy shows the same targets
        match = next((c for c in named if c["id"] == g["id"]), None)
        if match is not None:
            g["scope"]["mapping_targets"] = match["scope"]["mapping_targets"]
    return mapping_challenges(named, run.provision_inventory, quotes)


def _completeness(run: ExtractionRun, source: SourceDocument) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Both deterministic completeness checks over the current candidates."""
    return (closure(run.provision_inventory, run.candidates),
            unrecorded_subdivisions(run.provision_inventory, run.candidates, source.body))


def _summary(result: dict[str, Any], gaps: list[dict[str, Any]],
             challenges: list[dict[str, Any]] = ()) -> dict[str, Any]:
    return {"uncovered": result["uncovered"], "unaccepted": result["unaccepted"],
            "unrecorded_subdivisions": [s["ref"] for g in gaps for s in g["subdivisions"]],
            "scope_challenges": [c["ref"] for c in challenges if c["challenged"]]}


def _parse(text: str, allowed: set[str], errors: list[str], warnings: list[str]) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(f"response is not valid JSON: {exc}")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        errors.append("response must be a JSON object with a 'rules' list")
        return None
    if set(payload) - allowed:
        warnings.append(f"ignored unexpected top-level keys: {sorted(set(payload) - allowed)}")
    return payload


def _repair_pass(run: ExtractionRun, source: SourceDocument, primary_key: str, ctx: DocContext,
                 targets: list[dict[str, Any]], *, provider_name: str, model: str, cache: ResponseCache,
                 provider: StructuredLLMProvider | None, force: bool, settings: dict[str, Any], as_of: date,
                 validator, request: PreparedRequest | None = None) -> RepairPass:
    """The single repair request of this run. Its candidates are evaluated exactly like primary
    ones; the only extra check can only reject (repair.repair_scope_rejection). `request`:
    a prepared repair request built elsewhere (large-document mode sends excerpts only)."""
    req = request or prepare_repair_request(source, provider_name, model, settings, primary_key, ctx.scope,
                                            targets, run.scope_mappings)
    kinds = sorted({s for t in targets for s in t["sources"]})
    reason = f"{len(targets)} repair target(s) from {', '.join(kinds) or 'no check'}"
    if run.scope_mappings:
        reason += f"; {len(run.scope_mappings)} scope_mapping_challenge(s)"
    rp = RepairPass(reason=reason, prompt_version=REPAIR_PROMPT_VERSION, targets=targets,
                    mappings=run.scope_mappings, cache_key=req.key, prompt_sha256=req.key_fields["prompt_sha256"],
                    cache_entry=_display_path(cache.path(req.key)))
    targets = rp.targets            # the audit's own copies: resolutions are attached to these
    run.scope_mappings = rp.mappings
    try:
        entry, rp.cache_hit = _fetch(req, cache, provider, force, settings)
    except ProviderError as exc:
        rp.invoked = True
        rp.errors.append(f"repair request failed: {exc}")
        return rp
    if entry is None:
        rp.errors.append("repair needed, but there is no cached repair response and no live provider")
        return rp
    rp.invoked = True
    rp.provider_metadata = entry.get("provider_metadata") or {}
    rp.raw_response_text = entry["response_text"]
    payload = _parse(rp.raw_response_text, _REPAIR_KEYS, rp.errors, run.warnings)
    if payload is None:
        return rp
    if "target_resolutions" not in payload:
        run.warnings.append("review: repair response has no target_resolutions")
    run.warnings += attach_resolutions(payload.get("target_resolutions"), targets, source.body, source.view)
    if run.scope_mappings and "scope_mappings" not in payload:
        run.warnings.append("review: repair response has no scope_mappings")
    run.warnings += attach_mapping_decisions(payload.get("scope_mappings"), run.scope_mappings, source.body,
                                             source.view)
    for raw in payload["rules"]:
        c = evaluate_candidate(len(run.candidates), raw, source, as_of, validator, ctx)
        c.origin = "repair"
        if reason := repair_scope_rejection(c, targets):
            c.accepted = c.historical = False
            c.temporal_state = None
            c.rejection_reasons.append(reason)
        run.candidates.append(c)
        rp.candidate_indices.append(c.index)
    _dedupe(run.candidates)
    rp.accepted_count = sum(run.candidates[i].accepted for i in rp.candidate_indices)
    rp.rejected_count = len(rp.candidate_indices) - rp.accepted_count
    return rp


def _dedupe(candidates: list[CandidateResult]) -> None:
    """Reject later accepted candidates whose team_rule_id repeats an earlier accepted one (idempotent)."""
    seen: dict[str, int] = {}
    for c in candidates:
        if not c.accepted:
            continue
        rid = c.rule["team_rule_id"]
        if rid in seen:
            c.accepted = False
            c.rejection_reasons.append(f"duplicate: same team_rule_id {rid} as candidate {seen[rid]}")
        else:
            seen[rid] = c.index


def _apply_scope_mappings(run: ExtractionRun) -> None:
    """Propagate named-subject conditions to records linked to provisions the repair said
    they apply to, and recompose those records' scope text. Nothing else propagates them."""
    rp = run.repair
    problem = None if rp is None else ("; ".join(rp.errors) or (None if rp.invoked else rp.reason))
    run.coverage["scope_mappings"] = finalize_mappings(run.scope_mappings, rp is not None and rp.invoked,
                                                       problem if rp is not None else "no repair request")
    conditions = {g["id"]: g for g in run.global_scope if g["propagated"]}
    applies = [m for m in run.scope_mappings if m["final"] == "applies"]
    for c in run.candidates:
        if c.rule is None:
            continue
        added = False
        for m in applies:
            if m["provision_id"] not in c.provision_ids or m["condition_id"] in c.scope_inputs.get("carved", {}):
                continue
            if any(p["id"] == m["condition_id"] for p in c.propagated_scope):
                continue
            c.propagated_scope.append({"id": m["condition_id"], "kind": m["kind"], "mode": "named_subject",
                                       "governed_provision_ids": [m["provision_id"]],
                                       "basis": f"named-subject mapping to {m['provision_id']} verified by repair",
                                       "applied": True})
            added = True
        if added:
            applied = [p["id"] for p in c.propagated_scope if p["applied"]]
            conds = [{**conditions[i], "governed_provision_ids": conditions[i]["scope"]["propagate_ids"]
                      if conditions[i]["scope"]["mode"] != "named_subject" else [c.provision_ids[0]]}
                     for i in conditions if i in applied]
            inputs = c.scope_inputs
            c.rule["exemptions"] = compose_scope(inputs.get("exemptions"), conds, "exemption")
            c.rule["coverage_conditions"] = compose_scope(inputs.get("coverage_conditions"), conds,
                                                          "coverage_condition", inputs.get("operative", []))


def _metadata(c: CandidateResult) -> tuple[int, ...]:
    """Verified structured metadata of a record, compared component-wise by _dedupe_same_source."""
    return (int(c.rule.get("effective_date") is not None),
            sum(oc["evidence_check"]["status"] != "failed" for oc in c.operative_conditions),
            sum(p["applied"] for p in c.propagated_scope),
            int(c.version_evidence is not None and c.version_evidence.status != "failed"))


def _dedupe_same_source(run: ExtractionRun) -> None:
    """Suppress EXACT same-source duplicates: validated candidates of this document with the
    same category and the identical published quoted_span (the same raw passage, so also
    identical raw offsets). One record is published; the earlier (primary) record is kept
    unless a repair record's verified structured metadata is strictly better. The suppressed
    record stays in the audit (duplicate_of) with both provenances in run.dedupe. Records
    that quote DIFFERENT passages are never merged here, however similar."""
    groups: dict[tuple[str, str, bool], list[CandidateResult]] = {}
    for c in run.candidates:
        if (c.accepted or c.historical) and c.rule is not None:
            groups.setdefault((c.rule["category"], c.rule["quoted_span"], c.historical), []).append(c)
    for group in groups.values():
        keep = group[0]
        for c in group[1:]:
            mine, theirs = _metadata(c), _metadata(keep)
            if (c.origin == "repair" and keep.origin == "primary" and all(a >= b for a, b in zip(mine, theirs))
                    and mine != theirs):
                keep = c
        for c in group:
            if c is keep:
                continue
            c.accepted = c.historical = False
            c.duplicate_of = keep.index
            c.rejection_reasons.append(f"duplicate: same source passage and category as candidate {keep.index} "
                                       f"(team_rule_id {keep.rule['team_rule_id']}); suppressed, one record kept")
            run.dedupe.append({
                "kept_index": keep.index, "kept_team_rule_id": keep.rule["team_rule_id"],
                "suppressed_index": c.index, "suppressed_team_rule_id": c.rule["team_rule_id"],
                "reason": "identical raw quoted_span and category in the same document",
                "raw_start": keep.citation.start, "raw_end": keep.citation.end,
                "provenance": {"kept": {"origin": keep.origin, "citation": keep.rule["citation"],
                                        "provision_ids": keep.provision_ids},
                               "suppressed": {"origin": c.origin, "citation": c.rule["citation"],
                                              "provision_ids": c.provision_ids}},
                "kept_because": ("repair record has strictly better verified metadata"
                                 if keep.origin == "repair" else "earlier (primary) record")})


def finalize(run: ExtractionRun, source: SourceDocument) -> None:
    for c in run.candidates:   # historical means validated: any rejection voids it
        if c.historical and c.rejection_reasons:
            c.historical, c.temporal_state = False, None
    _dedupe(run.candidates)
    _apply_scope_mappings(run)
    _dedupe_same_source(run)
    for c in run.candidates:   # held: the unresolved time of application is the ONLY problem
        reason = c.temporal.get("unresolved_reason")
        c.held = bool(reason) and not c.accepted and c.rejection_reasons == [reason]
        c.temporal_state = (c.temporal_state if c.historical else "held" if c.held else
                            c.rule["status"] if c.accepted else None)
    run.candidate_count = len(run.candidates)
    run.rules = [c.rule for c in run.candidates if c.accepted]
    run.accepted_count = len(run.rules)
    for c in run.candidates:
        if c.historical:
            run.warnings.append(f"candidate {c.index} historical ({c.temporal_state}): {c.status_derivation}")
        elif not c.accepted:
            run.warnings.append(f"candidate {c.index} {'held' if c.held else 'rejected'}: "
                                f"{'; '.join(c.rejection_reasons)}")

    final, gaps = _completeness(run, source)   # both checks rerun after the (single) repair pass
    rp = run.repair
    if rp is not None and rp.invoked:
        run.coverage["after_repair"] = _summary(final, gaps)
    targets = rp.targets if rp is not None else []
    if rp is not None:
        problem = "; ".join(rp.errors) or (None if rp.invoked else rp.reason)
        run.coverage["repair_targets"] = resolve_targets(targets, run.candidates, rp.invoked, problem)
    resolved_out = [t["ref"] for t in targets if t["final_resolution"] == "resolved_out_of_scope"]

    def open_gap(ref: str) -> bool:     # not a target (reported there) and not resolved out of scope
        return not any(same_ref(ref, t["ref"]) for t in targets) and not any(same_ref(ref, r) for r in resolved_out)

    in_scope = [p for p in run.provision_inventory if p["scope"] == "in_scope"]
    run.coverage.update({
        "inventory_items": len(run.provision_inventory), "in_scope_items": len(in_scope),
        "in_scope_refs": len(final["provisions"]),
        "uncertain": [p["ref"] for p in run.provision_inventory if p["scope"] == "uncertain"],
        "final": _summary(final, gaps),
        "link_mismatches": final["link_mismatches"], "provisions": final["provisions"],
        "unrecorded_subdivisions": gaps})
    if final["link_mismatches"]:
        run.warnings.append(f"review: provision links whose rule citation does not name the provision: "
                            f"{len(final['link_mismatches'])}")

    reasons = []
    if run.errors:
        reasons.append("the response could not be processed")
    elif not run.coverage.get("inventory_valid", False):
        reasons.append("no complete, well-formed provision inventory; coverage cannot be established")
    elif not run.provision_inventory and run.candidates:
        reasons.append("the inventory lists no provisions although rules were produced")
    if not run.errors and not run.candidates and not (run.empty_result or {}).get("verified"):
        reasons.append("no rule candidates, and no source-grounded justification that the document has no "
                       "in-scope legal content")
    if run.coverage["uncertain"]:
        reasons.append(f"inventory provisions of uncertain scope: {run.coverage['uncertain']}")
    if held := [c for c in run.candidates if c.held]:
        why = sorted({c.status_derivation for c in held})
        reasons.append(f"{len(held)} candidate record(s) held, not published, because the time of application is "
                       f"unresolved: {why}")
    if mismatch := [c.index for c in run.candidates if c.temporal.get("posture_consistent") is False]:
        reasons.append(f"enactment_status inconsistent with the document posture "
                       f"({run.posture.get('established')}): candidates {mismatch}")
    if unmapped := [f"{m['condition_id']}->{m['provision_id']} ({m['unresolved_reason']})"
                    for m in run.scope_mappings if m.get("final") == "unresolved"]:
        reasons.append(f"named-subject scope mappings unresolved (not applied): {unmapped}")
    mapped = {(m["condition_id"], m["provision_id"]) for m in run.scope_mappings}
    linked = {pid for c in run.candidates if c.accepted or c.held for pid in c.provision_ids}
    if unverified := sorted(f"{g['id']}->{pid}" for g in run.global_scope
                            if g["propagated"] and g["scope"]["mode"] == "named_subject"
                            for pid in named_targets(g, linked) if (g["id"], pid) not in mapped):
        reasons.append(f"named-subject scope mappings never verified (not applied): {unverified}")
    if legis := [f"candidate {c.index}: {c.legislative['conflict']}" for c in run.candidates
                 if c.legislative.get("conflict") and c.legislative.get("status") in (None, "pending")]:
        reasons.append(f"legislative status needs review: {legis}")
    if unpropagated := [g["id"] for g in run.global_scope if not g["propagated"]]:
        reasons.append(f"document-level scope conditions not applied (evidence or governed provisions not "
                       f"established): {unpropagated}")
    if unresolved := [t for t in targets if t["final_resolution"] == "unresolved"]:
        reasons.append("repair targets still unresolved: "
                       + "; ".join(f"{t['ref']} ({t['unresolved_reason']})" for t in unresolved))
    if refs := [r for r in final["uncovered"] if open_gap(r)]:
        reasons.append(f"in-scope provisions with no candidate record: {refs}")
    held_refs = {p["ref"] for p in final["provisions"]
                 if p["candidates"] and all(run.candidates[i].held for i in p["candidates"])}
    if refs := [r for r in final["unaccepted"] if open_gap(r) and r not in held_refs]:
        reasons.append(f"in-scope provisions whose candidate records were all rejected: {refs}")
    if refs := [s["ref"] for g in gaps for s in g["subdivisions"] if open_gap(s["ref"])]:
        reasons.append(f"source subdivisions with no accepted record (not repair targets): {refs}")
    if rp is not None:
        reasons += [f"repair pass: {e}" for e in rp.errors]
    run.review_reasons = reasons
    run.document_status = "review_required" if reasons else "complete"


def _record_inventory(run: ExtractionRun, inventory: Any, legacy: bool = False) -> None:
    """Validate the inventory item by item. Malformed items (and repeated ids) are dropped
    and make coverage unprovable (document review_required); they never reject a rule."""
    run.coverage["inventory_valid"] = False
    if not isinstance(inventory, list):
        run.warnings.append("review: response has no provision inventory" if inventory is None
                            else "review: provision inventory is malformed; ignored")
        return
    bad = 0
    seen: set[str] = set()
    for item in inventory:
        try:
            note = (LegacyProvisionNote if legacy else ProvisionNote).model_validate(item)
        except ValidationError:
            bad += 1
            continue
        if note.id in seen:
            bad += 1
            run.warnings.append(f"review: inventory id {note.id!r} is repeated; later item ignored")
            continue
        seen.add(note.id)
        if (note.scope == "in_scope") != (note.category is not None):
            run.warnings.append(f"review: inventory {note.ref!r} is {note.scope} with category {note.category!r}")
        run.provision_inventory.append(note.model_dump())
    if bad:
        run.warnings.append(f"review: provision inventory is malformed ({bad} of {len(inventory)} items); "
                            "malformed items ignored")
    run.coverage["inventory_valid"] = bad == 0


def _record_empty_result(run: ExtractionRun, item: Any, source: SourceDocument) -> None:
    """The response's no-rules justification, with its evidence check (decided in finalize)."""
    if item is None:
        return
    try:
        just = NoRulesJustification.model_validate(item)
    except ValidationError:
        run.empty_result = {"justification": item, "verified": False, "problem": "malformed"}
        return
    check = verify_text(just.evidence, source.body, source.view)
    run.empty_result = {**just.model_dump(), "evidence_check": check.model_dump(),
                        "verified": check.status != "failed",
                        "problem": None if check.status != "failed" else f"evidence not found ({check.reason})"}
    if run.candidates:
        run.warnings.append("review: a no-rules justification was given although rules were produced; ignored")


def _verify_global_scope(run: ExtractionRun, items: Any, source: SourceDocument,
                         inventory_ids: set[str] | None = None, legacy: bool = False) -> list[dict[str, Any]]:
    """Document-level scope conditions. Only those whose verbatim evidence (one part, or two
    parts across one page artifact) is verified in the raw source, and whose governed
    provisions are the whole document or known inventory ids, are propagated; every entry
    is kept in the audit with its check."""
    if items is None:
        run.warnings.append("review: response has no global_scope list")
        return []
    verified: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(items if isinstance(items, list) else []):
        try:
            cond = (LegacyScopeCondition if legacy else ScopeCondition).model_validate(item)
        except ValidationError:
            run.warnings.append(f"global_scope[{i}] is malformed; not propagated")
            continue
        if cond.id in seen:
            run.warnings.append(f"global_scope id {cond.id!r} is duplicated; later entry not propagated")
            continue
        seen.add(cond.id)
        check, notes = verify_quote_parts(cond.evidence_parts, source.body, source.view)
        governed = cond.governed_provision_ids
        problem = None
        if check.status == "failed":
            problem = f"evidence not found in source ({check.reason})"
        elif governed is not None and not governed:
            problem = "it names no provision it governs (and is not document-wide)"
        elif governed is not None and inventory_ids is not None and (unknown := sorted(set(governed) - inventory_ids)):
            problem = f"it governs provision ids that are not in the inventory: {unknown}"
        scope = None
        if problem is None:
            words = scope_words(cond.model_dump(), check.start, check.end, source.body, source.view)
            scope = resolve_condition(cond.model_dump(), words, run.provision_inventory)
        run.global_scope.append({**cond.model_dump(), "evidence_check": check.model_dump(),
                                 "propagated": problem is None, "problem": problem, "scope": scope})
        run.warnings += [f"global_scope {cond.id}: {n}" for n in notes]
        if problem is None:
            verified.append({**cond.model_dump(exclude={"evidence_parts"}), "evidence": check.source_span,
                             "scope": scope})
        else:
            run.warnings.append(f"global_scope {cond.id}: {problem}; not propagated")
    return verified


def _propagate_scope(rule: ExtractedRule, scope: list[dict[str, Any]], source: SourceDocument,
                     res: CandidateResult) -> list[dict[str, Any]]:
    """Deterministic propagation of structural and explicit-reference conditions (scope.py):
    a condition applies when it reaches every provision (propagate_ids=None) or the rule
    links one of the provision ids it reaches. No citation or title text is compared.
    Named-subject conditions are NOT propagated here (only after a verified mapping). A
    rule escapes a condition only through a carve-out whose evidence is verified."""
    known = {c["id"] for c in scope}
    carved: dict[str, str] = {}
    for co in rule.scope_carve_outs:
        if co.scope_id not in known:
            res.warnings.append(f"review: carve-out names unknown or unverified scope id {co.scope_id!r}; ignored")
            continue
        check = verify_text(co.evidence, source.body, source.view)
        if check.status == "failed":
            res.warnings.append(f"review: carve-out from {co.scope_id} lacks verified evidence; condition still applied")
        else:
            carved[co.scope_id] = check.source_span
    res.scope_inputs["carved"] = carved
    applied = []
    for cond in scope:
        mode = cond["scope"]["mode"]
        if mode == "named_subject":
            continue
        governed = cond["scope"]["propagate_ids"]
        shared = sorted(set(governed or ()) & set(rule.provision_ids))
        if governed is not None and not shared:
            continue
        basis = (f"{mode}: every provision" if governed is None
                 else f"{mode}: rule links reached provision(s) {shared}")
        entry = {"id": cond["id"], "kind": cond["kind"], "mode": mode, "governed_provision_ids": governed,
                 "basis": basis}
        if cond["id"] in carved:
            res.propagated_scope.append({**entry, "applied": False, "carve_out_evidence": carved[cond["id"]]})
            continue
        res.propagated_scope.append({**entry, "applied": True})
        applied.append({**cond, "governed_provision_ids": governed})
    return applied


def _pydantic_errors(exc: ValidationError, prefix: str = "") -> list[str]:
    return [f"{prefix}{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()]


def evaluate_candidate(index: int, raw: Any, source: SourceDocument, as_of: date, validator,
                       ctx: DocContext | None = None) -> CandidateResult:
    ctx = ctx or DocContext()
    res = CandidateResult(index=index, raw=raw)
    if not isinstance(raw, dict):
        res.pydantic_errors.append("candidate is not a JSON object")
        res.rejection_reasons.append("pydantic: candidate is not an object")
        return res

    semantic, res.warnings = split_trusted(raw, source.meta)
    try:
        rule = (LegacyExtractedRule if ctx.legacy else ExtractedRule).model_validate(semantic)
    except ValidationError as exc:
        res.pydantic_errors = _pydantic_errors(exc)
        res.rejection_reasons.append("pydantic: candidate does not match the extraction model")
        try:  # diagnostic only
            res.citation, _ = verify_quote_parts(_QUOTE_PARTS.validate_python(semantic.get("quote_parts")),
                                                 source.body, source.view)
        except ValidationError:
            pass
        return res

    # Provision links: the only basis for scope targeting and coverage.
    res.provision_ids, res.source_basis = list(rule.provision_ids), rule.source_basis
    if ctx.inventory_ids is not None and (unknown := [p for p in rule.provision_ids if p not in ctx.inventory_ids]):
        res.rejection_reasons.append(f"provision link: {unknown} are not inventory provision ids")
    if not rule.provision_ids:
        if any(c["scope"]["propagate_ids"] is not None or c["scope"]["mode"] == "named_subject" for c in ctx.scope):
            res.rejection_reasons.append("provision link: the record links no inventory provision, so the "
                                         "document's provision-specific scope conditions cannot be applied")
        else:
            res.warnings.append("review: the record links no inventory provision")
    if rule.source_basis is None:
        res.warnings.append("review: source basis not declared (pre-v6 response)")
    elif rule.source_basis != "operative_text":
        res.warnings.append(f"review: the record rests on {rule.source_basis}, not operative legal text "
                            "(labelled in the published requirement)")

    # Citation integrity: the quote parts must be verbatim raw text (one span, or two
    # parts reconstructed into one raw span across exactly one page artifact).
    res.citation, notes = verify_quote_parts(rule.quote_parts, source.body, source.view)
    res.warnings += notes
    if res.citation.status == "failed":
        res.rejection_reasons.append(f"citation: quote not found in source text ({res.citation.reason})")
    elif res.citation.reconstructed:
        res.warnings.append("quote crosses one page artifact: the published span is the exact raw text, "
                            "including the recorded page artifact")
    elif res.citation.status == "normalized_match":
        res.warnings.append("quoted_span matched only after safe normalization; exact source text used in the record")
    elif res.citation.status == "layout_normalized_match":
        res.warnings.append("quoted_span matched only after layout normalization (whitespace next to brackets); "
                            "exact source text used in the record")

    # Enactment status: pending and failed must be shown by verified text; compared with the posture.
    if rule.enactment_status_evidence is not None:
        res.status_evidence = verify_text(rule.enactment_status_evidence, source.body, source.view)
        if res.status_evidence.status == "failed":
            res.rejection_reasons.append("citation: enactment_status_evidence not found in source text")
    elif rule.enactment_status in ("pending", "failed"):
        res.rejection_reasons.append(f"status: {rule.enactment_status} without verified enactment_status_evidence")

    # Effective date: verified evidence, then deterministic consequences of its classification
    # (a relative formula is resolved only by temporal.resolve_relative).
    if rule.effective_date_evidence is not None:
        res.effective_date_evidence = verify_text(rule.effective_date_evidence, source.body, source.view)
    temporal = resolve_effective_date(rule, res.effective_date_evidence, source.body, as_of, ctx.base_dates)
    res.temporal = temporal.audit
    res.rejection_reasons += temporal.rejections
    res.warnings += temporal.warnings
    expected = EXPECTED_STATUS.get(ctx.posture.get("established", "unknown"))
    res.temporal["posture"] = ctx.posture.get("established")
    res.temporal["posture_consistent"] = None if expected is None else rule.enactment_status == expected
    if res.temporal["posture_consistent"] is False:
        res.warnings.append(f"review: enactment_status {rule.enactment_status} is inconsistent with the document "
                            f"posture {ctx.posture['established']}")

    if rule.enactment_status in ("pending", "failed"):
        # Proposals: pending vs failed is decided deterministically (legislative.py).
        res.legislative = legislative_status(rule.enactment_status, source.body, ctx.session, as_of)
        status = res.legislative["status"]
        res.status_derivation = (f"legislative: {res.legislative['status_basis']}" if status is not None else
                                 f"{TEMPORAL_UNRESOLVED}: legislative status undetermined: "
                                 f"{res.legislative['conflict']}")
        if res.legislative.get("conflict"):
            res.warnings.append(f"review: legislative status: {res.legislative['conflict']}")
    else:
        status, res.status_derivation = derive_status(rule.enactment_status, temporal.effective_date,
                                                      temporal.has_date_evidence, as_of, no_date_in_force(ctx.posture))
    if status is None:
        res.rejection_reasons.append(f"status: {res.status_derivation}")
        if res.status_derivation.startswith(TEMPORAL_UNRESOLVED):
            res.temporal["unresolved_reason"] = f"status: {res.status_derivation}"

    # Version history must be verbatim. Its dates are classified by the validity window below:
    # a future repeal/expiry is an END (the current wording still applies), a future
    # amendment date a later START; an unclassified future date holds the record.
    if rule.version_evidence is not None:
        res.version_evidence = verify_text(rule.version_evidence, source.body, source.view)
        if res.version_evidence.status == "failed":
            res.rejection_reasons.append("citation: version_evidence not found in source text")
    elif rule.version_note is not None:
        res.warnings.append("review: version_note given without verbatim version_evidence")

    # Operative conditions: non-calendar triggers. They never set a date or change status;
    # they are preserved (with verified evidence) so applicability can later be `unknown`.
    operative = []
    for oc in rule.operative_conditions:
        check = verify_text(oc.evidence, source.body, source.view)
        res.operative_conditions.append({"statement": oc.statement, "evidence_check": check.model_dump()})
        if check.status == "failed":
            res.rejection_reasons.append("citation: operative condition evidence not found in source text")
        else:
            operative.append(oc)
    for oc in temporal.operative:  # effective-date evidence classified as an operative condition
        res.operative_conditions.append({"statement": oc.statement, "from": "effective_date_evidence",
                                         "evidence_check": res.effective_date_evidence.model_dump()})
        operative.append(oc)
    if operative:
        res.warnings.append("review: applicability depends on an unresolved operative condition")

    status, effective_date = _apply_validity(res, rule, source, status, temporal.effective_date, as_of)

    res.scope_inputs.update(exemptions=rule.exemptions, coverage_conditions=rule.coverage_conditions,
                            operative=[oc.statement for oc in operative])
    propagated = _propagate_scope(rule, ctx.scope, source, res)
    res.rule = build_record(rule, source.meta, res.citation, status, effective_date, propagated, operative,
                            basis=rule.source_basis)
    if res.citation.status != "failed" and res.rule["quoted_span"] not in source.body:
        res.rejection_reasons.append("citation: published quoted_span is not a contiguous raw substring")  # invariant
    legal_text = " ".join(p["model_text"] for p in res.citation.parts) if res.citation.reconstructed \
        else res.rule["quoted_span"]  # figure checks ignore page-header text inside a reconstructed span
    for field, text in (("requirement", rule.requirement), ("key_value", rule.key_value)):
        for figure in unsupported_figures(text or "", legal_text):
            res.warnings.append(f"review: {field} states '{figure}', which its quoted_span does not contain")
    # A held record (status undetermined) is validated with a placeholder status, so that
    # every OTHER defect still rejects it; the placeholder is never published.
    checked = res.rule if status is not None else {**res.rule, "status": "pending"}
    try:
        RuleRecord.model_validate(checked)
        res.pydantic_valid = True
    except ValidationError as exc:
        res.pydantic_errors = _pydantic_errors(exc, prefix="record.")
        res.rejection_reasons.append("pydantic: normalized record does not match RuleRecord")
    res.schema_errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
                         for e in validator.iter_errors(checked)]
    res.schema_valid = not res.schema_errors
    if res.schema_errors:
        res.rejection_reasons.append("official schema: normalized record is invalid")
    res.historical = res.historical and not res.rejection_reasons
    if res.historical:
        res.rule["status"] = None   # validated as of its own window; never published as current
    res.accepted = not res.rejection_reasons and not res.historical
    res.temporal_state = res.temporal_state if res.historical else status if res.accepted else None
    return res


def _apply_validity(res: CandidateResult, rule: ExtractedRule, source: SourceDocument, status: str | None,
                    effective_date: str | None, as_of: date) -> tuple[str | None, str | None]:
    """The record's validity window (validity.py) from its verified passages, and its
    consequence for an enacted record. Returns the (possibly changed) status and effective date."""
    pieces = [("quoted_span", res.citation, False), ("version_evidence", res.version_evidence, True)]
    ev = res.effective_date_evidence
    if ev is not None and ev.status != "failed":
        if res.temporal.get("applied_kind") == "history_note":
            s, e = enclosing_note_span(source.body, ev.start, ev.end)
            note = source.body[s:e]
            pieces.append(("history_note", CitationCheck(status="exact_match", start=s, end=e, occurrences=1,
                                                         model_span=note, source_span=note), True))
        else:
            pieces.append(("effective_date_evidence", ev, False))
    pieces += [("operative_condition", CitationCheck.model_validate(oc["evidence_check"]), False)
               for oc in res.operative_conditions if oc["evidence_check"]["status"] != "failed"]
    res.validity = resolve_window(pieces)
    if rule.enactment_status != "enacted":
        return status, effective_date          # proposals: legislative.py decides; window only recorded
    window = res.validity["decision"] = decide_window(res.validity, as_of)
    unresolved = res.temporal.get("unresolved_reason")
    if window["outcome"] == "historical" and status == "not_yet_effective":
        window = {**window, "outcome": "hold",
                  "reason": f"a start after as_of but an end on or before it ({window['reason']})"}
    if window["outcome"] == "historical" and (status == "in_force" or unresolved):
        if status is None:                     # an unknown start no longer matters: the window has ended
            res.rejection_reasons.remove(res.temporal.pop("unresolved_reason"))
        res.historical, res.temporal_state = True, window["state"]
        res.status_derivation = f"historical ({window['state']}): {window['reason']}"
        return "in_force", effective_date      # placeholder for validation only (status is never published)
    if window["outcome"] == "hold" and status is not None:
        res.status_derivation = f"{TEMPORAL_UNRESOLVED}: {window['reason']}"
        res.rejection_reasons.append(f"status: {res.status_derivation}")
        res.temporal["unresolved_reason"] = f"status: {res.status_derivation}"
        return None, effective_date
    if window["outcome"] == "not_yet_effective" and status == "in_force":
        res.status_derivation = f"enacted; {window['reason']}"
        return "not_yet_effective", window["start"]
    if window["state"] == "current_until" and status is not None:
        res.status_derivation += f"; {window['reason']}"
    return status, effective_date


def write_artifact(run: ExtractionRun, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
