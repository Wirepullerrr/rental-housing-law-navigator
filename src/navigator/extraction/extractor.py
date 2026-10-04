"""Extraction orchestration for ONE supplied corpus document.

    supplied text -> canonical view (page artifacts marked, segments numbered) -> prompt
      -> [cache | provider] -> raw JSON
      -> provision inventory and document-level scope conditions (evidence verified)
      -> per candidate: Pydantic ExtractedRule -> quote parts verified against the RAW text
         (a cross-page quote is reconstructed as one exact raw span) -> effective-date
         evidence classification -> status derivation -> scope propagation + operative
         conditions -> trusted metadata + team_rule_id -> RuleRecord -> official JSON Schema
      -> completeness: inventory coverage closure + subdivision guard (coverage.py)
      -> repair targets = their merged union (repair.py)
      -> at most ONE repair request for those targets; each target is classified
         in_scope / out_of_scope / uncertain, and its candidates go through exactly
         the same evaluation
      -> completeness rerun; every target resolved or the document review_required
      -> ExtractionRun audit record, document_status complete | review_required

A candidate is accepted only if every stage passes. Rejected candidates are never
retried or repaired. Vendor-neutral: depends only on the StructuredLLMProvider protocol.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache, cache_key, canonical_json, sha256_hex
from navigator.extraction.config import DEFAULT_AS_OF, GENERATION_SETTINGS
from navigator.extraction.coverage import candidate_citation, closure, same_ref, unrecorded_subdivisions
from navigator.extraction.models import (CandidateResult, ExtractedRule, ExtractionRun, ProvisionNote, QuotePart,
                                         RepairPass, RepairResponse, RuleRecord, ScopeCondition, SourceMeta,
                                         generation_json_schema)
from navigator.extraction.normalize import build_record, derive_status, split_trusted
from navigator.extraction.prompt import (EXTRACTION_PROMPT_VERSION, REPAIR_PROMPT_VERSION, render_prompt,
                                        render_repair_prompt)
from navigator.extraction.provider import ProviderError, StructuredLLMProvider
from navigator.extraction.repair import (attach_resolutions, build_targets, canonical_order,
                                        repair_scope_rejection, resolve_targets, target_identity)
from navigator.extraction.quotes import verify_quote_parts, verify_text
from navigator.extraction.review import calendar_dates, ref_matches, unsupported_figures
from navigator.extraction.source_view import PAGE_BREAK_MARKER, SEGMENT_LABEL, SourceView, build_view
from navigator.extraction.temporal import resolve_effective_date
from navigator.validation import make_rule_validator

_QUOTE_PARTS = TypeAdapter(list[QuotePart])
_TOP_LEVEL_KEYS = {"provisions", "global_scope", "rules"}
_REPAIR_KEYS = {"target_resolutions", "rules"}


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
                           primary_key: str, scope: list[dict[str, Any]],
                           targets: list[dict[str, Any]]) -> PreparedRequest:
    """The key names the complete, sorted target set explicitly (as well as through the prompt
    hash): a different target set never reuses a cached repair response."""
    system, user = render_repair_prompt(source.meta, source.view.text, scope, canonical_order(targets))
    return _prepared(source, provider_name, model, settings, system, user, generation_json_schema(RepairResponse),
                     **{"pass": "repair", "primary_cache_key": primary_key,
                        "repair_prompt_version": REPAIR_PROMPT_VERSION, "repair_targets": target_identity(targets)})


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
        _record_inventory(run, payload.get("provisions"))
        scope = _verify_global_scope(run, payload.get("global_scope"), source)
        run.candidates = [evaluate_candidate(i, raw, source, as_of, validator, scope)
                          for i, raw in enumerate(payload["rules"])]
        _dedupe(run.candidates)
        primary, gaps = _completeness(run, source)
        run.coverage["after_primary"] = _summary(primary, gaps)
        if targets := build_targets(primary, gaps):   # every deterministic check runs BEFORE repair
            if repair:
                run.repair = _repair_pass(run, source, req.key, scope, targets, provider_name=provider_name,
                                          model=model, cache=cache, provider=provider, force=force,
                                          settings=settings, as_of=as_of, validator=validator)
            else:
                run.repair = RepairPass(reason="repair disabled for this run", targets=targets)
    _finalize(run, source)
    return run


def _completeness(run: ExtractionRun, source: SourceDocument) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Both deterministic completeness checks over the current candidates."""
    return (closure(run.provision_inventory, run.candidates),
            unrecorded_subdivisions(run.provision_inventory, run.candidates, source.body))


def _summary(result: dict[str, Any], gaps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"uncovered": result["uncovered"], "unaccepted": result["unaccepted"],
            "unrecorded_subdivisions": [s["ref"] for g in gaps for s in g["subdivisions"]]}


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


