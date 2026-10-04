"""Post-run report for a full-corpus extraction stage (M3). Offline: reads only the
stage's per-document artifacts, its spend ledger, the runner summary, the manifest
and the supplied texts. No provider is contacted; extraction output is not changed.

    uv run python scripts/report_corpus.py --run-dir outputs/m3/full \\
        --unprocessed "D067=reason it was not sent"

Writes into --run-dir:
  full_corpus_summary.json   corpus totals and one audit row per supplied-text document
  review_queue.json          every review_required document, with machine-readable reason codes
  rules.json                 candidate Module-A output: publishable accepted records only,
                             in the submission-template wrapper {"rules": [...]}
  duplicate_clusters.json    exact same-source duplicates the pipeline suppressed, and likely duplicate /
                             overlapping records within and across documents (audit only; nothing
                             further is merged or removed)
  delta_vs_baseline.json     (with --baseline-dir) every candidate whose publishability changed

Spend figures are estimates from API-reported token usage, never a billing balance.
Not legal advice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigator import starter_pack as sp  # noqa: E402
from navigator.extraction.config import PRICE_PER_MTOK  # noqa: E402
from navigator.extraction.corpus import (DISCLAIMER, _display, estimate_cost, summary_path,  # noqa: E402
                                         supplied_documents)
from navigator.extraction.extractor import load_source  # noqa: E402
from navigator.extraction.models import ExtractionRun  # noqa: E402
from navigator.extraction.normalize import level_for  # noqa: E402
from navigator.validation import make_rule_validator, rule_records  # noqa: E402

TARGET_TYPES = ("inventory_uncovered", "subdivision_guard", "scope_challenge")
SUMMARY_BASES = {"official_bill_summary", "official_bill_history"}


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- review reason codes

def _held_codes(run: ExtractionRun) -> list[dict[str, Any]]:
    out = []
    for c in run.candidates:
        if not c.held:
            continue
        sd = c.status_derivation or ""
        rel = c.temporal.get("relative_resolution") or {}
        if "legislative status undetermined" in sd:
            code, sub = "other", "legislative_status_conflict"
        elif "end boundar" in sd or "version/history evidence has a date" in sd or "stated end day" in sd \
                or "start after as_of but an end" in sd:
            code, sub = "unresolved_effective_date", f"validity window: {sd}"
        elif "relative or conditional" in sd:
            reason = rel.get("reason") or ""
            code = ("unsupported_date_formula" if "supported grammar" in reason or "unsupported" in reason
                    else "unresolved_relative_date")
            sub = reason or None
        elif "no effective date is established" in sd:
            code, sub = "unresolved_effective_date", f"posture {run.posture.get('established')}"
        else:
            code, sub = "other", sd
        out.append({"code": code, "subcode": sub, "candidate": c.index, "citation": (c.rule or {}).get("citation")})
    return out


def review_codes(run: ExtractionRun) -> list[dict[str, Any]]:
    """One entry per pipeline review reason: the user-facing code, an optional subcode and the
    pipeline's own text. Codes follow the M3 review-queue vocabulary; 'other' is the catch-all."""
    targets = run.repair.targets if run.repair is not None else []
    out: list[dict[str, Any]] = []

    def add(code: str, text: str, subcode: str | None = None, **extra: Any) -> None:
        out.append({"code": code, "subcode": subcode, "detail": text, **extra})

    for text in run.review_reasons:
        if text.startswith("the response could not be processed"):
            add("other", text, "response_unprocessable")
        elif text.startswith(("no complete, well-formed provision inventory", "the inventory lists no provisions")):
            add("incomplete_inventory", text, "inventory_invalid")
        elif text.startswith("no rule candidates, and no source-grounded justification"):
            add("other", text, "unjustified_empty_result")
        elif text.startswith("inventory provisions of uncertain scope"):
            add("uncertain_scope", text, "inventory_uncertain")
        elif "held, not published" in text:
            add("held_rule", text, None, held=_held_codes(run))
            for h in _held_codes(run):
                if h["code"] != "held_rule":
                    add(h["code"], text, h["subcode"], candidate=h["candidate"])
        elif text.startswith("enactment_status inconsistent with the document posture"):
            add("ambiguous_document_posture", text, "status_posture_mismatch")
        elif text.startswith(("named-subject scope mappings unresolved", "named-subject scope mappings never")):
            add("scope_mapping_uncertain", text)
        elif text.startswith("legislative status needs review"):
            add("other", text, "legislative_status_conflict")
        elif text.startswith("document-level scope conditions not applied"):
            add("uncertain_scope", text, "global_scope_unverified")
        elif text.startswith("repair targets still unresolved"):
            for t in targets:
                if t.get("final_resolution") != "unresolved":
                    continue
                kinds = set(t.get("sources") or [])
                code = ("uncovered_subdivision" if kinds == {"subdivision_guard"} else
                        "uncertain_scope" if kinds == {"scope_challenge"} else "incomplete_inventory")
                add(code, text, f"unresolved repair target {t['ref']}: {t.get('unresolved_reason')}",
                    target_sources=sorted(kinds))
        elif text.startswith("in-scope provisions with no candidate record"):
            add("incomplete_inventory", text, "in_scope_provision_without_record")
        elif text.startswith("in-scope provisions whose candidate records were all rejected"):
            add("incomplete_inventory", text, "in_scope_provision_all_rejected")
        elif text.startswith("source subdivisions with no accepted record"):
            add("uncovered_subdivision", text)
        elif text.startswith("repair pass:"):
            add("repair_failed", text)
        else:
            add("other", text)
    return out


