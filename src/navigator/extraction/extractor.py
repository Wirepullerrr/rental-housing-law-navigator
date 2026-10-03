"""Extraction orchestration for ONE supplied corpus document.

    supplied text -> prompt -> [cache | provider] -> raw JSON
      -> per candidate: Pydantic ExtractedRule -> citation check -> status derivation
         -> trusted metadata + team_rule_id -> RuleRecord (Pydantic) -> official JSON Schema
      -> ExtractionRun audit record

A candidate is accepted only if every stage passes. Failures stay separate and
explicit; nothing is retried or repaired here. Vendor-neutral: depends only on
the StructuredLLMProvider protocol.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache, cache_key, canonical_json, sha256_hex
from navigator.extraction.citation import verify_span
from navigator.extraction.config import DEFAULT_AS_OF, GENERATION_SETTINGS
from navigator.extraction.models import (CandidateResult, ExtractedRule, ExtractionRun, ProvisionNote, RuleRecord,
                                         SourceMeta, generation_json_schema)
from navigator.extraction.normalize import build_record, derive_status, split_trusted
from navigator.extraction.prompt import EXTRACTION_PROMPT_VERSION, render_prompt
from navigator.extraction.provider import StructuredLLMProvider
from navigator.extraction.review import calendar_dates, uncovered_provisions, uniform_confidence, unsupported_figures
from navigator.validation import make_rule_validator

_INVENTORY = TypeAdapter(list[ProvisionNote])


class SourceNotAvailable(ValueError):
    """The doc id is unknown, or has no supplied local text."""


class CacheMiss(RuntimeError):
    """No cached response for these inputs and no live provider was given."""


@dataclass(frozen=True)
class SourceDocument:
    meta: SourceMeta
    body: str


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


def prepare_request(source: SourceDocument, provider_name: str, model: str,
                    settings: dict[str, Any] = GENERATION_SETTINGS) -> PreparedRequest:
    system, user = render_prompt(source.meta, source.body)
    schema = generation_json_schema()
    key_fields = {
        "source_doc_id": source.meta.doc_id,
        "source_sha256": source.meta.content_sha256,
        "provider": provider_name,
        "model": model,
        "prompt_version": EXTRACTION_PROMPT_VERSION,
        "prompt_sha256": sha256_hex(system + "\x00" + user),
        "response_schema_sha256": sha256_hex(canonical_json(schema)),
        "generation_settings": settings,
    }
    return PreparedRequest(system, user, schema, key_fields, cache_key(key_fields))


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


def extract_document(source: SourceDocument, *, provider_name: str, model: str,
                     cache: ResponseCache, provider: StructuredLLMProvider | None = None,
                     as_of: date = DEFAULT_AS_OF, force: bool = False,
                     settings: dict[str, Any] = GENERATION_SETTINGS,
                     schema_path: Path = sp.REPO_ROOT / sp.SCHEMA_PATH) -> ExtractionRun:
    """Extract rules from one document. Calls `provider` only on a cache miss (or with force=True)."""
    if provider is not None and (provider.name, provider.model) != (provider_name, model):
        raise ValueError(f"provider is {provider.name}/{provider.model}, expected {provider_name}/{model}")
    req = prepare_request(source, provider_name, model, settings)
    entry = None if force else cache.get(req.key)
    cache_hit = entry is not None
    if entry is None:
        if provider is None:
            raise CacheMiss(f"no cached response for {source.meta.doc_id} with these inputs (key {req.key[:12]})")
        result = provider.generate(system_instruction=req.system_instruction, prompt=req.prompt,
                                   response_json_schema=req.response_json_schema, settings=settings)
        entry = {"key": req.key, "key_fields": req.key_fields, "created_at": _now(),
                 "response_text": result.text, "provider_metadata": result.metadata}
        cache.put(req.key, entry)

    run = ExtractionRun(
        run_at=_now(), source=source.meta, provider=provider_name, model=model,
        prompt_version=EXTRACTION_PROMPT_VERSION, prompt_sha256=req.key_fields["prompt_sha256"],
        response_schema_sha256=req.key_fields["response_schema_sha256"], generation_settings=settings,
        as_of=as_of.isoformat(), cache_key=req.key, cache_hit=cache_hit,
        cache_entry=_display_path(cache.path(req.key)), provider_metadata=entry.get("provider_metadata") or {},
        raw_response_text=entry["response_text"],
    )
    _evaluate_response(run, source, as_of, _official_validator(Path(schema_path)))
    return run


def _evaluate_response(run: ExtractionRun, source: SourceDocument, as_of: date, validator) -> None:
    try:
        payload = json.loads(run.raw_response_text)
    except json.JSONDecodeError as exc:
        run.errors.append(f"response is not valid JSON: {exc}")
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        run.errors.append("response must be a JSON object with a 'rules' list")
        return
    if set(payload) - {"rules", "provisions"}:
        run.warnings.append(f"ignored unexpected top-level keys: {sorted(set(payload) - {'rules', 'provisions'})}")
    _record_inventory(run, payload.get("provisions"))

    run.candidates = [evaluate_candidate(i, raw, source, as_of, validator) for i, raw in enumerate(payload["rules"])]
    seen: dict[str, int] = {}
    for c in run.candidates:
        if c.accepted:
            rid = c.rule["team_rule_id"]
            if rid in seen:
                c.accepted = False
                c.rejection_reasons.append(f"duplicate: same team_rule_id {rid} as candidate {seen[rid]}")
            else:
                seen[rid] = c.index
    run.candidate_count = len(run.candidates)
    run.rules = [c.rule for c in run.candidates if c.accepted]
    run.accepted_count = len(run.rules)
    for c in run.candidates:
        if not c.accepted:
            run.warnings.append(f"candidate {c.index} rejected: {'; '.join(c.rejection_reasons)}")
    if (msg := uniform_confidence(r["confidence"] for r in run.rules)) is not None:
        run.warnings.append(msg)
    # Recall check: any extracted record counts, including ones rejected later (e.g. for status).
    extracted = [c.rule["citation"] for c in run.candidates if c.rule]
    if uncovered := uncovered_provisions(run.provision_inventory, extracted):
        run.warnings.append(f"review: provisions marked in scope but cited by no extracted record: {uncovered}")


def _record_inventory(run: ExtractionRun, inventory: Any) -> None:
    """The model's provision inventory is audit-only: a malformed one is a warning, never an error."""
    if inventory is None:
        run.warnings.append("review: response has no provision inventory")
        return
    try:
        run.provision_inventory = [n.model_dump() for n in _INVENTORY.validate_python(inventory)]
    except ValidationError:
        run.warnings.append("review: provision inventory is malformed; ignored")


