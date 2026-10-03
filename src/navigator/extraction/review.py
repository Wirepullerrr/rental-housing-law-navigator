"""Deterministic, general-purpose review checks.

These produce WARNINGS only: they never accept, reject or alter a record. They
point a human reviewer at likely extraction problems.

Implemented (general, not statute-specific):
- unsupported_figures: monetary amounts, percentages, durations and calendar dates
  stated in a claim (`requirement`, `key_value`) but absent from the quoted_span.
  English number words and "per cent" are normalized first, so "30 days" matches
  "thirty days" and "5%" matches "five per cent".
- uncovered_provisions: provisions the model marked in scope in its own inventory
  that no extracted record cites (ranges such as "(a)-(c)" expanded).
- ref_matches: the structural provision matcher also used to propagate
  document-level scope conditions (token sequences, never similarity).

Deliberately NOT implemented (brittle or statute-specific):
- entailment of non-numeric claims (e.g. "key and lock costs" outside the quote);
- keyword detection of version history misfiled as conflict_note;
- validating citation strings against a document's numbering scheme.
Known false positives: fractions in words ("one and one-half"), ordinals
("first month's rent" vs "1 month's rent"), and inventory refs written in a
different style from the citation.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

_SMALL = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)}
_WORD = "|".join(sorted([*_SMALL, *_TENS, "hundred", "thousand"], key=len, reverse=True))
_NUMBER_PHRASE = re.compile(rf"\b(?:{_WORD})(?:[\s-]+(?:{_WORD}))*\b", re.IGNORECASE)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]
_FIGURE = re.compile(
    r"(?P<money>\$\s?\d[\d,]*(?:\.\d+)?)"
    r"|(?P<date>\b(?:" + "|".join(_MONTHS) + r")\s+\d{1,2},\s*\d{4}|\b\d{4}-\d{2}-\d{2}\b)"
    r"|(?P<pct>\b\d+(?:\.\d+)?)\s?%"
    r"|(?P<num>\b\d+(?:\.\d+)?)[\s-]*(?:business\s+|calendar\s+)?(?P<unit>hour|day|week|month|year)s?\b",
    re.IGNORECASE)


def _phrase_value(phrase: str) -> int:
    total = current = 0
    for word in re.split(r"[\s-]+", phrase.lower()):
        if word in _SMALL:
            current += _SMALL[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word == "thousand":
            total, current = total + (current or 1) * 1000, 0
    return total + current


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\bper\s*cent\b|\bpercent\b", "%", text, flags=re.IGNORECASE)
    text = _NUMBER_PHRASE.sub(lambda m: str(_phrase_value(m.group(0))), text)
    return re.sub(r"\b(\d+)\s*\(\s*\1\s*\)", r"\1", text)  # drafting style "ten (10) days"


def _number(value: str) -> str:
    value = value.replace(",", "")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _to_iso(text: str) -> str | None:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    month, day, year = re.match(r"(\w+)\s+(\d{1,2}),\s*(\d{4})", text).groups()
    try:
        return date(int(year), _MONTHS.index(month.lower()) + 1, int(day)).isoformat()
    except ValueError:
        return None


def _figures(text: str) -> dict[tuple[str, str], str]:
    """Canonical figure -> its text as written (after normalization)."""
    found: dict[tuple[str, str], str] = {}
    for m in _FIGURE.finditer(_normalize(text)):
        if m.group("money"):
            key = ("$", _number(m.group("money").lstrip("$").strip()))
        elif m.group("date"):
            iso = _to_iso(m.group("date"))
            if iso is None:
                continue
            key = ("date", iso)
        elif m.group("pct"):
            key = ("%", _number(m.group("pct")))
        else:
            key = (m.group("unit").lower(), _number(m.group("num")))
        found.setdefault(key, m.group(0).strip())
    return found


def unsupported_figures(claim: str, quoted_span: str) -> list[str]:
    """Figures stated in `claim` that do not appear in `quoted_span`."""
    supported = _figures(quoted_span)
    return [shown for key, shown in _figures(claim).items() if key not in supported]


def calendar_dates(text: str) -> list[date]:
    """Explicit calendar dates in text ('August 1, 2025' or '2025-08-01')."""
    return [date.fromisoformat(value) for kind, value in _figures(text) if kind == "date"]


_REF_NOISE = {"section", "sec", "subsection", "paragraph", "clause"}
_RANGE = re.compile(r"\(([a-z]|\d+)\)\s*[-–]\s*\(([a-z]|\d+)\)", re.IGNORECASE)


def _ref_tokens(text: str) -> list[str]:
    """'§ 15B(2)(a)' -> ['15b', '2', 'a']; '4.a' -> ['4', 'a']."""
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _REF_NOISE]


def _expand_ranges(text: str) -> list[str]:
    """'§ 98.0709(a)-(c)' -> ['§ 98.0709(a)', '§ 98.0709(b)', '§ 98.0709(c)'] (single letters or numbers)."""
    m = _RANGE.search(text)
    if not m:
        return [text]
    a, b = m.group(1), m.group(2)
    if a.isdigit() and b.isdigit() and 0 <= int(b) - int(a) <= 50:
        items = [str(n) for n in range(int(a), int(b) + 1)]
    elif a.isalpha() and b.isalpha() and a.islower() == b.islower() and a <= b:
        items = [chr(c) for c in range(ord(a), ord(b) + 1)]
    else:
        items = [a, b]
    return [x for item in items for x in _expand_ranges(text[:m.start()] + f"({item})" + text[m.end():])]


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def ref_matches(ref: str, citation: str) -> bool:
    """Structural match: does `citation` name provision `ref` (or a subdivision of it)?
    Token sequences with ranges expanded on both sides (a range ref matches if any of its
    elements matches); descriptive text after a comma in `ref` is ignored. No similarity
    scoring. '§ 98.0706' matches '§ 98.0706(c)(1)'; '§ 98.0706(c)' does not match '§ 98.0706'."""
    needles = [_ref_tokens(r) for r in _expand_ranges(ref.split(",")[0])]
    hays = [_ref_tokens(c) for c in _expand_ranges(citation)]
    return any(_contains_run(h, n) for n in needles for h in hays)


def uncovered_provisions(inventory: list[dict[str, Any]], citations: list[str]) -> list[str]:
    """In-scope inventory refs (ranges expanded) that no citation names."""
    return [ref for p in inventory if p.get("category") for ref in _expand_ranges(p["ref"].split(",")[0])
            if not any(ref_matches(ref, c) for c in citations)]
