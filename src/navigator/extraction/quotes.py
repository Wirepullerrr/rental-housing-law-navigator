"""Quote verification against the RAW source, including cross-page reconstruction.

A record's support is given as quote parts, each naming the view segment it is
copied from (source_view.py).

One part (normal case): the unchanged single-span citation policy. The text
must be found verbatim (exact, or after the safe whitespace/NFC normalization
of citation.py) in the raw source, preferably inside the segment it names. A
match elsewhere is accepted with a review warning; a match that overlaps
removed page furniture is rejected.

Two parts (cross-page case), accepted only if ALL hold:
  - the segments are adjacent (n, n+1), in source order;
  - exactly one detected page artifact lies between them;
  - part 1 is verbatim text flush against the END of segment n and part 2 is
    verbatim text flush against the START of segment n+1, so nothing but
    whitespace and that one artifact lies between them (no legal text skipped).
The published span is then raw[start of part 1 : end of part 2]: an exact,
contiguous substring of the untouched source that contains the page artifact,
which is recorded separately. More than two parts are always rejected.

Nothing here edits the model's text, fills gaps or matches fuzzily.
"""

from __future__ import annotations

from collections.abc import Sequence

from navigator.extraction.citation import LAYOUT_NORMALIZATION, NORMALIZATION, match_anchored, verify_span
from navigator.extraction.models import CitationCheck, QuotePart
from navigator.extraction.source_view import MARKER_PATTERN, PAGE_BREAK_MARKER, Segment, SourceView

MAX_PARTS = 2
RECONSTRUCTION = "raw text from the start of part 1 through the end of part 2, across exactly one page artifact"


def verify_text(span: str, raw: str, view: SourceView) -> CitationCheck:
    """One verbatim passage, checked against the RAW source. A failure is diagnosed, never repaired."""
    check = verify_span(span, raw)
    if check.status == "failed":
        flat = " ".join(span.split())
        if MARKER_PATTERN.search(span):
            check.reason = "quote includes a view marker ([[PAGE BREAK]] or [[SEGMENT n]]), which is not source text"
        elif flat and flat in " ".join(view.without_markers().split()):
            check.reason = ("quote joins text across a removed page artifact; a cross-page passage needs two "
                            "quote parts")
    elif view.artifacts_between(check.start, check.end):
        return CitationCheck(status="failed", model_span=span, reason="quote contains removed page furniture")
    return check


def verify_quote_parts(parts: Sequence[QuotePart], raw: str, view: SourceView) -> tuple[CitationCheck, list[str]]:
    """Return the overall check (source_span is what gets published) and review warnings."""
    if len(parts) == 1:
        return _single(parts[0], raw, view)
    model_span = f"\n{PAGE_BREAK_MARKER}\n".join(p.quoted_text for p in parts)
    if len(parts) > MAX_PARTS:
        return _failed(model_span, parts, f"{len(parts)} quote parts; at most two (one page break) are allowed"), []
    return _cross_page(parts[0], parts[1], model_span, raw, view), []


def _part_record(p: QuotePart, status: str = "failed", start: int | None = None, end: int | None = None) -> dict:
    return {"segment_id": p.segment_id, "model_text": p.quoted_text, "status": status, "start": start, "end": end}


def _failed(model_span: str, parts: Sequence[QuotePart], reason: str) -> CitationCheck:
    return CitationCheck(status="failed", model_span=model_span, reason=reason,
                         parts=[_part_record(p) for p in parts])


def _single(part: QuotePart, raw: str, view: SourceView) -> tuple[CitationCheck, list[str]]:
    seg = view.segment(part.segment_id)
    if seg is not None:
        local = verify_span(part.quoted_text, raw[seg.raw_start:seg.raw_end])
        if local.status != "failed":
            start, end = seg.raw_start + local.start, seg.raw_start + local.end
            check = local.model_copy(update={"start": start, "end": end, "source_span": raw[start:end]})
            check.parts = [_part_record(part, check.status, start, end)]
            return check, []
    check = verify_text(part.quoted_text, raw, view)
    warnings = []
    if check.status != "failed":
        actual = view.segment_containing(check.start, check.end)
        warnings.append(f"review: quote part names segment {part.segment_id}, but the text is in segment "
                        f"{actual.id if actual else '?'}")
    check.parts = [_part_record(part, check.status, check.start, check.end)]
    return check, warnings


def _cross_page(p1: QuotePart, p2: QuotePart, model_span: str, raw: str, view: SourceView) -> CitationCheck:
    s1, s2 = view.segment(p1.segment_id), view.segment(p2.segment_id)
    if s1 is None or s2 is None:
        return _failed(model_span, (p1, p2), "a quote part names a segment that does not exist")
    if s2.id != s1.id + 1:
        return _failed(model_span, (p1, p2), f"quote parts are from segments {s1.id} and {s2.id}; two parts must "
                                             "come from adjacent segments n and n+1, in source order")
    crossed = view.artifacts_between(s1.raw_end, s2.raw_start)
    if len(crossed) != 1:
        return _failed(model_span, (p1, p2), f"quote parts cross {len(crossed)} page artifacts; exactly one is allowed")
    m1 = match_anchored(p1.quoted_text, raw[s1.raw_start:s1.raw_end], at_end=True)
    if m1 is None:
        return _failed(model_span, (p1, p2), _why_not_anchored(p1, s1, raw, "end"))
    m2 = match_anchored(p2.quoted_text, raw[s2.raw_start:s2.raw_end], at_end=False)
    if m2 is None:
        return _failed(model_span, (p1, p2), _why_not_anchored(p2, s2, raw, "start"))

    start, end = s1.raw_start + m1[1], s2.raw_start + m2[2]
    span = raw[start:end]
    gap_before, gap_after = raw[s1.raw_start + m1[2]:crossed[0].raw_start], raw[crossed[0].raw_end:s2.raw_start + m2[1]]
    if gap_before.strip() or gap_after.strip():   # guaranteed by anchoring; checked again, never assumed
        return _failed(model_span, (p1, p2), "text other than the page artifact lies between the quote parts")
    exact = m1[0] == m2[0] == "exact_match"
    layout = "layout_normalized_match" in (m1[0], m2[0])
    status = "exact_match" if exact else "layout_normalized_match" if layout else "normalized_match"
    return CitationCheck(
        status=status,
        normalization=RECONSTRUCTION + ("" if exact else
                                        f"; parts matched with: {LAYOUT_NORMALIZATION if layout else NORMALIZATION}"),
        start=start, end=end, occurrences=raw.count(span), model_span=model_span, source_span=span,
        reconstructed=True,
        parts=[_part_record(p1, m1[0], s1.raw_start + m1[1], s1.raw_start + m1[2]),
               _part_record(p2, m2[0], s2.raw_start + m2[1], s2.raw_start + m2[2])],
        crossed_artifacts=[{"raw_start": a.raw_start, "raw_end": a.raw_end, "text": a.text} for a in crossed])


def _why_not_anchored(part: QuotePart, seg: Segment, raw: str, edge: str) -> str:
    which = "part 1" if edge == "end" else "part 2"
    if verify_span(part.quoted_text, raw[seg.raw_start:seg.raw_end]).status != "failed":
        return (f"{which} is in segment {seg.id} but not at its {edge}: legal text between the quote and the "
                "page break would be skipped")
    if verify_span(part.quoted_text, raw).status != "failed":
        return f"{which} is not in segment {seg.id}"
    return f"{which} not found in source text"