# ---------------------------------------------------------------- per-document audit

def _usage(entries: list[dict[str, Any]], kind: str) -> dict[str, int]:
    return {k: sum((e.get("usage") or {}).get(k) or 0 for e in entries if e["pass"] == kind)
            for k in ("input_tokens", "output_tokens", "thinking_tokens")}


def document_audit(run: ExtractionRun, ledger: list[dict[str, Any]], body: str) -> dict[str, Any]:
    rp = run.repair
    targets = rp.targets if rp is not None else []
    accepted = [c for c in run.candidates if c.accepted]
    held = [c for c in run.candidates if c.held]
    historical = [c for c in run.candidates if c.historical]
    suppressed = [c for c in run.candidates if c.duplicate_of is not None]
    rejected = [c for c in run.candidates if not (c.accepted or c.held or c.historical or c.duplicate_of is not None)]
    mine = [e for e in ledger if e["doc_id"] == run.source.doc_id]
    usage = {k: _usage(mine, k) for k in ("primary", "repair")}
    cost = {k: round(sum(e["estimated_cost_usd"] for e in mine if e["pass"] == k), 6) for k in usage}
    by_type = Counter(s for t in targets for s in (t.get("sources") or []))
    outcomes = Counter()
    for t in targets:
        fr = t.get("final_resolution")
        if fr == "resolved_by_accepted_rule":
            outcomes["accepted_rule"] += 1
        elif fr == "resolved_out_of_scope":
            outcomes["verified_out_of_scope"] += 1
        elif "rejected" in (t.get("unresolved_reason") or ""):
            outcomes["rejected"] += 1
        else:
            outcomes["unresolved"] += 1
    mapping_outcomes = Counter()
    for m in run.scope_mappings:
        decision = (m.get("decision") or {}).get("decision")
        if decision == "uncertain":
            mapping_outcomes["uncertain"] += 1
        mapping_outcomes[str(m.get("final"))] += 1
    rel = [{"candidate": c.index, "outcome": r.get("outcome"), "formula": r.get("formula"),
            "base_date": r.get("base_date"), "resolved_date": r.get("resolved_date"), "reason": r.get("reason"),
            "accepted": c.accepted, "held": c.held}
           for c in run.candidates if (r := c.temporal.get("relative_resolution"))]
    legis = [{"candidate": c.index, "model_status": (c.raw or {}).get("enactment_status"),
              "status": c.legislative.get("status"), "basis": c.legislative.get("status_basis"),
              "conflict": c.legislative.get("conflict"), "accepted": c.accepted, "held": c.held}
             for c in run.candidates if c.legislative]
    return {
        "doc_id": run.source.doc_id, "jurisdiction": run.source.jurisdiction,
        "level": level_for(run.source.jurisdiction), "source_type": run.source.source_type,
        "posture": {"declared": run.posture.get("declared"), "established": run.posture.get("established")},
        "source_basis": dict(Counter(str(c.source_basis) for c in accepted + held)),
        "source_chars": run.source.body_chars, "model": run.model,
        "reported_model": run.provider_metadata.get("model"),
        "thinking_level": run.generation_settings.get("thinking_level"), "prompt_version": run.prompt_version,
        "primary_cache_hit": run.cache_hit, "repair_cache_hit": rp.cache_hit if rp is not None else None,
        "primary_provider_calls": sum(e["pass"] == "primary" for e in mine),
        "repair_provider_calls": sum(e["pass"] == "repair" for e in mine),
        "repair_invoked": bool(rp is not None and rp.invoked),
        "inventory_count": len(run.provision_inventory),
        "candidates": run.candidate_count, "accepted": len(accepted), "rejected": len(rejected), "held": len(held),
        "historical": len(historical), "suppressed_duplicates": len(suppressed),
        "historical_records": [{"candidate": c.index, "team_rule_id": c.rule["team_rule_id"],
                                "temporal_state": c.temporal_state, "effective_date": c.rule["effective_date"],
                                "end_exclusive": c.validity.get("end_exclusive"),
                                "evidence": [b for b in c.validity.get("boundaries", []) if b["role"] == "end"],
                                "derivation": c.status_derivation} for c in historical],
        "dedupe": run.dedupe,
        "temporal_states": dict(Counter(str(c.temporal_state) for c in run.candidates)),
        "repair_targets_by_type": {**{k: by_type.get(k, 0) for k in TARGET_TYPES},
                                   "scope_mapping_challenge": len(run.scope_mappings)},
        "target_outcomes": {**{k: outcomes.get(k, 0) for k in ("accepted_rule", "verified_out_of_scope",
                                                                "rejected", "unresolved")},
                            **{f"mapping_{k}": mapping_outcomes.get(k, 0)
                               for k in ("applies", "does_not_apply", "uncertain", "unresolved")}},
        "citations": {"accepted": len(accepted),
                      "exact_match": sum(c.citation.status == "exact_match" for c in accepted),
                      "normalized_match": sum(c.citation.status == "normalized_match" for c in accepted),
                      "layout_normalized_match": sum(c.citation.status == "layout_normalized_match"
                                                     for c in accepted),
                      "reconstructed_cross_page": sum(c.citation.reconstructed for c in accepted),
                      "raw_substring": sum(r["quoted_span"] in body for r in run.rules),
                      "all_candidates": dict(Counter(c.citation.status for c in run.candidates if c.citation)),
                      "rejected_for_citation": sum(any(x.startswith("citation: quote not found") for x in
                                                       c.rejection_reasons) for c in rejected)},
        "global_scope": [{"id": g["id"], "kind": g["kind"], "declared_mode": g.get("scope_mode"),
                          "mode": (g.get("scope") or {}).get("mode"), "propagated": g["propagated"],
                          "reaches": (g.get("scope") or {}).get("propagate_ids", "n/a"),
                          "applied_to": [c.index for c in accepted
                                         if any(p["id"] == g["id"] and p["applied"] for p in c.propagated_scope)]}
                         for g in run.global_scope],
        "scope_mappings": [{"pair": f"{m['condition_id']}->{m['provision_id']}",
                            "decision": (m.get("decision") or {}).get("decision"), "final": m.get("final")}
                           for m in run.scope_mappings],
        "status_decisions": dict(Counter(c.status_derivation or "n/a" for c in accepted + held)),
        "statuses": dict(Counter(c.rule["status"] for c in accepted)),
        "relative_date_resolutions": rel,
        "legislative_session": run.legislative_session, "legislative_decisions": legis,
        "document_status": run.document_status, "review_reasons": run.review_reasons,
        "review_codes": sorted({r["code"] for r in review_codes(run)}),
        "errors": run.errors,
        "tokens": usage, "estimated_cost_usd": {**cost, "total": round(sum(cost.values()), 6)},
    }


