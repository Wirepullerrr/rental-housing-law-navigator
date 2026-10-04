"""Validity windows (M3.3): when the CURRENT text of a rule starts and stops applying.

Internal temporal semantics, kept in the audit only (the official RuleRecord is unchanged):

  effective_from         "effective DATE", "becomes effective on DATE"        start
  operative_from         "shall become operative on DATE"                     start
  amendment_history      "Effective DATE" inside an amendment/codification
                         note ("(Amended by Stats. ... Effective DATE ...)")  start of the CURRENT wording
  repealed_on            "repealed as of DATE"; "remain in effect only until
                         DATE, and as of that date is repealed"               end: not in force from DATE
  expires_on             "expires on DATE"; "remain(s) in effect until DATE"  end: DATE itself is ambiguous
  valid_through          "effective DATE through DATE", "for the period of
                         DATE through DATE", "valid through DATE",
                         "effective through DATE"                             end: in force through DATE
  superseded_on          reserved: no deterministic grammar; never inferred
  unknown_temporal_note  a written calendar date in verified temporal
                         evidence that none of the above explains

Deliberately narrow (VALIDITY_RESOLVER); not a natural-language date interpreter. DATE is
a written calendar date ("January 1, 2030"). Only VERIFIED evidence is read: the
record's quoted_span, its version_evidence, its effective_date_evidence and its
operative-condition evidence, each already located in the raw text, so every boundary
keeps exact raw offsets.

decide() for an enacted record, given the status derived from its start:
  - distinct end boundaries that disagree, or a query date falling ON an ambiguous end
    day: the end cannot be established safely -> held (review_required), never guessed;
  - an end on or before as_of -> HISTORICAL (temporal_state expired, or repealed):
    validated and kept in the audit with its evidence, never published as current;
  - an end after as_of -> the current text applies until then. A future repeal or expiry
    is NOT a future start and never rejects the record;
  - an amendment_history date after as_of in version/history evidence -> the extracted
    wording is a later version: not_yet_effective from that date;
  - any other date after as_of in version/history evidence -> held (it is not
    classified, so nothing is guessed). The same date in the quoted rule text is only
    recorded.
A future effective_from / operative_from keeps its existing meaning through the
effective-date logic (temporal.py): not_yet_effective.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from navigator.extraction.models import CitationCheck
from navigator.extraction.review import calendar_dates
from navigator.extraction.temporal import looks_like_history_note

VALIDITY_RESOLVER = "validity-window/v1"
START_KINDS = ("effective_from", "operative_from", "amendment_history")
END_KINDS = ("repealed_on", "expires_on", "valid_through", "superseded_on")

_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"


def _d(name: str) -> str:
    return rf"(?P<{name}>{_MONTH}\s+\d{{1,2}},\s*\d{{4}})"


_THROUGH = r",?\s+(?:through|thru)\s+"
# (kind, pattern). Earlier patterns win: a date already classified is not classified again.
_PATTERNS = [
    ("repealed_on", re.compile(r"\bremains?\s+in\s+effect\s+only\s+until\s+" + _d("end")
                               + r",?\s+and\s+as\s+of\s+that\s+date\s+is\s+repealed", re.IGNORECASE)),
    ("repealed_on", re.compile(r"\brepealed\s+(?:as\s+of|on)\s+" + _d("end"), re.IGNORECASE)),
    ("expires_on", re.compile(r"\bexpires?\s+on\s+" + _d("end"), re.IGNORECASE)),
    ("expires_on", re.compile(r"\b(?:remains?|shall\s+remain|is|are)\s+in\s+effect\s+(?:only\s+)?until\s+"
                              + _d("end"), re.IGNORECASE)),
    ("valid_through", re.compile(r"\b(?:effective|valid|in\s+effect)\s+(?:from\s+|on\s+)?" + _d("start") + _THROUGH
                                 + _d("end"), re.IGNORECASE)),
    ("valid_through", re.compile(r"\bfor\s+the\s+period\s+(?:of\s+|from\s+)?" + _d("start") + _THROUGH + _d("end"),
                                 re.IGNORECASE)),
    ("valid_through", re.compile(r"\b(?:effective|valid|in\s+effect)\s+through\s+" + _d("end"), re.IGNORECASE)),
    ("operative_from", re.compile(r"\bbecomes?\s+operative\s+(?:on|as\s+of)\s+" + _d("start"), re.IGNORECASE)),
    ("effective_from", re.compile(r"\b(?:effective|takes?\s+effect|becomes?\s+effective)\s+(?:on\s+|as\s+of\s+)?"
                                  + _d("start"), re.IGNORECASE)),
]


def _iso(text: str) -> date | None:
    found = calendar_dates(text)
    return found[0] if found else None


def scan(origin: str, check: CitationCheck, history: bool = False) -> list[dict[str, Any]]:
    """Every boundary in one verified passage, with raw offsets. `history`: the passage is
    version/history evidence, so a bare "Effective DATE" dates the current wording."""
    text, base = check.source_span or "", check.start or 0
    dated_wording = history or looks_like_history_note(text)   # "Effective DATE" dates the wording itself
    taken: list[tuple[int, int]] = []
    found: list[dict[str, Any]] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            groups = [g for g in ("start", "end") if g in pattern.groupindex and m.group(g)]
            if any(m.start(g) < e and s < m.end(g) for g in groups for s, e in taken):
                continue
            for g in groups:
                when = _iso(m.group(g))
                if when is None:
                    continue
                taken.append((m.start(g), m.end(g)))
                role = "end" if g == "end" else "start"
                k = kind if role == "end" else ("amendment_history" if dated_wording and kind == "effective_from"
                                                else "effective_from" if kind == "valid_through" else kind)
                entry = {"kind": k, "role": role, "date": when.isoformat(), "origin": origin, "history": history,
                         "evidence": " ".join(m.group(0).split()), "raw_start": base + m.start(),
                         "raw_end": base + m.end()}
                if role == "end":
                    # end_exclusive: the first day the rule no longer applies. "repealed as of D"
                    # excludes D; "through D" includes D; for "expires on D" / "in effect until D"
                    # the day D itself is ambiguous (decide() holds a query on that day).
                    last_day = when if k == "repealed_on" else when + timedelta(days=1)
                    entry["end_exclusive"] = last_day.isoformat()
                    entry["ambiguous_day"] = when.isoformat() if k == "expires_on" else None
                found.append(entry)
    classified = {date.fromisoformat(b["date"]) for b in found}
    for when in calendar_dates(text):
        if when not in classified:
            found.append({"kind": "unknown_temporal_note", "role": "unknown", "date": when.isoformat(),
                          "origin": origin, "history": history, "evidence": None, "raw_start": None, "raw_end": None})
    return found


def resolve_window(pieces: list[tuple[str, CitationCheck, bool]]) -> dict[str, Any]:
    """Boundaries from every verified passage: (origin, check, is_history_evidence)."""
    boundaries = [b for origin, check, history in pieces
                  if check is not None and check.status != "failed" and check.source_span
                  for b in scan(origin, check, history)]
    ends = sorted({b["end_exclusive"] for b in boundaries if b["role"] == "end"})
    return {"resolver": VALIDITY_RESOLVER, "boundaries": boundaries, "end_exclusive": ends[0] if len(ends) == 1 else None,
            "end_conflict": ends if len(ends) > 1 else None}


def decide(window: dict[str, Any], as_of: date) -> dict[str, Any]:
    """The window's consequence for an enacted record (module docstring). Returns
    {"outcome": None | "historical" | "hold" | "not_yet_effective", "state", "reason", "start"}."""
    out: dict[str, Any] = {"outcome": None, "state": None, "reason": None, "start": None}
    bounds = window["boundaries"]
    if window["end_conflict"]:
        out.update(outcome="hold", reason=f"the verified evidence states different end boundaries "
                                          f"{window['end_conflict']}; the end of validity cannot be established")
        return out
    history_future = [b for b in bounds if b["history"] and date.fromisoformat(b["date"]) > as_of]
    unknown = [b for b in history_future if b["role"] == "unknown"]
    if unknown:
        out.update(outcome="hold", reason=f"version/history evidence has a date after as_of {as_of} that the "
                                          f"validity grammar does not classify ({unknown[0]['date']})")
        return out
    end = window["end_exclusive"]
    if end is not None:
        ends = [b for b in bounds if b["role"] == "end"]
        if any(b["ambiguous_day"] == as_of.isoformat() for b in ends):
            out.update(outcome="hold", reason=f"as_of {as_of} is the stated end day of an 'until' / 'expires on' "
                                              "boundary, which may or may not include that day")
            return out
        if date.fromisoformat(end) <= as_of:
            kind = "repealed" if all(b["kind"] == "repealed_on" for b in ends) else "expired"
            out.update(outcome="historical", state=kind,
                       reason=f"its verified validity ended before {end} (first day not in force), on or "
                              f"before as_of {as_of}: {ends[0]['evidence']!r}")
            return out
        out["state"] = "current_until"
        out["reason"] = f"in force until {end} (first day not in force), after as_of {as_of}"
    later = [b for b in history_future if b["kind"] == "amendment_history"]
    if later:
        start = max(b["date"] for b in later)
        out.update(outcome="not_yet_effective", start=start,
                   reason=f"the extracted wording is a version effective {start}, after as_of {as_of}")
    return out
