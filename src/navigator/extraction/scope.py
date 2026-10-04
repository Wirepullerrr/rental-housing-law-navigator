"""Scope modes of document-level conditions (M3.2): how a verified exemption or coverage
condition reaches the records it governs.

The mode is decided in Python from the condition's verified SCOPE WORDS (the model's
`scope_quote`, which must be raw text located in or shortly before the condition's
evidence; without one, the evidence itself, plus the lead-in of a lettered list item).
The model's declared `scope_mode` is recorded but never trusted on its own.

  structural          the words name a container: "this section", "this Division",
                      "the provisions of this act". The governed provisions must form a
                      structural container: the whole document (governed ids null), or
                      the smallest group of inventory provisions whose refs share a token
                      prefix with the stating provision and that contains every proposed
                      id and every in-scope provision of the group. Python then propagates
                      to that container.
  explicit_reference  the words cite provisions ("Sections 98.0704 and 98.0705",
                      "subsection (4)"; a label-only reference is read relative to the
                      stating provision's section). Python parses the references and
                      propagates ONLY to inventory provisions inside them.
  named_subject       anything else: the words name a legal mechanism ("the rent cap",
                      "The Just Cause Ordinance"). Nothing propagates automatically. Every
                      proposed (condition, provision) pair, limited to provisions that
                      records link, becomes a scope_mapping_challenge in the single repair
                      request, which decides applies / does_not_apply / uncertain.

A structural or explicit-reference claim that cannot be verified is handled as
named_subject. Only "applies" mappings propagate, after the repair; does_not_apply never
does; anything else is unresolved and the document review_required.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from navigator.extraction.models import ScopeMappingDecision
from navigator.extraction.quotes import verify_quote_parts, verify_text
from navigator.extraction.review import _ref_tokens, ref_matches
from navigator.extraction.source_view import SourceView

SCOPE_RESOLVER = "scope-modes/v1"
LEAD_REACH = 4000        # how far before its evidence a condition's scope words may stand

_KIND = (r"(?:section|subsection|paragraph|subparagraph|clause|division|subdivision|article|chapter|subchapter|part|"
         r"title|act|ordinance|code|law|regulation|rule|bill)s?")
# A container as the SUBJECT of the scope clause ("This Division shall not apply", "the provisions of
# this section do not apply") or as its frame ("for purposes of this section", "nothing in this act");
# never a cross-reference such as "as defined in Division 1 of this Code".
_CONTAINER = re.compile(r"\b(?:(?:the\s+provisions\s+of\s+)?(?:this|these)\s+" + _KIND +
                        r"\s+(?:shall|does|do|is|are|will|applies|apply)\b|(?:nothing\s+in|(?:for\s+)?(?:the\s+)?"
                        r"purposes\s+of)\s+(?:this|these)\s+" + _KIND + r"\b)", re.IGNORECASE)
_BARE_CONTAINER = re.compile(r"(?:this|these)\s+" + _KIND + r"\s*$", re.IGNORECASE)
_PIECE = r"(?:\d[\w.:\-]*(?:\s*\([A-Za-z0-9]{1,4}\))*|\([A-Za-z0-9]{1,4}\)(?:\s*\([A-Za-z0-9]{1,4}\))*)"
_REF = re.compile(r"(?:§+|\b(?:sections?|subsections?|paragraphs?|subdivisions?|clauses?)\b)\s*(?P<first>" + _PIECE +
                  r")(?P<more>(?:\s*(?:,|\band\b|\bor\b|\bthrough\b|\bto\b)\s*" + _PIECE + r")*)", re.IGNORECASE)
_MORE = re.compile(_PIECE)
_LIST_ITEM = re.compile(r"^\s*\(?[A-Za-z0-9]{1,3}[).]")
# A section heading line ("§98.0703 Exemptions", "Sec. 5. Notices"), not a wrapped line of running
# text that happens to start with "Section 50093 of ...".
_SECTION_HEADING = re.compile(r"^\s*(?:§+|Sec\.|SEC\.|Section|SECTION)\s*\d[\w.:\-]*\.?\s+[A-Z]")
_LEAD_IN = re.compile(r"[^\n.;]*" + _CONTAINER.pattern + r"[^.;:]{0,300}:", re.IGNORECASE)


def _base(ref: str) -> str:
    """The section part of a ref, before its first parenthesized label: '§ 15B(9)' -> '§ 15B'."""
    return ref.split("(", 1)[0].strip()


def parse_references(text: str, source_ref: str | None) -> list[str]:
    """Provision references written in `text`; a label-only one is read relative to the
    section of `source_ref`."""
    refs = []
    for m in _REF.finditer(text):
        for piece in [m.group("first"), *_MORE.findall(m.group("more") or "")]:
            piece = " ".join(piece.split())
            if piece.startswith("("):
                if not source_ref:
                    continue
                piece = _base(source_ref) + piece
            refs.append(piece)
    return list(dict.fromkeys(refs))


def classify(text: str, source_ref: str | None) -> tuple[str, Any]:
    """(mode, detail) from the scope words alone."""
    if m := _CONTAINER.search(text):
        return "structural", m.group(0)
    if refs := parse_references(text, source_ref):
        return "explicit_reference", refs
    return "named_subject", None


def scope_words(cond: dict[str, Any], evidence_start: int, evidence_end: int, raw: str,
                view: SourceView) -> dict[str, Any]:
    """The verified words a condition's mode is decided from, and where they come from."""
    out: dict[str, Any] = {"scope_quote": cond.get("scope_quote"), "scope_quote_check": None, "text": None,
                           "from": None}
    quote = cond.get("scope_quote")
    if quote:
        check = verify_text(quote, raw, view)
        out["scope_quote_check"] = check.status
        if check.status != "failed":
            positions = [m.start() for m in re.finditer(re.escape(check.source_span), raw)] or [check.start]
            near = [s for s in positions if evidence_start - LEAD_REACH <= s <= evidence_end]
            if near:
                text = check.source_span
                if _BARE_CONTAINER.search(text):   # "This Division" + the verb that follows it in the source
                    end = near[-1] + len(check.source_span)
                    text = f"{text} {' '.join(raw[end:end + 40].split())}"
                out.update(text=text, **{"from": "scope_quote"})
                return out
            out["scope_quote_check"] = "not near the condition's evidence"
    evidence = raw[evidence_start:evidence_end]
    out.update(text=evidence, **{"from": "evidence"})
    line_prefix = raw[raw.rfind("\n", 0, evidence_start) + 1:evidence_start]
    if not _CONTAINER.search(evidence) and _LIST_ITEM.match(line_prefix + evidence):
        window = raw[max(0, evidence_start - LEAD_REACH):evidence_start]
        heading = max((m.end() for m in re.finditer(r"^.*$", window, re.MULTILINE)
                       if _SECTION_HEADING.match(m.group(0))), default=0)
        leads = list(_LEAD_IN.finditer(window, heading))
        if leads:
            out.update(text=" ".join(leads[-1].group(0).split()), **{"from": "lead-in of the list item"})
    return out