# ---------------------------------------------------------------- rules.json

def build_rules(runs: dict[str, ExtractionRun], bodies: dict[str, str], manifest: dict[str, dict[str, str]],
                validator) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rules, problems = [], defaultdict(list)
    seen: dict[str, str] = {}
    for doc_id in sorted(runs):
        run = runs[doc_id]
        for c in run.candidates:
            if not c.accepted or c.held:
                continue
            rule = c.rule
            where = f"{doc_id} candidate {c.index}"
            if errs := [e.message for e in validator.iter_errors(rule)]:
                problems["schema_failures"].append(f"{where}: {errs}")
            if rule["team_rule_id"] in seen:
                problems["duplicate_ids"].append(f"{where}: {rule['team_rule_id']} also {seen[rule['team_rule_id']]}")
            seen.setdefault(rule["team_rule_id"], where)
            m = manifest[doc_id]
            if rule.get("source_doc_id") != doc_id or rule.get("source_url") != m["url"]:
                problems["provenance_failures"].append(f"{where}: source_doc_id/source_url differ from the manifest")
            if rule.get("jurisdiction") != m["jurisdiction"]:
                problems["provenance_failures"].append(f"{where}: jurisdiction differs from the manifest")
            if c.citation is None or c.citation.status == "failed" or rule["quoted_span"] not in bodies[doc_id]:
                problems["citation_failures"].append(f"{where}: quoted_span is not verified raw source text")
            if (c.historical or c.duplicate_of is not None or rule["status"] is None
                    or (c.validity.get("end_exclusive") or "9999") <= run.as_of):
                problems["temporal_failures"].append(f"{where}: held, historical, suppressed or expired record")
            rules.append(rule)
    return rules, {k: problems.get(k, []) for k in ("schema_failures", "duplicate_ids", "citation_failures",
                                                     "provenance_failures", "temporal_failures")}


