"""Canonical extraction view: the raw source with repeated page furniture marked.

PDF-derived text carries running headers/footers in the middle of legal text.
The model then quotes "around" them and its quote is no longer a contiguous
source substring. This module detects such artifacts STRUCTURALLY, with no
document-specific or legal knowledge, and replaces each with a visible marker
in the text given to the model, so it can keep quotes within one segment.

Detection (conservative; every condition must hold):
  - a line's key is its whitespace-collapsed text with digit runs replaced by
    "#";
  - a candidate block is a maximal run of consecutive lines that are blank or
    whose key occurs at least MIN_REPEATS times, with at least MIN_BLOCK_LINES
    non-blank lines;
  - the block's sequence of keys recurs at least MIN_REPEATS times;
  - PAGE COUNTER: across those occurrences, in document order, the text is
    identical except for numbers that strictly increase (page numbers);
  - occurrences are at least MIN_GAP_LINES non-blank lines apart (a page of
    text, not adjacent table rows).
A single repeated line never qualifies. Identical repeats (a title quoted
three times, bill-history rows) and numbers that do not count upwards (dates,
regulation numbers) are left untouched. Removed text is recorded verbatim with
its raw offsets.

The text between artifacts is split into numbered segments, each introduced by
a [[SEGMENT n]] line, so quotes can name the segment they come from (quotes.py).
Segments and artifacts tile the raw text exactly.

The raw source is never modified: citations are still verified against it,
exactly as before. The view only changes what the model reads.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

PAGE_BREAK_MARKER = "[[PAGE BREAK]]"
SEGMENT_LABEL = "[[SEGMENT {}]]"
MARKER_PATTERN = re.compile(r"\[\[(?:PAGE BREAK|SEGMENT \d+)\]\]")
MIN_REPEATS = 3
MIN_BLOCK_LINES = 2
MIN_GAP_LINES = 8


@dataclass(frozen=True)
class Artifact:
    raw_start: int
    raw_end: int
    text: str


@dataclass(frozen=True)
class Segment:
    id: int          # 1-based, as labelled in the view
    view_start: int
    raw_start: int
    length: int

    @property
    def raw_end(self) -> int:
        return self.raw_start + self.length


@dataclass(frozen=True)
class SourceView:
    text: str
    artifacts: tuple[Artifact, ...]
    segments: tuple[Segment, ...]

    def to_raw(self, view_offset: int) -> int | None:
        """Raw offset for a view offset, or None if it falls inside a marker or label."""
        for s in self.segments:
            if s.view_start <= view_offset < s.view_start + s.length:
                return s.raw_start + (view_offset - s.view_start)
        return None

    def segment(self, seg_id: int) -> Segment | None:
        return self.segments[seg_id - 1] if 1 <= seg_id <= len(self.segments) else None

    def segment_containing(self, raw_start: int, raw_end: int) -> Segment | None:
        return next((s for s in self.segments if s.raw_start <= raw_start and raw_end <= s.raw_end), None)

    def artifacts_between(self, raw_start: int, raw_end: int) -> list[Artifact]:
        """Artifacts overlapping the raw range [raw_start, raw_end)."""
        return [a for a in self.artifacts if a.raw_start < raw_end and raw_start < a.raw_end]

    def without_markers(self) -> str:
        """Segments joined with the markers dropped (diagnostics only, never for acceptance)."""
        return " ".join(self.text[s.view_start:s.view_start + s.length] for s in self.segments)


def _key(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.split()))


def _artifact_line_spans(lines: list[str]) -> list[tuple[int, int]]:
    """[start, end) line indexes of page-furniture blocks."""
    keys = [_key(line) for line in lines]
    counts = Counter(k for k in keys if k)
    repeated = {k for k, n in counts.items() if n >= MIN_REPEATS}
    blocks: list[list[int]] = []          # non-blank line indexes of each candidate run
    i = 0
    while i < len(lines):
        if keys[i] not in repeated:
            i += 1
            continue
        j = i
        while j < len(lines) and (not keys[j] or keys[j] in repeated):
            j += 1
        run = [k for k in range(i, j) if keys[k]]
        if len(run) >= MIN_BLOCK_LINES:
            blocks.append(run)
        i = j

    def signature(run: list[int]) -> tuple[str, ...]:
        return tuple(keys[k] for k in run)

    groups: dict[tuple[str, ...], list[tuple[int, int]]] = defaultdict(list)
    for run in blocks:
        groups[signature(run)].append((run[0], run[-1] + 1))
    accepted = {sig: occ for sig, occ in groups.items() if _is_page_furniture(lines, keys, occ)}

    # Second pass: a qualifying header can sit inside a longer run because an adjacent
    # legal line also repeats. Take only the exact sub-run, and only if the extended
    # group still passes every page-furniture test; the adjacent line stays in the text.
    for run in blocks:
        if signature(run) in accepted:
            continue
        for sig in list(accepted):
            n = len(sig)
            hit = next((p for p in range(len(run) - n + 1) if signature(run[p:p + n]) == sig), None)
            if hit is not None:
                extended = sorted(accepted[sig] + [(run[hit], run[hit + n - 1] + 1)])
                if _is_page_furniture(lines, keys, extended):
                    accepted[sig] = extended
                break
    return sorted(span for occ in accepted.values() for span in occ)


def _is_page_furniture(lines: list[str], keys: list[str], occurrences: list[tuple[int, int]]) -> bool:
    if len(occurrences) < MIN_REPEATS:
        return False
    numbers = [[int(n) for n in re.findall(r"\d+", "".join(lines[s:e]))] for s, e in occurrences]
    if len({len(n) for n in numbers}) != 1:
        return False
    varying = [p for p in range(len(numbers[0])) if len({n[p] for n in numbers}) > 1]
    if not varying:
        return False                      # identical repeats are not provably page furniture
    if not all(a[p] < b[p] for a, b in zip(numbers, numbers[1:]) for p in varying):
        return False                      # varying numbers must count upwards like page numbers
    gaps = [sum(1 for k in keys[prev_end:start] if k)
            for (_, prev_end), (start, _) in zip(occurrences, occurrences[1:])]
    return all(g >= MIN_GAP_LINES for g in gaps)


def build_view(raw: str) -> SourceView:
    lines = raw.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    spans = _artifact_line_spans(lines)

    parts: list[str] = []
    artifacts: list[Artifact] = []
    segments: list[Segment] = []
    view_len, raw_pos = 0, 0

    def keep(raw_start: int, raw_end: int) -> None:
        nonlocal view_len
        if raw_end > raw_start:
            label = SEGMENT_LABEL.format(len(segments) + 1) + "\n"
            parts.append(label)
            view_len += len(label)
            segments.append(Segment(len(segments) + 1, view_len, raw_start, raw_end - raw_start))
            parts.append(raw[raw_start:raw_end])
            view_len += raw_end - raw_start

    for s, e in spans:
        a_start, a_end = starts[s], starts[e]
        keep(raw_pos, a_start)
        artifacts.append(Artifact(a_start, a_end, raw[a_start:a_end]))
        marker = PAGE_BREAK_MARKER + "\n"
        parts.append(marker)
        view_len += len(marker)
        raw_pos = a_end
    keep(raw_pos, len(raw))
    return SourceView(text="".join(parts), artifacts=tuple(artifacts), segments=tuple(segments))