def _container(governed: list[str] | None, source_id: str | None, inventory: list[dict[str, Any]],
               in_scope: set[str]) -> tuple[set[str] | None, str]:
    everything = {p["id"] for p in inventory}
    if governed is None:
        return everything, "whole document"
    proposed = set(governed)
    source = next((p for p in inventory if p["id"] == source_id), None)
    groups: list[tuple[str, set[str]]] = []
    if source is not None:
        tokens = _ref_tokens(source["ref"])
        for k in range(len(tokens), 0, -1):
            members = {p["id"] for p in inventory if _ref_tokens(p["ref"])[:k] == tokens[:k]}
            groups.append((f"provisions under {' '.join(tokens[:k])}", members))
    groups.append(("whole document", everything))
    for label, members in groups:
        if proposed <= members and (members & in_scope) <= proposed:
            return members, label
    return None, "the proposed provisions are not a structural container of the stating provision"


def resolve_condition(cond: dict[str, Any], words: dict[str, Any], inventory: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide the mode and what the condition reaches. Returns the audit; `propagate_ids`
    is the set Python propagates to (None: every provision), or [] for named_subject,
    whose pairs are set later by named_targets."""
    by_id = {p["id"]: p for p in inventory}
    source = by_id.get(cond.get("source_provision_id") or "")
    mode, detail = classify(words["text"] or "", source["ref"] if source else None)
    audit: dict[str, Any] = {"resolver": SCOPE_RESOLVER, "declared_mode": cond.get("scope_mode"), "mode": mode,
                             "scope_words": words, "detail": detail, "propagate_ids": [], "mapping_targets": [],
                             "verification": None}
    in_scope = {p["id"] for p in inventory if p.get("scope") == "in_scope"}
    governed = cond.get("governed_provision_ids")
    if mode == "structural":
        members, why = _container(governed, cond.get("source_provision_id"), inventory, in_scope)
        audit["verification"] = why
        if members is not None:
            audit["propagate_ids"] = None if why == "whole document" else sorted(members)
            return audit
    elif mode == "explicit_reference":
        reached = {p["id"] for p in inventory if any(ref_matches(r, p["ref"]) for r in detail)}
        if reached:
            audit["propagate_ids"] = sorted(reached)
            audit["verification"] = f"references {detail} reach {sorted(reached)}"
            if governed is not None and (outside := sorted(set(governed) - reached)):
                audit["verification"] += f"; proposed ids outside them are not used: {outside}"
            return audit
        audit["verification"] = f"references {detail} reach no inventory provision"
    # named_subject, or an unverifiable structural / explicit-reference claim
    if mode != "named_subject":
        audit["fallback_from"] = mode
        audit["mode"] = "named_subject"
    return audit


def named_targets(cond: dict[str, Any], linked: set[str]) -> list[str]:
    """The pairs to verify for a named-subject condition: its proposed provisions (every
    linked provision if it proposed none), limited to provisions that records link."""
    governed = cond.get("governed_provision_ids")
    proposed = set(governed) if governed is not None else set(linked)
    return sorted(proposed & linked - {cond.get("source_provision_id")}, key=_id_order)


def _id_order(pid: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", pid)
    return (int(digits) if digits else 0, pid)


def mapping_challenges(conditions: list[dict[str, Any]], inventory: list[dict[str, Any]],
                       quotes: dict[str, str]) -> list[dict[str, Any]]:
    """One challenge per (named-subject condition, proposed provision) pair. `quotes`:
    provision id -> a verified record quote of that provision."""
    by_id = {p["id"]: p for p in inventory}
    out = []
    for c in conditions:
        if c["scope"]["mode"] != "named_subject":
            continue
        for pid in c["scope"]["mapping_targets"]:
            item = by_id[pid]
            out.append({"condition_id": c["id"], "provision_id": pid, "kind": c["kind"],
                        "statement": c["statement"], "scope_words": c["scope"]["scope_words"]["text"],
                        "condition_evidence": c["evidence"], "target_ref": item["ref"],
                        "target_anchor": item.get("anchor"), "target_quote": quotes.get(pid)})
    return out


def mapping_identity(mappings: list[dict[str, Any]]) -> list[list[str]]:
    """Sorted (condition, provision) pairs: part of the repair cache key."""
    return sorted([m["condition_id"], m["provision_id"]] for m in mappings)


def attach_mapping_decisions(items: Any, mappings: list[dict[str, Any]], raw: str, view: SourceView) -> list[str]:
    """Validate the repair's scope_mappings and attach each to its pair (m["decision"])."""
    warnings = []
    index = {(m["condition_id"], m["provision_id"]): m for m in mappings}
    for n, item in enumerate(items if isinstance(items, list) else []):
        try:
            d = ScopeMappingDecision.model_validate(item)
        except ValidationError:
            warnings.append(f"repair: scope_mappings[{n}] is malformed; ignored")
            continue
        m = index.get((d.condition_id, d.provision_id))
        if m is None:
            warnings.append(f"repair: mapping {d.condition_id}->{d.provision_id} was not requested; ignored")
            continue
        if "decision" in m:
            warnings.append(f"repair: duplicate mapping {d.condition_id}->{d.provision_id}; later entry ignored")
            continue
        check = verify_quote_parts(d.evidence_parts, raw, view)[0] if d.evidence_parts else None
        m["decision"] = {"decision": d.decision, "reason": d.reason,
                         "evidence_check": check.model_dump() if check else None,
                         "evidence_verified": bool(check) and check.status != "failed"}
    return warnings


def finalize_mappings(mappings: list[dict[str, Any]], repair_ran: bool, problem: str | None) -> dict[str, int]:
    """Final state of every mapping: applies | does_not_apply | unresolved (with a reason)."""
    counts = {"requested": len(mappings), "applies": 0, "does_not_apply": 0, "unresolved": 0}
    for m in mappings:
        d = m.get("decision")
        if not repair_ran or problem:
            final, why = "unresolved", f"repair did not complete: {problem or 'not run'}"
        elif d is None:
            final, why = "unresolved", "omitted by the repair response"
        elif not d["reason"]:
            final, why = "unresolved", "decision without a reason"
        elif d["evidence_check"] is not None and not d["evidence_verified"]:
            final, why = "unresolved", "its evidence is not source text"
        elif d["decision"] == "uncertain":
            final, why = "unresolved", "classified uncertain by the repair pass"
        elif d["decision"] == "does_not_apply" and not d["evidence_verified"]:
            final, why = "unresolved", "does_not_apply without verified evidence"
        else:
            final, why = d["decision"], None
        m["final"], m["unresolved_reason"] = final, why
        counts[final] += 1
    return counts