def _repair_pass(run: ExtractionRun, source: SourceDocument, primary_key: str, scope: list[dict[str, Any]],
                 targets: list[dict[str, Any]], *, provider_name: str, model: str, cache: ResponseCache,
                 provider: StructuredLLMProvider | None, force: bool, settings: dict[str, Any], as_of: date,
                 validator) -> RepairPass:
    """The single repair request of this run. Its candidates are evaluated exactly like primary
    ones; the only extra check can only reject (repair.repair_scope_rejection)."""
    req = prepare_repair_request(source, provider_name, model, settings, primary_key, scope, targets)
    kinds = sorted({s for t in targets for s in t["sources"]})
    rp = RepairPass(reason=f"{len(targets)} repair target(s) from {', '.join(kinds)}",
                    prompt_version=REPAIR_PROMPT_VERSION, targets=targets, cache_key=req.key,
                    prompt_sha256=req.key_fields["prompt_sha256"], cache_entry=_display_path(cache.path(req.key)))
    targets = rp.targets            # the audit's own copies: resolutions are attached to these
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
    for raw in payload["rules"]:
        c = evaluate_candidate(len(run.candidates), raw, source, as_of, validator, scope)
        c.origin = "repair"
        if reason := repair_scope_rejection(candidate_citation(c), targets):
            c.accepted = False
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


def _finalize(run: ExtractionRun, source: SourceDocument) -> None:
    _dedupe(run.candidates)
    run.candidate_count = len(run.candidates)
    run.rules = [c.rule for c in run.candidates if c.accepted]
    run.accepted_count = len(run.rules)
    for c in run.candidates:
        if not c.accepted:
            run.warnings.append(f"candidate {c.index} rejected: {'; '.join(c.rejection_reasons)}")

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
    if run.coverage["uncertain"]:
        run.warnings.append(f"review: provisions with uncertain scope: {run.coverage['uncertain']}")
    if final["link_mismatches"]:
        run.warnings.append(f"review: inventory rule links that do not match the rule's citation: "
                            f"{len(final['link_mismatches'])}")

    reasons = []
    if run.errors:
        reasons.append("the response could not be processed")
    elif not run.coverage.get("inventory_valid", False):
        reasons.append("no complete, well-formed provision inventory; coverage cannot be established")
    elif not run.provision_inventory and run.candidates:
        reasons.append("the inventory lists no provisions although rules were produced")
    if unresolved := [t for t in targets if t["final_resolution"] == "unresolved"]:
        reasons.append("repair targets still unresolved: "
                       + "; ".join(f"{t['ref']} ({t['unresolved_reason']})" for t in unresolved))
    if refs := [r for r in final["uncovered"] if open_gap(r)]:
        reasons.append(f"in-scope provisions with no candidate record: {refs}")
    if refs := [r for r in final["unaccepted"] if open_gap(r)]:
        reasons.append(f"in-scope provisions whose candidate records were all rejected: {refs}")
    if refs := [s["ref"] for g in gaps for s in g["subdivisions"] if open_gap(s["ref"])]:
        reasons.append(f"source subdivisions with no accepted record (not repair targets): {refs}")
    if rp is not None:
        reasons += [f"repair pass: {e}" for e in rp.errors]
    run.review_reasons = reasons
    run.document_status = "review_required" if reasons else "complete"


def _record_inventory(run: ExtractionRun, inventory: Any) -> None:
    """Validate the inventory item by item. Malformed items are dropped and make coverage
    unprovable (document review_required); they never reject a rule."""
    run.coverage["inventory_valid"] = False
    if not isinstance(inventory, list):
        run.warnings.append("review: response has no provision inventory" if inventory is None
                            else "review: provision inventory is malformed; ignored")
        return
    bad = 0
    for item in inventory:
        try:
            note = ProvisionNote.model_validate(item)
        except ValidationError:
            bad += 1
            continue
        if (note.scope == "in_scope") != (note.category is not None):
            run.warnings.append(f"review: inventory {note.ref!r} is {note.scope} with category {note.category!r}")
        run.provision_inventory.append(note.model_dump())
    if bad:
        run.warnings.append(f"review: provision inventory is malformed ({bad} of {len(inventory)} items); "
                            "malformed items ignored")
    run.coverage["inventory_valid"] = bad == 0


def _verify_global_scope(run: ExtractionRun, items: Any, source: SourceDocument) -> list[dict[str, Any]]:
    """Document-level scope conditions. Only those whose verbatim evidence (one part, or two
    parts across one page artifact) is verified in the raw source are propagated; every
    entry is kept in the audit with its check."""
    if items is None:
        run.warnings.append("review: response has no global_scope list")
        return []
    verified: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(items if isinstance(items, list) else []):
        try:
            cond = ScopeCondition.model_validate(item)
        except ValidationError:
            run.warnings.append(f"global_scope[{i}] is malformed; not propagated")
            continue
        if cond.id in seen:
            run.warnings.append(f"global_scope id {cond.id!r} is duplicated; later entry not propagated")
            continue
        seen.add(cond.id)
        check, notes = verify_quote_parts(cond.evidence_parts, source.body, source.view)
        ok = check.status != "failed"
        run.global_scope.append({**cond.model_dump(), "evidence_check": check.model_dump(), "propagated": ok})
        run.warnings += [f"global_scope {cond.id}: {n}" for n in notes]
        if ok:
            verified.append({**cond.model_dump(exclude={"evidence_parts"}), "evidence": check.source_span})
        else:
            run.warnings.append(f"global_scope {cond.id} evidence not found in source ({check.reason}); "
                                "not propagated")
    return verified