# ---------------------------------------------------------------- duplicate / overlap audit

_CA_CODES = {"civ": "Civ", "civil": "Civ", "gov": "Gov", "government": "Gov", "bpc": "Bus", "bus": "Bus",
             "business": "Bus", "hsc": "Hea", "health": "Hea", "ccp": "CCP", "pen": "Pen", "penal": "Pen"}
# (state the pattern belongs to, pattern, key). A state-specific pattern is applied only to records of
# that state, so e.g. an NJ session-law "c.110, § 4" is never read as a Massachusetts chapter.
_CITE_KEYS = [
    ("CA", re.compile(r"\b(Civ(?:il)?|Gov(?:ernment)?|Health|Bus(?:iness)?|Penal|Code\s+of\s+Civil\s+Procedure)\b"
                      r"[^§\d]{0,30}?(?:Code)?\s*,?\s*(?:§+|sec(?:tion)?s?\.?)\s*(\d+(?:\.\d+)?)", re.I),
     lambda m: f"CA {_CA_CODES.get(m.group(1).split()[0].lower(), 'CCP')} Code {m.group(2)}"),
    ("CA", re.compile(r"\b(CIV|GOV|BPC|HSC|CCP|PEN)\s*(?:§+|sec(?:tion)?\.?)\s*(\d+(?:\.\d+)?)"),
     lambda m: f"CA {_CA_CODES.get(m.group(1).lower(), m.group(1))} Code {m.group(2)}"),
    ("MA", re.compile(r"\b(?:c\.|ch\.|chapter)\s*(\d+[A-Z]?)\s*,?\s*(?:§+|sec(?:tion)?\.?)\s*(\d+[A-Z]*)", re.I),
     lambda m: f"MGL c.{m.group(1).upper()} s.{m.group(2).upper()}"),
    ("NJ", re.compile(r"\b(?:N\.?\s*J\.?\s*S\.?\s*A\.?|C\.)\s*(\d+[A-Z]?:\d+[A-Z]?-\d+(?:\.\d+)?)", re.I),
     lambda m: f"NJSA {m.group(1).upper()}"),
    ("NJ", re.compile(r"\bN\.?\s*J\.?\s*A\.?\s*C\.?\s*(\d+:\d+-\d+(?:\.\d+)?)", re.I), lambda m: f"NJAC {m.group(1)}"),
    (None, re.compile(r"\b(BMC|B\.M\.C\.|LAMC|L\.A\.M\.C\.|SDMC|S\.D\.M\.C\.|Admin(?:istrative)?\.?\s+Code|Municipal\s+"
                      r"Code|Rent\s+Ordinance)\s*,?\s*(?:§+|sec(?:tion)?\.?)?\s*(\d+(?:\.\d+)*[A-Z]?)", re.I),
     lambda m: f"{re.sub(r'[^A-Za-z]', '', m.group(1)).upper()[:6]} {m.group(2)}"),
]


def citation_keys(citation: str, jurisdiction: str = "") -> set[str]:
    state = jurisdiction.split(",")[-1].strip()
    keys = {fmt(m) for st, rx, fmt in _CITE_KEYS if st in (None, state) for m in rx.finditer(citation or "")}
    return {f"{jurisdiction} {k}" if k.split()[0] in ("BMC", "LAMC", "SDMC", "ADMINC", "MUNICI", "RENTOR") else k
            for k in keys}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _shingles(text: str, n: int = 8) -> set[str]:
    words = _norm(text).split()
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _member(doc_id: str, r: dict[str, Any]) -> dict[str, Any]:
    return {"team_rule_id": r["team_rule_id"], "source_doc_id": doc_id, "jurisdiction": r["jurisdiction"],
            "category": r["category"], "citation": r["citation"], "status": r["status"],
            "effective_date": r["effective_date"], "published": r.get("_published", True),
            "quoted_span": r["quoted_span"][:160]}


