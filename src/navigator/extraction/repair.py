"""Repair targets: what the single repair request is about, and when a target is resolved.

Targets are computed deterministically after the primary pass, from BOTH checks,
and merged by provision (same_ref), so a ref found by both carries both sources:
  inventory_uncovered  an in-scope inventory ref with no candidate at all
  subdivision_guard    a source subdivision next to recorded subdivisions of an
                       in-scope provision, with no candidate at all
A ref whose only candidates were rejected is never a target: rejected candidates
are not retried. It stays unresolved and the document review_required.

A structural target is not presumed to be in scope. The repair response
classifies every target as in_scope (and gives records), out_of_scope or
uncertain. After the repair pass, a target is resolved only by
  - an accepted record citing it (or one of its subdivisions), or
  - an out_of_scope classification with a reason and verified verbatim evidence;
    for a guard target the evidence must lie inside that subdivision's source text.
Uncertain, omitted, all-candidates-rejected, unverified out_of_scope and a repair
that did not run all leave the target unresolved.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from navigator.extraction.coverage import candidate_citation, same_ref
from navigator.extraction.models import CandidateResult, TargetResolution
from navigator.extraction.quotes import verify_quote_parts
from navigator.extraction.review import _ref_tokens, ref_matches
from navigator.extraction.source_view import SourceView

INVENTORY = "inventory_uncovered"
GUARD = "subdivision_guard"
RESOLVED_BY_RULE = "resolved_by_accepted_rule"
RESOLVED_OUT_OF_SCOPE = "resolved_out_of_scope"
UNRESOLVED = "unresolved"


def build_targets(closure_result: dict[str, Any], gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The union of both target kinds, deduplicated by provision, in canonical order."""
    targets: list[dict[str, Any]] = []

    def add(ref: str, source: str, **fields: Any) -> None:
        existing = next((t for t in targets if same_ref(t["ref"], ref)), None)
        if existing is None:
            targets.append({"ref": ref, "sources": [source], **fields})
            return
        existing["sources"] = sorted(set(existing["sources"]) | {source})
        for key, value in fields.items():
            existing.setdefault(key, value)

    for p in closure_result["provisions"]:
        if not p["candidates"]:
            add(p["ref"], INVENTORY, summary=p["summary"], category=p["category"],
                pre_repair_state="in-scope inventory provision with no candidate")
    for gap in gaps:
        for sub in gap["subdivisions"]:
            if not sub["candidates"]:
                add(sub["ref"], GUARD, parent=gap["ref"], raw_start=sub["raw_start"], raw_end=sub["raw_end"],
                    pre_repair_state=f"source subdivision with no candidate; {gap['ref']} is recorded only "
                                     f"through {', '.join(f'({x})' for x in gap['cited'])}")
    return canonical_order(targets)


def canonical_order(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(targets, key=lambda t: (_ref_tokens(t["ref"]), t["ref"]))


def target_identity(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sorted canonical target set: part of the repair cache key."""
    return [{"ref": t["ref"], "sources": sorted(t["sources"])} for t in canonical_order(targets)]


def match_target(ref: str, targets: list[dict[str, Any]]) -> int | None:
    return next((i for i, t in enumerate(targets) if same_ref(t["ref"], ref)), None)


def attach_resolutions(items: Any, targets: list[dict[str, Any]], raw: str, view: SourceView) -> list[str]:
    """Validate the repair's target_resolutions and attach each to its target (t["resolution"]).
    Returns warnings for malformed, unknown and duplicate entries (all ignored)."""
    warnings = []
    for n, item in enumerate(items if isinstance(items, list) else []):
        try:
            res = TargetResolution.model_validate(item)
        except ValidationError:
            warnings.append(f"repair: target_resolutions[{n}] is malformed; ignored")
            continue
        i = match_target(res.ref, targets)
        if i is None:
            warnings.append(f"repair: resolution for {res.ref!r}, which is not a requested target; ignored")
            continue
        if "resolution" in targets[i]:
            warnings.append(f"repair: duplicate resolution for {targets[i]['ref']!r}; later entry ignored")
            continue
        audit: dict[str, Any] = {"scope": res.scope, "reason": res.reason, "evidence_check": None,
                                 "evidence_verified": False, "evidence_problem": None}
        if res.evidence_parts:
            check, _ = verify_quote_parts(res.evidence_parts, raw, view)
            audit["evidence_check"] = check.model_dump()
            if check.status == "failed":
                audit["evidence_problem"] = f"evidence not found in source ({check.reason})"
            elif "raw_start" in targets[i] and not (targets[i]["raw_start"] <= check.start
                                                    and check.end <= targets[i]["raw_end"]):
                audit["evidence_problem"] = "evidence is not text of the target subdivision"
            else:
                audit["evidence_verified"] = True
        targets[i]["resolution"] = audit
    return warnings


def resolve_targets(targets: list[dict[str, Any]], candidates: list[CandidateResult], repair_ran: bool,
                    repair_problem: str | None) -> dict[str, int]:
    """Set each target's final resolution after the repair pass; return the counts."""
    for t in targets:
        res = t.get("resolution")
        covering = [c for c in candidates if candidate_citation(c) and ref_matches(t["ref"], candidate_citation(c))]
        t["repair_scope"] = res["scope"] if res else None
        t["reason"] = res["reason"] if res else None
        t["candidate_indices"] = [c.index for c in covering if c.origin == "repair"]
        t["accepted_indices"] = [c.index for c in covering if c.accepted]
        t["final_resolution"], t["unresolved_reason"] = _final(t, res, repair_ran, repair_problem)
    counts = {"before_repair": len(targets), RESOLVED_BY_RULE: 0, RESOLVED_OUT_OF_SCOPE: 0, "still_unresolved": 0}
    for t in targets:
        counts["still_unresolved" if t["final_resolution"] == UNRESOLVED else t["final_resolution"]] += 1
    return counts


def _final(t: dict[str, Any], res: dict[str, Any] | None, repair_ran: bool,
           repair_problem: str | None) -> tuple[str, str | None]:
    if t["accepted_indices"]:
        return RESOLVED_BY_RULE, None
    if not repair_ran or repair_problem:
        return UNRESOLVED, f"repair did not complete: {repair_problem or 'not run'}"
    if res is None:
        return UNRESOLVED, ("no resolution given; every candidate was rejected" if t["candidate_indices"]
                            else "omitted by the repair response")
    if res["scope"] == "out_of_scope":
        if res["reason"] and res["evidence_verified"]:
            return RESOLVED_OUT_OF_SCOPE, None
        return UNRESOLVED, ("classified out_of_scope without a reason and verified evidence"
                            + (f" ({res['evidence_problem']})" if res["evidence_problem"] else ""))
    if res["scope"] == "uncertain":
        return UNRESOLVED, "classified uncertain by the repair pass"
    return UNRESOLVED, ("classified in_scope, but every candidate was rejected" if t["candidate_indices"]
                        else "classified in_scope, but no candidate was produced")


def repair_scope_rejection(cite: str | None, targets: list[dict[str, Any]]) -> str | None:
    """The only extra check on a repair candidate, and it can only reject: it must cite a
    target, and not one the same response classified out_of_scope or uncertain."""
    hits = [t for t in targets if cite and ref_matches(t["ref"], cite)]
    if not hits:
        return "repair: candidate does not cite a repair target"
    declined = [t for t in hits if t.get("resolution", {}).get("scope") in ("out_of_scope", "uncertain")]
    if len(declined) == len(hits):
        return f"repair: candidate cites a target the repair classified as {declined[0]['resolution']['scope']}"
    return None
