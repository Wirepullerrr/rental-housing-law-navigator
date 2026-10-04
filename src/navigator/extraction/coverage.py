"""Coverage closure: does every in-scope inventory provision have a candidate record?

Deterministic and structural (review.py matchers; never similarity). Records link to
inventory provisions by id (`provision_ids`, prompt v6). For each in-scope inventory
ref (ranges such as "(a)-(c)" expanded into elements), a candidate maps to it when it
LINKS that provision's id AND its citation names the ref, a subdivision of it, or an
ancestor of it (a record for "(b)(1)" linked to an item "(b)(1)(A)"). A citation alone
never counts: free-form titles (which may contain commas, or share words with their
neighbours) cannot make a provision look covered. A link whose citation names
something unrelated is NOT counted; it is reported as a link mismatch.

A ref is
  uncovered   no candidate at all (accepted or rejected) maps to it; only these
              are sent to the targeted repair pass;
  unaccepted  candidates map to it but none was validated (accepted; historical, i.e.
              validated but expired as of the query date; or an exact duplicate of either).

Subdivision guard (unrecorded_subdivisions): the closure can only be as fine as
the inventory. When an in-scope ref is covered by accepted records ONLY through
some of its subdivisions (e.g. "§ 9(b)".."§ 9(f)" for an inventoried "§ 9"), the
source text around those records is scanned for the neighbouring line-initial
labels of the same style that continue the cited sequence ("(a)" before "(b)",
"(g)" after "(f)", gaps in between), stopping at section headings. Each such
subdivision without an accepted record is reported with its source range and any
(rejected) candidates citing it. Subdivisions the inventory itself lists as
out_of_scope or uncertain are left to that decision. The guard only reports;
repair.py decides what becomes a repair target.

Scope challenge (scope_challenges): neither check above can see a provision the model
itself declared out_of_scope. Each inventory item gives a verbatim `anchor` (its first
words); provision_regions locates it in the raw text. The item's region starts at its
own label when only a label precedes the anchor on that line (e.g. "(c) "), and runs
to the next located anchor. An out_of_scope item becomes a scope_challenge only when
SEVERAL independent signals in its own source text suggest a substantive rule:
  - its role is definition, procedure or uncertain: a claim that it states no rule.
    Operative rules of another subject, exemptions/scope conditions, remedies,
    enforcement, history and boilerplate are decided by other rules;
  - a neighbouring inventory item (document order) is in scope;
  - its text contains an enumeration (two or more labelled lines after its own first
    line, or an inline series after "such as" / "including" / "the following");
  - and normative language (shall, must, may only, prohibited, ...) or
    grounds/conditions language (grounds, causes, reasons, conditions, criteria).
Not challenged: a glossary (two or more "means" definitions), and a provision whose
content is a verified document-level scope condition (its source_provision_id).
A challenge never publishes anything; it only makes the item a repair target.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from typing import Any

from navigator.extraction.models import CandidateResult
from navigator.extraction.quotes import verify_text
from navigator.extraction.review import _ref_tokens, expand_ref, ref_is_ancestor, ref_matches
from navigator.extraction.source_view import SourceView

_HEADING = re.compile(r"^\s*(?:§|Section\b|SECTION\b|Sec\.|SEC\.)")
_LABEL = re.compile(r"^\s*\(([a-z]|[A-Z]|\d{1,3})\)")
MAX_REGION = 2000
CHALLENGE_ROLES = {"definition", "procedure", "uncertain", None}
# Only labels (e.g. "(c) ", "1. ", "Sec. 5. ") between the start of a line and an anchor.
_LEADING_LABELS = re.compile(r"[\s\u00a0]*(?:(?:§+|Sec\.|Section|SEC\.)?[\s\u00a0]*\(?[A-Za-z0-9]{1,4}"
                             r"(?:[.:-][A-Za-z0-9]{1,4})*[).:]?[\s\u00a0]*){1,4}")
_LIST_LINE = re.compile(r"^\s*(?:\([A-Za-z0-9]{1,3}\)|\d{1,3}\.|[a-z]\.)\s", re.MULTILINE)
_SERIES = re.compile(r"\b(?:such\s+as|including|includes|include|the\s+following)\b(?P<rest>[^.]{0,300})",
                     re.IGNORECASE)
_NORMATIVE = re.compile(r"\b(?:shall|must|may\s+only|may\s+not|no\s+\w+\s+may|prohibited|unlawful|permitted|"
                        r"allowed|entitled|required)\b", re.IGNORECASE)
_GROUNDS = re.compile(r"\b(?:grounds?|causes?|reasons?|conditions?|criteria|criterion|circumstances?)\b",
                      re.IGNORECASE)
_MEANS = re.compile(r"\bmeans\b", re.IGNORECASE)


def candidate_citation(c: CandidateResult) -> str | None:
    if c.rule and isinstance(c.rule.get("citation"), str):
        return c.rule["citation"]
    raw = c.raw.get("citation") if isinstance(c.raw, dict) else None
    return raw if isinstance(raw, str) else None


def linked(item: dict[str, Any], c: CandidateResult) -> bool:
    return bool(item.get("id")) and item["id"] in c.provision_ids


def closure(inventory: list[dict[str, Any]], candidates: list[CandidateResult]) -> dict[str, Any]:
    cites = {c.index: candidate_citation(c) for c in candidates}
    accepted = {c.index for c in candidates if c.validated}   # accepted, historical or suppressed twin
    provisions, mismatches = [], []
    for item_no, item in enumerate(inventory):
        if item.get("scope") != "in_scope":
            continue
        declared = [c.index for c in candidates if linked(item, c)]
        for i in declared:
            if cites[i] is None or not any(ref_matches(r, cites[i]) or ref_is_ancestor(cites[i], r)
                                           for r in expand_ref(item["ref"])):
                mismatches.append(f"rule {i} links {item['id']} ({item['ref']!r}) but cites {cites[i]!r}")
        for ref in expand_ref(item["ref"]):
            basis: dict[int, str] = {}
            for i in declared:
                if cites[i] is None:
                    continue
                if ref_matches(ref, cites[i]):
                    basis[i] = "provision link; citation names it"
                elif ref_is_ancestor(cites[i], ref):
                    basis[i] = "provision link; citation names an ancestor"
            provisions.append({"ref": ref, "inventory_item": item_no, "provision_id": item.get("id"),
                               "summary": item.get("summary"),
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


def same_ref(a: str, b: str) -> bool:
    """Same provision, allowing one side to omit a citation prefix ('98.0709(a)' vs
    'San Diego Mun. Code § 98.0709(a)'); never a bare single-token suffix match."""
    ta, tb = _ref_tokens(a), _ref_tokens(b)
    n = min(len(ta), len(tb))
    return ta == tb or (n >= 2 and ta[-n:] == tb[-n:])


def unrecorded_subdivisions(inventory: list[dict[str, Any]], candidates: list[CandidateResult],
                            raw: str) -> list[dict[str, Any]]:
    """In-scope refs covered (by accepted records) only via some subdivisions, with the
    neighbouring source subdivisions that no accepted record cites (see module docstring)."""
    lines = raw.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    located = [c for c in candidates if c.validated and c.citation.start is not None and candidate_citation(c)]
    decided = [r for p in inventory if p.get("scope") in ("out_of_scope", "uncertain") for r in expand_ref(p["ref"])]
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
            subdivisions = []
            for label, raw_start, raw_end in _neighbours(lines, starts, [c for x in labels for c in children[x]],
                                                         set(labels)):
                sub = f"{ref}({label})"
                if any(same_ref(sub, d) for d in decided):
                    continue
                subdivisions.append({"ref": sub, "label": label, "raw_start": raw_start, "raw_end": raw_end,
                                     "candidates": [c.index for c in candidates
                                                    if candidate_citation(c) and ref_matches(sub, candidate_citation(c))]})
            if subdivisions:
                found.append({"ref": ref, "provision_id": item.get("id"), "cited": sorted(labels, key=_order),
                              "unrecorded": [s["label"] for s in subdivisions], "subdivisions": subdivisions})
    return found


def _order(label: str) -> int:
    return int(label) if label.isdigit() else ord(label)


def _neighbours(lines: list[str], starts: list[int], records: list[CandidateResult],
                cited: set[str]) -> list[tuple[str, int, int]]:
    """(label as written, raw_start, raw_end) for each label of the source list that the
    cited subdivisions belong to, minus the cited ones. The list is a run of line-initial
    labels of one style ("(a)", "(A)" or "(1)") counting up by one, inside the
    heading-bounded region around the records, that contains every cited label and
    overlaps the records' lines. A subdivision's range runs to the next label of the run
    (or the end of the region). No such run: no claim is made."""
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
            if runs and m.group(1).lower() == _step(runs[-1][-1][1].lower(), 1):
                runs[-1].append((n, m.group(1)))
            else:
                runs.append([(n, m.group(1))])
        for run in runs:
            overlap = min(run[-1][0], last) - max(run[0][0], first)
            if cited <= {label.lower() for _, label in run} and overlap >= 0 and len(run) > len(best):
                best = run
    bounds = [n for n, _ in best[1:]] + [bottom + 1]
    return [(label, starts[n], starts[end]) for (n, label), end in zip(best, bounds) if label.lower() not in cited]


def provision_regions(inventory: list[dict[str, Any]], raw: str, view: SourceView) -> dict[str, dict[str, Any]]:
    """Inventory id -> the raw source region of that provision: from its verified anchor
    to the next located anchor (at most MAX_REGION characters). Items whose anchor is
    missing or not source text have no region."""
    located: list[tuple[int, str, str]] = []
    cursor = 0
    for item in inventory:
        if not item.get("id") or not item.get("anchor"):
            continue
        check = verify_text(item["anchor"], raw, view)
        if check.status == "failed":
            continue
        start = check.start
        if check.status == "exact_match" and check.occurrences > 1:   # repeated words: take the next one in order
            nxt = raw.find(check.source_span, cursor)
            start = nxt if nxt >= 0 else start
        cursor = start
        line_start = raw.rfind("\n", 0, start) + 1
        if line_start < start and _LEADING_LABELS.fullmatch(raw[line_start:start]):
            start = line_start                                           # include the provision's own label
        located.append((start, item["id"], check.status))
    starts = sorted({s for s, _, _ in located})
    regions = {}
    for start, pid, status in located:
        end = next((s for s in starts if s > start), len(raw))
        regions[pid] = {"raw_start": start, "raw_end": min(end, start + MAX_REGION), "anchor_check": status}
    return regions


def _enumeration(text: str) -> bool:
    _, _, rest = text.partition("\n")              # the provision's own first line (its label) does not count
    if len(_LIST_LINE.findall(rest)) >= 2:
        return True
    return any(len(re.findall(r"[,;]", m.group("rest"))) >= 2 for m in _SERIES.finditer(text))


def scope_challenges(inventory: list[dict[str, Any]], regions: dict[str, dict[str, Any]], raw: str,
                     scope_sources: set[str]) -> list[dict[str, Any]]:
    """Every out_of_scope inventory item, with its signals and whether it is challenged
    (module docstring). `scope_sources`: ids of provisions that state a verified
    document-level scope condition."""
    out = []
    for k, item in enumerate(inventory):
        if item.get("scope") != "out_of_scope":
            continue
        role = item.get("role")
        entry: dict[str, Any] = {"ref": item["ref"], "provision_id": item.get("id"), "role": role,
                                 "reason": item.get("reason"), "summary": item.get("summary"), "signals": {},
                                 "challenged": False, "why_not": None}
        out.append(entry)
        if role not in CHALLENGE_ROLES:
            entry["why_not"] = f"role {role} is decided by other rules"
            continue
        if item.get("id") in scope_sources:
            entry["why_not"] = "its content is a verified document-level scope condition"
            continue
        region = regions.get(item.get("id"))
        if region is None:
            entry["why_not"] = "no verified anchor: its source text cannot be located"
            continue
        text = raw[region["raw_start"]:region["raw_end"]]
        entry.update(raw_start=region["raw_start"], raw_end=region["raw_end"])
        neighbours = [inventory[j] for j in (k - 1, k + 1) if 0 <= j < len(inventory)]
        signals = entry["signals"] = {
            "in_scope_neighbour": any(n.get("scope") == "in_scope" for n in neighbours),
            "enumeration": _enumeration(text),
            "normative_language": _NORMATIVE.search(text) is not None,
            "grounds_or_conditions": _GROUNDS.search(text) is not None,
        }
        if len(_MEANS.findall(text)) >= 2:
            entry["why_not"] = "glossary: several 'means' definitions"
            continue
        entry["challenged"] = (signals["in_scope_neighbour"] and signals["enumeration"]
                               and (signals["normative_language"] or signals["grounds_or_conditions"]))
        if not entry["challenged"]:
            entry["why_not"] = "too few substantive signals"
    return out