def _components(pairs: list[tuple[int, int]], n: int) -> list[list[int]]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[find(a)] = find(b)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def duplicate_audit(rules: list[dict[str, Any]], bodies: dict[str, str],
                    runs: dict[str, ExtractionRun] | None = None) -> dict[str, Any]:
    """`rules`: the published records plus held records (marked "_published": False), so that a
    statute restated by a secondary page whose records are held is still found."""
    clusters: list[dict[str, Any]] = []
    # 0. exact same-source duplicates already suppressed by the pipeline (one record published)
    for doc_id, run in sorted((runs or {}).items()):
        for entry in run.dedupe:
            clusters.append({"type": "suppressed_exact_duplicate", "documents": [doc_id], **entry, "rules": []})
    # 1. source documents whose supplied texts overlap heavily (the same article captured twice)
    docs = sorted(bodies)
    sh = {d: _shingles(bodies[d]) for d in docs}
    for i, a in enumerate(docs):
        for b in docs[i + 1:]:
            j = _jaccard(sh[a], sh[b])
            if j >= 0.5:
                clusters.append({"type": "overlapping_source_documents", "documents": [a, b],
                                 "text_jaccard_8gram": round(j, 3),
                                 "rules": [_member(r["source_doc_id"], r) for r in rules
                                           if r["source_doc_id"] in (a, b)]})
    # 2. identical quoted text in different documents
    by_quote = defaultdict(list)
    for r in rules:
        by_quote[_norm(r["quoted_span"])].append(r)
    for group in by_quote.values():
        if len({r["source_doc_id"] for r in group}) > 1:
            clusters.append({"type": "identical_quote_across_documents",
                             "rules": [_member(r["source_doc_id"], r) for r in group]})
    # 3. the same statute section cited from several documents (official vs secondary pages,
    #    amended/current versions); temporal variants are flagged, never merged
    by_cite = defaultdict(list)
    for r in rules:
        for key in citation_keys(r["citation"], r["jurisdiction"]):
            by_cite[key].append(r)
    for key, group in sorted(by_cite.items()):
        if len({r["source_doc_id"] for r in group}) > 1:
            clusters.append({"type": "same_statute_multiple_documents", "citation_key": key,
                             "documents": sorted({r["source_doc_id"] for r in group}),
                             "temporal_variants": len({(r["status"], r["effective_date"]) for r in group
                                                       if r.get("_published", True)}) > 1,
                             "rules": [_member(r["source_doc_id"], r) for r in group]})
    # 4. the same source text published twice within ONE document (identical or contained
    #    quotes under different citations, e.g. a repair record re-quoting a primary record)
    by_doc = defaultdict(list)
    for i, r in enumerate(rules):
        by_doc[r["source_doc_id"]].append(i)
    norms = [_norm(r["quoted_span"]) for r in rules]
    inner = [(i, j) for idx in by_doc.values() for a, i in enumerate(idx) for j in idx[a + 1:]
             if norms[i] in norms[j] or norms[j] in norms[i]]
    for comp in _components(inner, len(rules)):
        clusters.append({"type": "possible_semantic_duplicate", "basis": "one quote contains the other (different "
                         "passages: not merged automatically)", "documents": [rules[comp[0]]["source_doc_id"]],
                         "rules": [_member(rules[i]["source_doc_id"], rules[i]) for i in comp]})
    toks = [set(_norm(r["quoted_span"]).split()) for r in rules]
    contained = {frozenset(p) for p in inner}
    similar = [(i, j) for idx in by_doc.values() for a, i in enumerate(idx) for j in idx[a + 1:]
               if frozenset((i, j)) not in contained and rules[i]["category"] == rules[j]["category"]
               and _jaccard(toks[i], toks[j]) >= 0.5]
    for comp in _components(similar, len(rules)):
        clusters.append({"type": "possible_semantic_duplicate", "basis": "similar wording, word-set Jaccard >= 0.5 "
                         "(may also be distinct sibling provisions)", "documents": [rules[comp[0]]["source_doc_id"]],
                         "rules": [_member(rules[i]["source_doc_id"], rules[i]) for i in comp]})
    # 5. lexically overlapping quotes, same jurisdiction and category, different documents
    pairs = [(i, j) for i in range(len(rules)) for j in range(i + 1, len(rules))
             if rules[i]["source_doc_id"] != rules[j]["source_doc_id"]
             and rules[i]["jurisdiction"] == rules[j]["jurisdiction"] and rules[i]["category"] == rules[j]["category"]
             and _jaccard(toks[i], toks[j]) >= 0.5]
    for comp in _components(pairs, len(rules)):
        clusters.append({"type": "lexically_overlapping_quotes", "threshold": "word-set Jaccard >= 0.5",
                         "rules": [_member(rules[i]["source_doc_id"], rules[i]) for i in comp]})
    return {"note": ("Audit only: likely duplicates and overlaps found by deterministic text and citation "
                     "matching, over published records and held (unpublished) records. Nothing was merged or "
                     "removed beyond the pipeline's exact same-source suppression ('suppressed_exact_duplicate'); "
                     "any later deduplication must keep provenance and temporal distinctions (status, "
                     "effective_date). 'possible_semantic_duplicate' clusters quote DIFFERENT passages and are "
                     "never merged automatically; some are distinct sibling provisions with parallel wording. "
                     "Not legal advice."),
            "counts": dict(Counter(c["type"] for c in clusters)),
            "counts_with_two_or_more_published": dict(Counter(
                c["type"] for c in clusters if sum(m["published"] for m in c["rules"]) >= 2)),
            "clusters": clusters}


# ---------------------------------------------------------------- corpus summary