def _propagate_scope(rule: ExtractedRule, scope: list[dict[str, Any]], source: SourceDocument,
                     res: CandidateResult) -> list[dict[str, Any]]:
    """Deterministic propagation: a condition applies when it governs the whole document
    (governs=None) or its `governs` ref structurally matches the rule's citation. A rule
    escapes a condition only through a carve-out whose evidence is verified in the source."""
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
    applied = []
    for cond in scope:
        if cond["governs"] is not None and not ref_matches(cond["governs"], rule.citation):
            continue
        basis = "document-wide" if cond["governs"] is None else f"rule citation is within {cond['governs']}"
        if cond["id"] in carved:
            res.propagated_scope.append({"id": cond["id"], "kind": cond["kind"], "applied": False,
                                         "basis": basis, "carve_out_evidence": carved[cond["id"]]})
            continue
        res.propagated_scope.append({"id": cond["id"], "kind": cond["kind"], "applied": True, "basis": basis})
        applied.append(cond)
    return applied


def _pydantic_errors(exc: ValidationError, prefix: str = "") -> list[str]:
    return [f"{prefix}{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()]


def evaluate_candidate(index: int, raw: Any, source: SourceDocument, as_of: date, validator,
                       scope: list[dict[str, Any]] = ()) -> CandidateResult:
    res = CandidateResult(index=index, raw=raw)
    if not isinstance(raw, dict):
        res.pydantic_errors.append("candidate is not a JSON object")
        res.rejection_reasons.append("pydantic: candidate is not an object")
        return res

    semantic, res.warnings = split_trusted(raw, source.meta)
    try:
        rule = ExtractedRule.model_validate(semantic)
    except ValidationError as exc:
        res.pydantic_errors = _pydantic_errors(exc)
        res.rejection_reasons.append("pydantic: candidate does not match the extraction model")
        try:  # diagnostic only
            res.citation, _ = verify_quote_parts(_QUOTE_PARTS.validate_python(semantic.get("quote_parts")),
                                                 source.body, source.view)
        except ValidationError:
            pass
        return res

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

    # Effective date: verified evidence, then deterministic consequences of its classification.
    if rule.effective_date_evidence is not None:
        res.effective_date_evidence = verify_text(rule.effective_date_evidence, source.body, source.view)
    temporal = resolve_effective_date(rule, res.effective_date_evidence, source.body, as_of)
    res.temporal = temporal.audit
    res.rejection_reasons += temporal.rejections
    res.warnings += temporal.warnings

    status, res.status_derivation = derive_status(rule.enactment_status, temporal.effective_date,
                                                  temporal.has_date_evidence, as_of)
    if status is None:
        res.rejection_reasons.append(f"status: {res.status_derivation}")

    # Version history is evidence for later temporal modelling, never a conflict.
    # A verified annotation dated after as_of means the extracted (latest) wording
    # may not apply yet: reject conservatively rather than publish it as in force.
    if rule.version_evidence is not None:
        res.version_evidence = verify_text(rule.version_evidence, source.body, source.view)
        if res.version_evidence.status == "failed":
            res.rejection_reasons.append("citation: version_evidence not found in source text")
        elif later := [d for d in calendar_dates(res.version_evidence.source_span) if d > as_of]:
            res.rejection_reasons.append(f"status: a version annotation is dated {later[0]}, after as_of {as_of}; "
                                         "the extracted wording may not apply yet")
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

    propagated = _propagate_scope(rule, list(scope), source, res)
    res.rule = build_record(rule, source.meta, res.citation, status, temporal.effective_date, propagated, operative)
    if res.citation.status != "failed" and res.rule["quoted_span"] not in source.body:
        res.rejection_reasons.append("citation: published quoted_span is not a contiguous raw substring")  # invariant
    legal_text = " ".join(p["model_text"] for p in res.citation.parts) if res.citation.reconstructed \
        else res.rule["quoted_span"]  # figure checks ignore page-header text inside a reconstructed span
    for field, text in (("requirement", rule.requirement), ("key_value", rule.key_value)):
        for figure in unsupported_figures(text or "", legal_text):
            res.warnings.append(f"review: {field} states '{figure}', which its quoted_span does not contain")
    try:
        RuleRecord.model_validate(res.rule)
        res.pydantic_valid = True
    except ValidationError as exc:
        res.pydantic_errors = _pydantic_errors(exc, prefix="record.")
        res.rejection_reasons.append("pydantic: normalized record does not match RuleRecord")
    res.schema_errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
                         for e in validator.iter_errors(res.rule)]
    res.schema_valid = not res.schema_errors
    if res.schema_errors:
        res.rejection_reasons.append("official schema: normalized record is invalid")
    res.accepted = not res.rejection_reasons
    return res


def write_artifact(run: ExtractionRun, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
