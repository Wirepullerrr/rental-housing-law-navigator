"""Coverage closure: does every in-scope inventory provision have a candidate record?

Deterministic and structural (review.py matchers; never similarity). For each
in-scope inventory ref (ranges such as "(a)-(c)" expanded into elements), a
candidate maps to it when:
  - the candidate's citation names that ref or a subdivision of it, or
  - the inventory item lists the candidate's index in `rule_indices` AND the
    candidate's citation names an ancestor of the ref (a record for "(b)(1)"
    declared to cover "(b)(1)(A)").
A declared index whose candidate cites something unrelated is NOT counted; it
is reported as a link mismatch.

A ref is
  uncovered   no candidate at all (accepted or rejected) maps to it; only these
              are sent to the targeted repair pass;
  unaccepted  candidates map to it but none was accepted.

Completeness guard (unrecorded_subdivisions): the closure can only be as fine as
the inventory. When an in-scope ref is covered ONLY through records citing some
of its subdivisions (e.g. "§ 9(b)".."§ 9(f)" for an inventoried "§ 9"), the
source text around those records is scanned for the neighbouring line-initial
labels of the same style that continue the cited sequence ("(a)" before "(b)",
"(g)" after "(f)", gaps in between), stopping at section headings. Such
subdivisions with no ACCEPTED record make the document review_required. This
guard only flags; it never creates, rejects or repairs records, and it does not
trigger the repair pass (whose trigger stays: in-scope refs with no candidate).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from typing import Any

from navigator.extraction.models import CandidateResult
from navigator.extraction.review import _ref_tokens, expand_ref, ref_is_ancestor, ref_matches

_HEADING = re.compile(r"^\s*(?:§|Section\b|SECTION\b|Sec\.|SEC\.)")
_LABEL = re.compile(r"^\s*\(([a-z]|[A-Z]|\d{1,3})\)")


def candidate_citation(c: CandidateResult) -> str | None:
    if c.rule and isinstance(c.rule.get("citation"), str):
        return c.rule["citation"]
    raw = c.raw.get("citation") if isinstance(c.raw, dict) else None
    return raw if isinstance(raw, str) else None


def closure(inventory: list[dict[str, Any]], candidates: list[CandidateResult]) -> dict[str, Any]:
    cites = {c.index: candidate_citation(c) for c in candidates}
    accepted = {c.index for c in candidates if c.accepted}
    primary = {c.index for c in candidates if c.origin == "primary"}
    provisions, mismatches = [], []
    for item_no, item in enumerate(inventory):
        if item.get("scope") != "in_scope":
            continue
        declared = [i for i in item.get("rule_indices") or [] if isinstance(i, int)]
        for i in declared:
            if i not in primary:
                mismatches.append(f"inventory {item['ref']!r} lists rule {i}, which does not exist")
            elif cites[i] is None or not any(ref_matches(r, cites[i]) or ref_is_ancestor(cites[i], r)
                                             for r in expand_ref(item["ref"])):
                mismatches.append(f"inventory {item['ref']!r} lists rule {i}, which cites {cites[i]!r}")
        for ref in expand_ref(item["ref"]):
            basis: dict[int, str] = {}
            for i, cite in cites.items():
                if cite is None:
                    continue
                if ref_matches(ref, cite):
                    basis[i] = "citation"
                elif i in declared and i in primary and ref_is_ancestor(cite, ref):
                    basis[i] = "declared link; citation names an ancestor"
            provisions.append({"ref": ref, "inventory_item": item_no, "summary": item.get("summary"),
                               "category": item.get("category"), "candidates": sorted(basis),
                               "accepted": sorted(i for i in basis if i in accepted),
                               "basis": {str(i): b for i, b in sorted(basis.items())}})
    return {"provisions": provisions,
            "uncovered": [p["ref"] for p in provisions if not p["candidates"]],
            "unaccepted": [p["ref"] for p in provisions if p["candidates"] and not p["accepted"]],
            "link_mismatches": mismatches}


def _style(label: str) -> str:
    return "digit" if label.isdigit() else "lower" if label.islower() else "upper"


def _step(label: str, delta: int) -> str:
    return str(int(label) + delta) if label.isdigit() else chr(ord(label) + delta)


def unrecorded_subdivisions(inventory: list[dict[str, Any]], candidates: list[CandidateResult],
                            raw: str) -> list[dict[str, Any]]:
    """In-scope refs covered (by accepted records) only via some subdivisions, with the
    neighbouring source subdivisions that no accepted record cites (see module docstring)."""
    lines = raw.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    located = [c for c in candidates if c.accepted and c.citation.start is not None and candidate_citation(c)]
    listed = {r for p in inventory for r in expand_ref(p["ref"])}
    found: list[dict[str, Any]] = []
    for item in inventory:
        if item.get("scope") != "in_scope":
            continue
        for ref in expand_ref(item["ref"]):
            rt = _ref_tokens(ref)
            children: dict[str, list[CandidateResult]] = {}
            exact = False
            for c in located:
                for cite in expand_ref(candidate_citation(c)):
                    t = _ref_tokens(cite)
                    k = next((i for i in range(len(t) - len(rt) + 1) if t[i:i + len(rt)] == rt), None)
                    if k is None:
                        continue
                    if len(t) == k + len(rt):
                        exact = True
                    else:
                        children.setdefault(t[k + len(rt)], []).append(c)
            labels = [x for x in children if re.fullmatch(r"[a-z]|\d{1,3}", x)]
            if exact or not labels or len({_style(x) for x in labels}) != 1:
                continue
            missing = [m for m in _neighbours(lines, starts, [c for x in labels for c in children[x]], set(labels))
                       if f"{ref}({m})" not in listed]
            if missing:
                found.append({"ref": ref, "cited": sorted(labels, key=_order), "unrecorded": missing})
    return found


def _order(label: str) -> int:
    return int(label) if label.isdigit() else ord(label)


def _neighbours(lines: list[str], starts: list[int], records: list[CandidateResult], cited: set[str]) -> list[str]:
    """Labels of the source list that the cited subdivisions belong to, minus the cited ones.
    The list is a run of line-initial labels of one style ("(a)", "(A)" or "(1)") counting up
    by one, inside the heading-bounded region around the records, that contains every cited
    label and overlaps the records' lines. No such run: no claim is made."""
    first = min(bisect_right(starts, c.citation.start) - 1 for c in records)
    last = max(bisect_right(starts, c.citation.end - 1) - 1 for c in records)
    top, bottom = first, last
    while top > 0 and not _HEADING.match(lines[top - 1]):
        top -= 1
    while bottom + 1 < len(lines) and not _HEADING.match(lines[bottom + 1]):
        bottom += 1
    styles = ("digit",) if next(iter(cited)).isdigit() else ("lower", "upper")
    best: list[tuple[int, str]] = []
    for style in styles:
        runs: list[list[tuple[int, str]]] = []
        for n in range(top, bottom + 1):
            m = _LABEL.match(lines[n])
            if not m or _style(m.group(1)) != style:
                continue
            label = m.group(1).lower()
            if runs and label == _step(runs[-1][-1][1], 1):
                runs[-1].append((n, label))
            else:
                runs.append([(n, label)])
        for run in runs:
            overlap = min(run[-1][0], last) - max(run[0][0], first)
            if cited <= {label for _, label in run} and overlap >= 0 and len(run) > len(best):
                best = run
    return [label for _, label in best if label not in cited]