def corpus_summary(audits: list[dict[str, Any]], unprocessed: list[dict[str, Any]], runs: dict[str, ExtractionRun],
                   ledger: list[dict[str, Any]], rules: list[dict[str, Any]], discovered: int,
                   runner: dict[str, Any] | None) -> dict[str, Any]:
    cands = [c for run in runs.values() for c in run.candidates]
    accepted = [c for c in cands if c.accepted]
    held = [c for c in cands if c.held]
    historical = [c for c in cands if c.historical]
    suppressed = [c for c in cands if c.duplicate_of is not None]
    evidence = Counter(chk.status for c in cands
                       for chk in (c.status_evidence, c.version_evidence, c.effective_date_evidence) if chk)
    targets = [t for run in runs.values() if run.repair is not None for t in run.repair.targets]
    mappings = [m for run in runs.values() for m in run.scope_mappings]
    conds = [g for run in runs.values() for g in run.global_scope]
    rel = [c.temporal.get("relative_resolution") for c in cands if c.temporal.get("relative_resolution")]
    per_doc = [a["estimated_cost_usd"]["total"] for a in audits if a["primary_provider_calls"]]
    tok = {k: sum((e.get("usage") or {}).get(k) or 0 for e in ledger)
           for k in ("input_tokens", "output_tokens", "thinking_tokens")}
    cost = {k: round(sum(e["estimated_cost_usd"] for e in ledger if e["pass"] == k), 6) for k in ("primary", "repair")}
    failures = [r for r in (runner or {}).get("documents", []) if r["run_mode"] == "error"
                and str(r.get("detail", "")).startswith("provider")]
    return {
        "disclaimer": "Not legal advice. " + DISCLAIMER,
        "pricing_usd_per_million_tokens": PRICE_PER_MTOK,
        "documents": {
            "supplied_text_discovered": discovered, "processed_with_artifact": len(audits),
            "processed_live": sum(a["primary_provider_calls"] > 0 for a in audits),
            "resumed_or_cache_hit": sum(bool(a["primary_cache_hit"]) for a in audits),
            "complete": sum(a["document_status"] == "complete" for a in audits),
            "review_required": sum(a["document_status"] == "review_required" for a in audits),
            "provider_failures": [r["doc_id"] for r in failures],
            "unprocessed": unprocessed},
        "rules": {"total_candidates": len(cands), "accepted": len(accepted),
                  "rejected": len(cands) - len(accepted) - len(held) - len(historical) - len(suppressed),
                  "held": len(held), "historical": len(historical), "suppressed_exact_duplicates": len(suppressed),
                  "final_publishable_records": len(rules)},
        "citations": {"exact_match": sum(c.citation.status == "exact_match" for c in accepted),
                      "normalized_match": sum(c.citation.status == "normalized_match" for c in accepted),
                      "layout_normalized_match": sum(c.citation.status == "layout_normalized_match"
                                                     for c in accepted),
                      "verifier_all_candidate_quotes": dict(Counter(c.citation.status for c in cands if c.citation)),
                      "verifier_all_status_version_date_evidence": dict(evidence),
                      "reconstructed_cross_page": sum(c.citation.reconstructed for c in accepted),
                      "failures_among_candidates": sum(c.citation is not None and c.citation.status == "failed"
                                                       for c in cands),
                      "failures_among_published": 0 if all(c.citation and c.citation.status != "failed"
                                                           for c in accepted) else "SEE AUDIT"},
        "repairs": {
            "documents_requiring_repair": sum(a["repair_invoked"] for a in audits),
            "total_repair_targets": len(targets),
            "targets_by_source_type": {**dict(Counter(s for t in targets for s in t.get("sources") or [])),
                                       "scope_mapping_challenge": len(mappings)},
            "accepted_rule_resolutions": sum(t.get("final_resolution") == "resolved_by_accepted_rule"
                                             for t in targets),
            "verified_out_of_scope": sum(t.get("final_resolution") == "resolved_out_of_scope" for t in targets),
            "unresolved_targets": sum(t.get("final_resolution") == "unresolved" for t in targets),
            "scope_applies": sum(m.get("final") == "applies" for m in mappings),
            "scope_does_not_apply": sum(m.get("final") == "does_not_apply" for m in mappings),
            "scope_uncertain": sum((m.get("decision") or {}).get("decision") == "uncertain" for m in mappings),
            "scope_unresolved": sum(m.get("final") == "unresolved" for m in mappings)},
        "temporal": {
            "explicit_dates": sum(c.temporal.get("applied_kind") == "explicit_operative_date"
                                  and c.rule["effective_date"] is not None for c in accepted),
            "resolved_relative_dates": sum(r.get("outcome") == "resolved" for r in rel),
            "unresolved_relative_dates": sum(r.get("outcome") != "resolved" for r in rel),
            "legislative_session_expiration_decisions": sum(c.legislative.get("status_basis") == "session_expired"
                                                            for c in cands),
            "legislative_decisions_by_basis": dict(Counter(c.legislative.get("status_basis") for c in cands
                                                           if c.legislative)),
            "in_force_records": sum(c.rule["status"] == "in_force" for c in accepted),
            "pending_records": sum(c.rule["status"] == "pending" for c in accepted),
            "failed_records": sum(c.rule["status"] == "failed" for c in accepted),
            "not_yet_effective_records": sum(c.rule["status"] == "not_yet_effective" for c in accepted),
            "held_temporal_records": len(held),
            "historical_records": dict(Counter(c.temporal_state for c in historical)),
            "published_with_future_end_boundary": sum(bool(c.validity.get("end_exclusive")) for c in accepted),
            "held_by_validity_window": sum((c.status_derivation or "").startswith("temporal resolution required")
                                           and "validity" not in (c.status_derivation or "")
                                           and ("end boundar" in (c.status_derivation or "")
                                                or "version/history evidence has a date" in (c.status_derivation or ""))
                                           for c in held),
            "validity_resolver": "validity-window/v1"},
        "scope": {
            "conditions_by_mode": dict(Counter(str((g.get("scope") or {}).get("mode")) for g in conds)),
            "structural_mappings": sum((g.get("scope") or {}).get("mode") == "structural" and g["propagated"]
                                       for g in conds),
            "explicit_reference_mappings": sum((g.get("scope") or {}).get("mode") == "explicit_reference"
                                               and g["propagated"] for g in conds),
            "named_subject_challenges": len(mappings),
            "unresolved_scope_mappings": sum(m.get("final") == "unresolved" for m in mappings),
            "conditions_not_propagated": sum(not g["propagated"] for g in conds)},
        "cost": {
            "provider_primary_calls": sum(e["pass"] == "primary" for e in ledger),
            "provider_repair_calls": sum(e["pass"] == "repair" for e in ledger),
            "primary_cache_hits": sum(bool(a["primary_cache_hit"]) for a in audits),
            "repair_cache_hits": sum(bool(a["repair_cache_hit"]) for a in audits),
            "tokens": tok,
            "tokens_by_pass": {k: _usage(ledger, k) for k in ("primary", "repair")},
            "estimated_primary_cost_usd": cost["primary"], "estimated_repair_cost_usd": cost["repair"],
            "estimated_total_new_spend_usd": round(cost["primary"] + cost["repair"], 6),
            "per_document_new_cost_usd": {"documents": len(per_doc),
                                          "mean": round(mean(per_doc), 6) if per_doc else None,
                                          "median": round(median(per_doc), 6) if per_doc else None,
                                          "max": round(max(per_doc), 6) if per_doc else None}},
        "runner_stop_reason": (runner or {}).get("stop_reason"),
        "per_document_audit": audits,
    }