def _pydantic_errors(exc: ValidationError, prefix: str = "") -> list[str]:
    return [f"{prefix}{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()]


def evaluate_candidate(index: int, raw: Any, source: SourceDocument, as_of: date, validator) -> CandidateResult:
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
        if isinstance(semantic.get("quoted_span"), str):  # diagnostic only
            res.citation = verify_span(semantic["quoted_span"], source.body)
        return res

    # Citation integrity: the quoted span and any effective-date evidence must be in the source.
    res.citation = verify_span(rule.quoted_span, source.body)
    if res.citation.status == "failed":
        res.rejection_reasons.append("citation: quoted_span not found in source text")
    elif res.citation.status == "normalized_match":
        res.warnings.append("quoted_span matched only after safe normalization; exact source text used in the record")
    evidence_ok = False
    if rule.effective_date_evidence is not None:
        res.effective_date_evidence = verify_span(rule.effective_date_evidence, source.body)
        evidence_ok = res.effective_date_evidence.status != "failed"
        if not evidence_ok:
            res.rejection_reasons.append("citation: effective_date_evidence not found in source text")
    if rule.effective_date is not None and not evidence_ok:
        res.rejection_reasons.append("effective_date: not supported by verified verbatim evidence")

    status, res.status_derivation = derive_status(rule.enactment_status, rule.effective_date, evidence_ok, as_of)
    if status is None:
        res.rejection_reasons.append(f"status: {res.status_derivation}")

    # Version history is evidence for later temporal modelling, never a conflict.
    # A verified annotation dated after as_of means the extracted (latest) wording
    # may not apply yet: reject conservatively rather than publish it as in force.
    if rule.version_evidence is not None:
        res.version_evidence = verify_span(rule.version_evidence, source.body)
        if res.version_evidence.status == "failed":
            res.rejection_reasons.append("citation: version_evidence not found in source text")
        elif later := [d for d in calendar_dates(res.version_evidence.source_span) if d > as_of]:
            res.rejection_reasons.append(f"status: a version annotation is dated {later[0]}, after as_of {as_of}; "
                                         "the extracted wording may not apply yet")
    elif rule.version_note is not None:
        res.warnings.append("review: version_note given without verbatim version_evidence")

    res.rule = build_record(rule, source.meta, res.citation, status)
    for field, text in (("requirement", rule.requirement), ("key_value", rule.key_value)):
        for figure in unsupported_figures(text or "", res.rule["quoted_span"]):
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
