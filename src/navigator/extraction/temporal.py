"""Effective-date evidence: deterministic consequences of its classification.

The model quotes effective-date evidence and classifies it as one of:
  explicit_operative_date  operative text ties a calendar date to this obligation
  relative_date_formula    e.g. "the first day of the twelfth month following enactment"
  history_note             legislative/codification/amendment history, e.g.
                           "(Added 3-1-2020 by Ord. 1234; effective 4-1-2020.)"
  operative_condition      a non-calendar trigger, e.g. "until ... a portal exists"
The evidence must verify verbatim in the raw source (checked by the caller).
Python, not the model, then decides what the evidence may do:

  explicit_operative_date  populates effective_date only when that date appears in
                           the evidence itself. Evidence with the structure of a
                           history note is treated as history_note instead (a guard
                           against a note's word "effective" being read as the
                           obligation's start).
  relative_date_formula    resolved ONLY by the deterministic resolver below; the
                           model's own date is never used. Unresolved: effective_date
                           null and the status undetermined (the record is held).
  history_note             history metadata only: effective_date null, evidence kept.
                           Accepted as history only if the note has history structure
                           or a calendar date. Its dates are classified by the
                           validity window (validity.py): a future repeal/expiry is an
                           end, a future amendment date a later start, and any other
                           future date holds the record.
  operative_condition      effective_date null; kept as an unresolved operative
                           condition. Status is not changed by it.
Unclassified evidence is rejected. A model-supplied date that its kind may not
carry is discarded with a review warning; it is never used. Verified evidence that
matches the resolver grammar is a relative formula whatever the model called it.

Relative-date resolver (RELATIVE_RESOLVER). Deliberately narrow; no general date
interpreter. It resolves a formula only when ALL hold:
  - the formula evidence is verified raw text and matches exactly one instance of
      take effect | become effective | become operative  [on] the first day of the
      <Nth> month next following [the date of] enactment | approval
    (N an ordinal word or number, 1-24; case and whitespace free), and the sentence
    it sits in has no qualification the grammar does not model (another effective
    or operative date, an applicability clause, or a leading "except" /
    "notwithstanding" / "unless" / "subject to");
  - the raw text records exactly ONE distinct base date for that anchor, found by
    find_base_dates: "approved", "enacted" or "signed" [by the Governor] followed
    by a written calendar date (e.g. "Approved June 18, 2021."). An approval date
    serves both anchors; enacted/signed serve "enactment". The base date is always
    raw source text, never a model value.
Then the date is the first day of the Nth calendar month after the base date's month
(June 18, 2021 + 7 -> 2022-01-01). Anything else stays unresolved, with the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from navigator.extraction.models import CitationCheck, ExtractedRule, OperativeCondition
from navigator.extraction.review import _MONTHS, calendar_dates

RELATIVE_RESOLVER = "first-day-of-nth-month-next-following/v1"

# Amendment verb followed by "by <instrument>" within one note, e.g. "added 3-1-2020 by
# Ord. 1234", "as amended by 2025, 9, Secs. 54 and 55", "Retitled ... by Ord. 1".
_HISTORY = re.compile(r"\b(?:added|amended|retitled|renumbered|repealed|rewritten|inserted|re-?enacted)\b"
                      r"[^()\[\]]{0,160}?\bby\b", re.IGNORECASE)
_NOTE_REACH = 300   # how far back to look for the bracket that opens an enclosing note

_ORDINAL_WORDS = ("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth "
                  "fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth").split()
ORDINALS = {w: n for n, w in enumerate(_ORDINAL_WORDS, start=1)}
ORDINALS.update({f"twenty-{w}": 20 + n for w, n in list(ORDINALS.items())[:4]})
_TRIGGER = r"(?:take\s+effect|become\s+effective|become\s+operative)"
_FORMULA = re.compile(_TRIGGER + r"\s+(?:on\s+)?the\s+first\s+day\s+of\s+the\s+(?P<ordinal>[a-z]+(?:-[a-z]+)?|\d{1,2}"
                      r"(?:st|nd|rd|th))\s+month\s+next\s+following\s+(?:the\s+date\s+of\s+)?"
                      r"(?P<anchor>enactment|approval)\b", re.IGNORECASE)
_OTHER_DATE = re.compile(r"\btakes?\s+effect\b|\bbecomes?\s+(?:effective|operative)\b|\beffective\b|\boperative\b"
                         r"|\bappl(?:y|ies|icable)\b", re.IGNORECASE)
_LEADING_QUALIFIER = re.compile(r"\b(?:except|notwithstanding|unless|subject\s+to)\b", re.IGNORECASE)
_BASE_DATE = re.compile(r"\b(?P<event>approved|enacted|signed)\b(?:\s+by\s+(?:the\s+)?governor)?[\s,:]*(?:on\s+)?"
                        r"(?P<date>(?:" + "|".join(_MONTHS) + r")\s+\d{1,2},\s*\d{4})", re.IGNORECASE)
_ANCHOR_EVENTS = {"enactment": {"approved", "enacted", "signed"}, "approval": {"approved"}}
_CONTEXT_REACH = 400


@dataclass
class TemporalDecision:
    effective_date: str | None = None
    has_date_evidence: bool = False      # passed to derive_status: True means "dated only in relative terms"
    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    operative: list[OperativeCondition] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)


def enclosing_note_span(raw: str, start: int, end: int) -> tuple[int, int]:
    """Raw offsets of the bracketed note that contains raw[start:end], if any, else of the span."""
    depth = 0
    for i in range(start - 1, max(-1, start - _NOTE_REACH - 1), -1):
        if raw[i] in ")]":
            depth += 1
        elif raw[i] in "([":
            if depth == 0:
                close = min((j for j in (raw.find(")", end), raw.find("]", end)) if j >= 0), default=-1)
                return (i, close + 1) if 0 <= close - end <= _NOTE_REACH else (start, end)
            depth -= 1
    return start, end


def enclosing_note(raw: str, start: int, end: int) -> str:
    """The bracketed note that contains raw[start:end], if any, else the span itself."""
    s, e = enclosing_note_span(raw, start, end)
    return raw[s:e]


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


def find_base_dates(raw: str) -> list[dict[str, Any]]:
    """Enactment records written in the raw text: event, ISO date and exact raw span."""
    found = []
    for m in _BASE_DATE.finditer(raw):
        dates = calendar_dates(m.group("date"))
        if dates:
            found.append({"event": m.group("event").lower(), "date": dates[0].isoformat(),
                          "start": m.start(), "end": m.end(), "text": m.group(0)})
    return found


def _ordinal(token: str) -> int | None:
    token = token.lower()
    n = int(token[:-2]) if token[:-2].isdigit() else ORDINALS.get(token)
    return n if n is not None and 1 <= n <= 24 else None


def _sentence_context(raw: str, start: int, end: int) -> tuple[str, str]:
    """Text of the same sentence before and after raw[start:end] (bounded)."""
    before = raw[max(0, start - _CONTEXT_REACH):start]
    cut = max(before.rfind(". "), before.rfind(".\n"), before.rfind(".\t"))
    before = before[cut + 1:] if cut >= 0 else before
    after = raw[end:end + _CONTEXT_REACH]
    stop = re.search(r"\.(?:\s|$)", after)
    return before, after[:stop.end()] if stop else after


def resolve_relative(evidence: CitationCheck | None, raw: str, base_dates: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve a relative effective-date formula (module docstring). Always returns the audit;
    audit["resolved_date"] is an ISO date only when outcome == "resolved"."""
    audit: dict[str, Any] = {"resolver": RELATIVE_RESOLVER, "outcome": "unresolved", "reason": None,
                             "formula": None, "formula_evidence": None, "base_date": None,
                             "base_date_evidence": None, "base_date_candidates": [], "resolved_date": None}

    def fail(reason: str) -> dict[str, Any]:
        audit["reason"] = reason
        return audit

    if evidence is None or evidence.status == "failed" or evidence.source_span is None:
        return fail("the formula evidence is not verified source text")
    span = evidence.source_span
    audit["formula_evidence"] = {"text": span, "start": evidence.start, "end": evidence.end}
    matches = list(_FORMULA.finditer(span))
    if len(matches) != 1:
        return fail("the evidence does not state exactly one formula of the supported grammar")
    m = matches[0]
    audit["formula"] = " ".join(m.group(0).split())
    if len(re.findall(_TRIGGER, span, re.IGNORECASE)) != 1:
        return fail("the evidence states more than one effective or operative date")
    n = _ordinal(m.group("ordinal"))
    if n is None:
        return fail(f"unsupported month ordinal {m.group('ordinal')!r}")
    before, after = _sentence_context(raw, evidence.start, evidence.end)
    if _LEADING_QUALIFIER.search(before + span[:m.start()]) or _OTHER_DATE.search(after):
        return fail("the sentence qualifies the formula (another date, an applicability clause or an exception) "
                    "beyond the supported grammar")
    anchor = m.group("anchor").lower()
    candidates = [b for b in base_dates if b["event"] in _ANCHOR_EVENTS[anchor]]
    audit["base_date_candidates"] = candidates
    distinct = sorted({b["date"] for b in candidates})
    if not distinct:
        return fail(f"the document records no date of {anchor} (approved/enacted/signed with a calendar date)")
    if len(distinct) > 1:
        return fail(f"the document records several possible dates of {anchor}: {distinct}")
    base = date.fromisoformat(distinct[0])
    month = base.month - 1 + n
    resolved = date(base.year + month // 12, month % 12 + 1, 1)
    audit.update(outcome="resolved", base_date=base.isoformat(), base_date_evidence=candidates[0],
                 resolved_date=resolved.isoformat(), ordinal=n, anchor=anchor)
    return audit


def resolve_effective_date(rule: ExtractedRule, evidence: CitationCheck | None, raw: str,
                           as_of: date, base_dates: list[dict[str, Any]] = ()) -> TemporalDecision:
    """Apply the classification rules above. `evidence` is the verified check of
    rule.effective_date_evidence (None when the model gave no evidence); `base_dates`
    are the document's enactment records (find_base_dates)."""
    d = TemporalDecision()
    kind = rule.effective_date_evidence_kind
    d.audit = {"model_effective_date": rule.effective_date, "model_kind": kind, "applied_kind": None,
               "effective_date": None, "history_evidence": None, "relative_resolution": None}
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
    if _FORMULA.search(evidence.source_span or "") and kind != "relative_date_formula":
        d.warnings.append(f"review: effective-date evidence classified as {kind} states a relative formula of the "
                          "resolver grammar; treated as relative_date_formula")
        kind = "relative_date_formula"
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
        d.has_date_evidence = True          # unresolved -> derive_status leaves the status undetermined
        resolution = d.audit["relative_resolution"] = resolve_relative(evidence, raw, list(base_dates))
        if resolution["outcome"] == "resolved":
            d.effective_date = resolution["resolved_date"]
            if rule.effective_date is not None and rule.effective_date != d.effective_date:
                d.warnings.append(f"review: the model's date {rule.effective_date} differs from the deterministic "
                                  f"resolution {d.effective_date}; not used")
        elif dropped:
            d.warnings.append("review: a date computed by the model from a relative formula was discarded")
    elif applied == "history_note":
        if not (looks_like_history_note(context) or calendar_dates(context)):
            d.rejections.append("temporal: evidence classified as a history note has neither history-note "
                                "structure nor a calendar date; the effective date is unresolved")
            return d
        d.audit["history_evidence"] = context
        if dropped:
            d.warnings.append("review: effective_date from a history/amendment note was not used (its relation to "
                              "this obligation is not established); history evidence kept")
    else:  # operative_condition
        d.operative.append(OperativeCondition(statement=f"Takes effect upon a non-calendar condition: "
                                                        f"{' '.join(evidence.source_span.split())}",
                                              evidence=evidence.source_span))
        if dropped:
            d.warnings.append("review: a date given for an operative (non-calendar) condition was discarded")
    d.audit["effective_date"] = d.effective_date
    return d