def _state(c: dict[str, Any]) -> str:
    if c.get("accepted"):
        return "published"
    if c.get("historical"):
        return f"historical:{c.get('temporal_state')}"
    if c.get("duplicate_of") is not None:
        return "suppressed_exact_duplicate"
    return "held" if c.get("held") else "rejected"


def baseline_delta(baseline_dir: Path, runs: dict[str, ExtractionRun]) -> dict[str, Any]:
    """Every candidate whose publishability differs from the baseline artifact of its document."""
    changes, counts = [], Counter()
    for doc_id, run in sorted(runs.items()):
        path = baseline_dir / "documents" / f"{doc_id}_extraction.json"
        if not path.is_file():
            continue
        old = json.loads(path.read_text(encoding="utf-8"))["candidates"]
        new = [c.model_dump() for c in run.candidates]
        for a, b in zip(old, new):
            before, after = _state(a), _state(b)
            if before == after:
                continue
            reason = ("recovered: temporal (validity window)" if before == "rejected" and any(
                          "version annotation is dated" in x for x in a["rejection_reasons"]) else
                      "recovered: citation (layout-only whitespace)" if before in ("rejected", "held") and any(
                          x.startswith("citation:") for x in a["rejection_reasons"]) else
                      "removed: expired (historical)" if after.startswith("historical") else
                      "removed: exact same-source duplicate" if after == "suppressed_exact_duplicate" else "other")
            counts[(before, after, reason)] += 1
            rule = b.get("rule") or {}
            changes.append({"doc_id": doc_id, "candidate": b["index"], "before": before, "after": after,
                            "reason": reason, "team_rule_id": rule.get("team_rule_id"),
                            "citation": rule.get("citation"), "status": rule.get("status"),
                            "effective_date": rule.get("effective_date"),
                            "duplicate_of": b.get("duplicate_of"), "temporal_state": b.get("temporal_state")})
    return {"baseline": _display(baseline_dir) if baseline_dir.is_absolute() else baseline_dir.as_posix(),
            "counts": {" | ".join(k): v for k, v in sorted(counts.items())}, "changes": changes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline post-run report for a corpus extraction stage.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--unprocessed", action="append", default=[],
                        help="DOC=reason for a supplied-text document deliberately not sent")
    parser.add_argument("--baseline-dir", type=Path, help="an earlier stage directory to compare publishability with")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    notes = dict(x.split("=", 1) for x in args.unprocessed)
    manifest = {d["doc_id"]: d for d in supplied_documents()}
    ledger_path = args.run_dir / "spend_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))["requests"] if ledger_path.is_file() else []
    runner_path = summary_path(args.run_dir)
    runner = json.loads(runner_path.read_text(encoding="utf-8")) if runner_path.is_file() else None
    runner_rows = {r["doc_id"]: r for r in (runner or {}).get("documents", [])}

    runs: dict[str, ExtractionRun] = {}
    bodies: dict[str, str] = {}
    audits, unprocessed = [], []
    for doc_id in sorted(manifest):
        bodies[doc_id] = load_source(doc_id).body
        path = args.run_dir / "documents" / f"{doc_id}_extraction.json"
        if not path.is_file():
            row = runner_rows.get(doc_id, {})
            unprocessed.append({"doc_id": doc_id, "jurisdiction": manifest[doc_id]["jurisdiction"],
                                "source_chars": len(bodies[doc_id]),
                                "reason": notes.get(doc_id) or row.get("detail") or "no artifact"})
            continue
        runs[doc_id] = ExtractionRun.model_validate_json(path.read_text(encoding="utf-8"))
        audits.append(document_audit(runs[doc_id], ledger, bodies[doc_id]))

    validator = make_rule_validator(sp.read_json(sp.REPO_ROOT / sp.SCHEMA_PATH))
    rules, problems = build_rules(runs, bodies, manifest, validator)
    payload = {"rules": rules}
    assert rule_records(payload) == rules
    _write(args.run_dir / "rules.json", payload)

    summary = corpus_summary(audits, unprocessed, runs, ledger, rules, len(manifest), runner)
    summary["rules_json"] = {"path": "rules.json", "records": len(rules),
                             "documents_contributing": len({r["source_doc_id"] for r in rules}),
                             "sha256": hashlib.sha256((args.run_dir / "rules.json").read_bytes()).hexdigest(),
                             **{k: len(v) for k, v in problems.items()}, "problems": problems}
    _write(args.run_dir / "full_corpus_summary.json", summary)

    queue = [{"doc_id": a["doc_id"], "jurisdiction": a["jurisdiction"], "posture": a["posture"],
              "accepted": a["accepted"], "held": a["held"], "rejected": a["rejected"],
              "codes": a["review_codes"], "reasons": review_codes(runs[a["doc_id"]]),
              "informational_flags": (["source_summary_only"] if a["source_basis"]
                                      and set(a["source_basis"]) <= SUMMARY_BASES else [])}
             for a in audits if a["document_status"] == "review_required"]
    _write(args.run_dir / "review_queue.json",
           {"note": "Documents needing human review. Not resolved during the run. Not legal advice.",
            "documents": len(queue), "code_counts": dict(Counter(c for q in queue for c in q["codes"])),
            "queue": queue})

    held = [{**c.rule, "_published": False} for d in sorted(runs) for c in runs[d].candidates if c.held]
    dup = duplicate_audit(rules + held, {d: bodies[d] for d in runs}, runs)
    _write(args.run_dir / "duplicate_clusters.json", dup)

    if args.baseline_dir is not None:
        delta = baseline_delta(args.baseline_dir, runs)
        _write(args.run_dir / "delta_vs_baseline.json", delta)
        print(f"delta vs {args.baseline_dir}: {delta['counts']}")
    d, r = summary["documents"], summary["rules"]
    print(f"documents: discovered {d['supplied_text_discovered']}  with artifact {d['processed_with_artifact']}  "
          f"live {d['processed_live']}  cache-hit {d['resumed_or_cache_hit']}  complete {d['complete']}  "
          f"review_required {d['review_required']}  unprocessed {[u['doc_id'] for u in unprocessed]}")
    print(f"rules: candidates {r['total_candidates']}  accepted {r['accepted']}  rejected {r['rejected']}  "
          f"held {r['held']}  rules.json {len(rules)}  problems { {k: len(v) for k, v in problems.items()} }")
    print(f"cost: ${summary['cost']['estimated_total_new_spend_usd']:.4f} (estimate)  duplicates {dup['counts']}")
    return 0 if not any(problems.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
