"""Effective-date evidence: deterministic consequences of its classification.

The model quotes effective-date evidence and classifies it as one of:
  explicit_operative_date  operative text ties a calendar date to this obligation
  relative_date_formula    e.g. "the first day of the twelfth month following enactment"
  history_note             legislative/codification/amendment history, e.g.
                           "(Added 3-1-2020 by Ord. 1234; effective 4-1-2020.)"
  operative_condition      a non-calendar trigger, e.g. "until ... a portal exists"
The evidence must verify verbatim in the raw source (checked by the caller).
Python, not the model, then decides what the evidence may do:

  explicit_operative_date  the only kind that can populate effective_date, and only
                           when that date appears in the evidence itself.
                           Evidence with the structure of a history note is treated
                           as history_note instead (a guard against a note's word
                           "effective" being read as the obligation's start).
  relative_date_formula    never computed: effective_date null, status undetermined
                           (the record is rejected until a resolver exists).
  history_note             history metadata only: effective_date null, evidence kept.
                           Accepted as history only if the note has history structure
                           or a calendar date; a dated note after as_of rejects the
                           record conservatively (same rule as version_evidence).
  operative_condition      effective_date null; kept as an unresolved operative
                           condition. Status is not changed by it.
Unclassified evidence is rejected. A model-supplied date that its kind may not
carry is discarded with a review warning; it is never used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from navigator.extraction.models import CitationCheck, ExtractedRule, OperativeCondition
from navigator.extraction.review import _MONTHS, calendar_dates

# Amendment verb followed by "by <instrument>" within one note, e.g. "added 3-1-2020 by
# Ord. 1234", "as amended by 2025, 9, Secs. 54 and 55", "Retitled ... by Ord. 1".
_HISTORY = re.compile(r"\b(?:added|amended|retitled|renumbered|repealed|rewritten|inserted|re-?enacted)\b"
                      r"[^()\[\]]{0,160}?\bby\b", re.IGNORECASE)
_NOTE_REACH = 300   # how far back to look for the bracket that opens an enclosing note


@dataclass
class TemporalDecision:
    effective_date: str | None = None
    has_date_evidence: bool = False      # passed to derive_status: True means "dated only in relative terms"
    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    operative: list[OperativeCondition] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)


def enclosing_note(raw: str, start: int, end: int) -> str:
    """The bracketed note that contains raw[start:end], if any, else the span itself."""
    depth = 0
    for i in range(start - 1, max(-1, start - _NOTE_REACH - 1), -1):
        if raw[i] in ")]":
            depth += 1
        elif raw[i] in "([":
            if depth == 0:
                close = min((j for j in (raw.find(")", end), raw.find("]", end)) if j >= 0), default=-1)
                return raw[i:close + 1] if 0 <= close - end <= _NOTE_REACH else raw[start:end]
            depth -= 1
    return raw[start:end]


def looks_like_history_note(text: str) -> bool:
    return _HISTORY.search(text) is not None


def date_in_evidence(effective_date: str, evidence: str) -> bool:
    """Is the (partial) ISO date written in the evidence itself? No computation."""
    dates = calendar_dates(evidence)
    if any(d.isoformat().startswith(effective_date) for d in dates):
        return True
    year = effective_date[:4]
    if len(effective_date) == 4:
        return re.search(rf"\b{year}\b", evidence) is not None
    if len(effective_date) == 7:
        month = _MONTHS[int(effective_date[5:7]) - 1]
        return re.search(rf"\b{month}\b\W{{0,3}}{year}\b", evidence, re.IGNORECASE) is not None
    return False


def resolve_effective_date(rule: ExtractedRule, evidence: CitationCheck | None, raw: str,
                           as_of: date) -> TemporalDecision:
    """Apply the classification rules above. `evidence` is the verified check of
    rule.effective_date_evidence (None when the model gave no evidence)."""
    d = TemporalDecision()
    kind = rule.effective_date_evidence_kind
    d.audit = {"model_effective_date": rule.effective_date, "model_kind": kind, "applied_kind": None,
               "effective_date": None, "history_evidence": None}
    if rule.effective_date_evidence is None:
        if kind is not None:
            d.warnings.append("review: effective_date_evidence_kind given without evidence; ignored")
        if rule.effective_date is not None:
            d.rejections.append("effective_date: not supported by verified verbatim evidence")
        return d
    if evidence is None or evidence.status == "failed":
        d.rejections.append("citation: effective_date_evidence not found in source text")
        if rule.effective_date is not None:
            d.rejections.append("effective_date: not supported by verified verbatim evidence")
        return d
    if kind is None:
        d.rejections.append("temporal: effective_date_evidence is not classified (effective_date_evidence_kind "
                            "is null)")
        return d

    context = enclosing_note(raw, evidence.start, evidence.end)
    applied = kind
    if kind == "explicit_operative_date" and looks_like_history_note(context):
        applied = "history_note"
        d.warnings.append("review: evidence classified as an explicit effective date has the structure of a "
                          "history/amendment note; treated as history")
    d.audit["applied_kind"] = applied
    dropped = rule.effective_date is not None and applied != "explicit_operative_date"

    if applied == "explicit_operative_date":
        if rule.effective_date is None:
            d.rejections.append("temporal: explicit effective-date evidence without an effective_date")
        elif not date_in_evidence(rule.effective_date, evidence.source_span):
            d.rejections.append(f"effective_date: {rule.effective_date} is not written in its verified evidence")
        else:
            d.effective_date, d.has_date_evidence = rule.effective_date, True
    elif applied == "relative_date_formula":
        d.has_date_evidence = True          # derive_status: relative wording is never resolved by guessing
        if dropped:
            d.warnings.append("review: a date computed from a relative formula was discarded")
    elif applied == "history_note":
        if not (looks_like_history_note(context) or calendar_dates(context)):
            d.rejections.append("temporal: evidence classified as a history note has neither history-note "
                                "structure nor a calendar date; the effective date is unresolved")
            return d
        d.audit["history_evidence"] = context
        if dropped:
            d.warnings.append("review: effective_date from a history/amendment note was not used (its relation to "
                              "this obligation is not established); history evidence kept")
        if later := [x for x in calendar_dates(context) if x > as_of]:
            d.rejections.append(f"status: a history note is dated {later[0]}, after as_of {as_of}; the extracted "
                                "wording may not apply yet")
    else:  # operative_condition
        d.operative.append(OperativeCondition(statement=f"Takes effect upon a non-calendar condition: "
                                                        f"{' '.join(evidence.source_span.split())}",
                                              evidence=evidence.source_span))
        if dropped:
            d.warnings.append("review: a date given for an operative (non-calendar) condition was discarded")
    d.audit["effective_date"] = d.effective_date
    return d
