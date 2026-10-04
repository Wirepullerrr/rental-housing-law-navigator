"""Verify that a quoted_span really occurs in the source text.

Three outcomes count as verified, tried in this order:
  exact_match              the span is a substring of the source body, byte for byte.
  normalized_match         it matches after SAFE normalization only: Unicode NFC, and
                           every run of whitespace (spaces, tabs, line breaks) treated
                           as one space, with the span's ends trimmed. Words,
                           punctuation and quote characters must still match exactly.
  layout_normalized_match  (M3.3) as normalized_match, and additionally whitespace
                           immediately before or after ( ) [ ] is ignored on BOTH
                           sides, so "( Amended" matches "(Amended" and a line break
                           after "[" matches none. Nothing else changes: no other space
                           between words may differ, and no character other than that
                           whitespace may be added, dropped or changed.
Anything else is `failed`. There is no fuzzy or edit-distance matching, and the model's
quote is never edited to make it match: a verified check always carries the source's own
text at the located raw offsets, which is what gets published.
"""

from __future__ import annotations

import unicodedata

from navigator.extraction.models import CitationCheck

NORMALIZATION = "Unicode NFC; each whitespace run (incl. line breaks) compared as one space; span ends trimmed"
LAYOUT_NORMALIZATION = NORMALIZATION + "; whitespace immediately adjacent to ( ) [ ] ignored (layout only)"
_BRACKETS = "()[]"


def _collapse_with_map(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to one space; map each output char to its source index."""
    out: list[str] = []
    index: list[int] = []
    in_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not in_space:
                out.append(" ")
                index.append(i)
            in_space = True
        else:
            out.append(ch)
            index.append(i)
            in_space = False
    return "".join(out), index


def _layout_with_map(text: str) -> tuple[str, list[int]]:
    """_collapse_with_map, then drop each space immediately before or after ( ) [ ]."""
    collapsed, index = _collapse_with_map(text)
    out: list[str] = []
    kept: list[int] = []
    for i, ch in enumerate(collapsed):
        if ch == " " and ((i > 0 and collapsed[i - 1] in _BRACKETS)
                          or (i + 1 < len(collapsed) and collapsed[i + 1] in _BRACKETS)):
            continue
        out.append(ch)
        kept.append(index[i])
    return "".join(out), kept


def verify_span(span: str, source: str) -> CitationCheck:
    if not span or not span.strip():
        return CitationCheck(status="failed", model_span=span, reason="empty quoted_span")

    start = source.find(span)
    if start >= 0:
        return CitationCheck(status="exact_match", start=start, end=start + len(span),
                             occurrences=source.count(span), model_span=span, source_span=span)

    nfc_source = unicodedata.normalize("NFC", source)
    norm_source, index = _collapse_with_map(nfc_source)
    norm_span, _ = _collapse_with_map(unicodedata.normalize("NFC", span).strip())
    pos = norm_source.find(norm_span)
    if pos >= 0:
        s, e = index[pos], index[pos + len(norm_span) - 1] + 1
        return CitationCheck(status="normalized_match", normalization=NORMALIZATION, start=s, end=e,
                             occurrences=norm_source.count(norm_span), model_span=span,
                             source_span=nfc_source[s:e])
    lay_source, lay_index = _layout_with_map(nfc_source)
    lay_span = _layout_with_map(unicodedata.normalize("NFC", span).strip())[0].strip()
    pos = lay_source.find(lay_span) if lay_span else -1
    if pos < 0:
        return CitationCheck(status="failed", model_span=span,
                             reason="not found in source text, even after safe normalization")
    s, e = lay_index[pos], lay_index[pos + len(lay_span) - 1] + 1
    return CitationCheck(status="layout_normalized_match", normalization=LAYOUT_NORMALIZATION, start=s, end=e,
                         occurrences=lay_source.count(lay_span), model_span=span, source_span=nfc_source[s:e])


def match_anchored(span: str, text: str, *, at_end: bool) -> tuple[str, int, int] | None:
    """Locate `span` flush against the end (at_end=True) or the start of `text`: only
    whitespace may lie between the match and that edge. Same policy as verify_span
    (exact, else NFC + whitespace runs, else also layout whitespace next to brackets).
    Returns (status, start, end) offsets into `text`."""
    if not span or not span.strip():
        return None
    pos = text.rfind(span) if at_end else text.find(span)
    if pos >= 0 and not (text[pos + len(span):] if at_end else text[:pos]).strip():
        return "exact_match", pos, pos + len(span)
    for status, fold in (("normalized_match", _collapse_with_map), ("layout_normalized_match", _layout_with_map)):
        norm_text, index = fold(unicodedata.normalize("NFC", text))
        norm_span = fold(unicodedata.normalize("NFC", span).strip())[0].strip()
        if not norm_span:
            return None
        pos = norm_text.rfind(norm_span) if at_end else norm_text.find(norm_span)
        if pos >= 0 and not (norm_text[pos + len(norm_span):] if at_end else norm_text[:pos]).strip():
            return status, index[pos], index[pos + len(norm_span) - 1] + 1
    return None
