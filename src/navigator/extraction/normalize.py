"""Deterministic post-processing: model output + trusted metadata -> rule record.

Trust boundary:
  from the repository  team_rule_id (ids.py), jurisdiction, level, source_doc_id,
                       source_url, overrides ([] at single-document extraction)
  derived in Python    status, from the model's enactment_status and the
                       effective_date admitted by temporal.py and the query
                       date; document-level scope conditions propagated to
                       each rule they govern; quoted_span = verified raw text
  from the model       the semantic fields (category, title, requirement, ...)
  never used           model confidence: dropped, published as null
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any

from navigator.extraction.ids import make_team_rule_id
from navigator.extraction.models import CitationCheck, ExtractedRule, OperativeCondition, SourceMeta, Status

# Fields the model must not supply. If present they are dropped and reported.
TRUSTED_FIELDS = ("team_rule_id", "jurisdiction", "level", "status", "source_doc_id", "source_url",
                  "overrides", "retrieved_at", "conflict_flag")
# Model output that is never used for acceptance or applicability (kept only in `raw`).
IGNORED_FIELDS = ("confidence",)


def level_for(jurisdiction: str) -> str:
    """Manifest jurisdictions are a state code ('MA') or 'City, ST'."""
    return "state" if re.fullmatch(r"[A-Z]{2}", jurisdiction) else "city"


def split_trusted(raw: dict[str, Any], meta: SourceMeta) -> tuple[dict[str, Any], list[str]]:
    """Remove trusted fields from a raw model candidate; warn about any it supplied."""
    trusted_values = {"jurisdiction": meta.jurisdiction, "level": level_for(meta.jurisdiction),
                      "source_doc_id": meta.doc_id, "source_url": meta.url, "retrieved_at": meta.retrieved_at}
    semantic, warnings = dict(raw), []
    for name in TRUSTED_FIELDS:
        if name not in semantic:
            continue
        value = semantic.pop(name)
        expected = trusted_values.get(name)
        detail = f" (model said {value!r}, repository says {expected!r})" if expected is not None and value != expected else ""
        warnings.append(f"model supplied trusted field {name!r}; ignored, repository value used{detail}")
    for name in IGNORED_FIELDS:
        if semantic.pop(name, None) is not None:
            warnings.append(f"model supplied {name!r}; ignored (never used for acceptance or applicability)")
    return semantic, warnings


def _scope_text(statement: str, citation: str, governs: str | None) -> str:
    return f"{statement} [{citation}; {'document-wide' if governs is None else 'applies to ' + governs}]"


def compose_scope(rule_text: str | None, propagated: list[dict[str, Any]], kind: str,
                  operative: list[OperativeCondition] = ()) -> str | None:
    """Rule-specific text first, then propagated document-level conditions of `kind`,
    then unresolved operative conditions. Exact duplicates are dropped."""
    parts = [rule_text] if rule_text else []
    parts += [_scope_text(p["statement"], p["citation"], p["governs"]) for p in propagated if p["kind"] == kind]
    parts += [f"Operative condition (unresolved; applicability may be unknown): {c.statement}" for c in operative]
    unique = list(dict.fromkeys(p.strip() for p in parts if p and p.strip()))
    return "; ".join(unique) or None


def _date_bounds(partial: str) -> tuple[date, date]:
    parts = [int(p) for p in partial.split("-")]
    if len(parts) == 3:
        d = date(*parts)
        return d, d
    if len(parts) == 2:
        y, m = parts
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    return date(parts[0], 1, 1), date(parts[0], 12, 31)


def derive_status(enactment_status: str, effective_date: str | None, has_date_evidence: bool,
                  as_of: date) -> tuple[Status | None, str]:
    """Return (status, explanation). status is None when it cannot be determined."""
    if enactment_status in ("pending", "failed"):
        return enactment_status, f"enactment_status={enactment_status}"
    if effective_date is None:
        if has_date_evidence:
            return None, ("enacted, but the effective date is stated only in relative or conditional terms; "
                          "it must be resolved deterministically before a status can be assigned")
        return "in_force", "enacted; the document states no effective date"
    try:
        earliest, latest = _date_bounds(effective_date)
    except ValueError:
        return None, f"effective_date {effective_date!r} is not a real calendar date"
    if latest <= as_of:
        return "in_force", f"enacted; effective {effective_date} is on or before as_of {as_of}"
    if earliest > as_of:
        return "not_yet_effective", f"enacted; effective {effective_date} is after as_of {as_of}"
    return None, f"effective_date {effective_date!r} is too imprecise to compare with as_of {as_of}"


def build_record(rule: ExtractedRule, meta: SourceMeta, citation: CitationCheck, status: Status | None,
                 effective_date: str | None, propagated: list[dict[str, Any]] = (),
                 operative: list[OperativeCondition] = ()) -> dict[str, Any]:
    """Assemble a record in official-schema shape.

    quoted_span is always the exact raw source text located by the citation check
    (citation.source_span): for a normalized match, and for a cross-page quote
    reconstructed from two parts (which then contains the recorded page artifact).
    The model's own quote parts stay in the audit record. `effective_date` is the
    value decided by temporal.py, not the model's. `propagated` are verified
    document-level scope conditions governing this rule; `operative` are verified
    operative conditions (status is not changed by them).
    """
    span = citation.source_span if citation.status != "failed" and citation.source_span else citation.model_span
    return {
        "team_rule_id": make_team_rule_id(source_doc_id=meta.doc_id, category=rule.category,
                                          citation=rule.citation, quoted_span=span),
        "jurisdiction": meta.jurisdiction,
        "level": level_for(meta.jurisdiction),
        "category": rule.category,
        "status": status,
        "title": rule.title,
        "requirement": rule.requirement,
        "key_value": rule.key_value,
        "coverage_conditions": compose_scope(rule.coverage_conditions, list(propagated), "coverage_condition",
                                             list(operative)),
        "exemptions": compose_scope(rule.exemptions, list(propagated), "exemption"),
        "overrides": [],
        "interaction": rule.interaction,
        "effective_date": effective_date,
        "citation": rule.citation,
        "source_doc_id": meta.doc_id,
        "source_url": meta.url,
        "quoted_span": span,
        "confidence": None,  # model self-confidence is not used (see IGNORED_FIELDS)
        "conflict_flag": rule.conflict_note is not None,
        "conflict_note": rule.conflict_note,
    }
