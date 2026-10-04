"""Rule applicability for one address (M5). Deterministic; no LLM.

Order of evaluation, per (address, rule):

1. status `failed` -> never surfaced;
2. jurisdiction: the rule's jurisdiction must equal the address's resolved state jurisdiction
   or its resolved local jurisdiction (M4; postal_city is never read). An unresolved address
   matches no rule set, so every surfaceable rule is `unknown` (jurisdiction_unresolved);
3. temporal status from the published record (status, effective_date vs as_of);
4. coverage clauses (AND), 5. exemption clauses (OR), against the available property facts;
6. result: coverage False or exemption True -> omitted (definitely not applicable);
   otherwise pending / not_yet_effective by status, else applies (all True) or unknown.

A published record whose extraction left a scope condition unresolved (M3 artifact:
`scope_mappings[].final == "unresolved"`, e.g. D027) is at best unknown: the condition exists
but was not attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from navigator.applicability.conditions import (EXTRACTION_SCOPE, FACT_OF_REASON, JURISDICTION_UNRESOLVED, F, T, U,
                                                and3, classify, evaluate_clause, or3, split_clauses)
from navigator.applicability.facts import PropertyFacts

ENGINE = "applicability/m5-v1"
APPLIES, UNKNOWN, SUPERSEDED, NOT_YET, PENDING = "applies", "unknown", "superseded", "not_yet_effective", "pending"
RESULTS = (APPLIES, UNKNOWN, SUPERSEDED, NOT_YET, PENDING)

_REASON_TEXT = {
    "certificate_of_occupancy_unknown": "the certificate-of-occupancy date",
    "owner_type_unknown": "the owner type",
    "owner_occupancy_unknown": "whether the owner occupies the property",
    "subsidized_status_unknown": "whether the housing is deed-restricted or subsidized",
    "local_ordinance_coverage_unknown": "whether a local ordinance covers the property",
    "local_program_coverage_unknown": "whether the unit is covered by the local program (RSO/JCO/rent control)",
    "rent_history_unknown": "the property's rent history",
    "replacement_unit_status_unknown": "whether the unit is a replacement unit",
    "units_missing": "the number of units",
    "year_built_missing": "the year built",
    "year_built_in_cutoff_year": "the exact build date (year built is the cutoff year)",
    "property_type_uncertain": "the property type",
    "operative_condition_unresolved": "an operative condition that has not been confirmed",
    "coverage_condition_unsupported": "a coverage condition the engine cannot evaluate",
    "exemption_condition_unsupported": "an exemption the engine cannot evaluate",
    "extraction_scope_condition_unresolved": "a scope condition left unresolved by extraction",
}


@dataclass
class RuleOutcome:
    team_rule_id: str
    result: str | None                       # None = omitted
    omitted_because: str | None
    reasons: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)
    causes: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    coverage: bool | None = None
    exempt: bool | None = None


def scope_gaps(artifacts: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """team_rule_id -> unresolved scope conditions recorded by M3 for its provisions."""
    out: dict[str, list[dict[str, str]]] = {}
    for art in artifacts:
        unresolved = [m for m in art.get("scope_mappings") or [] if m.get("final") == "unresolved"]
        if not unresolved:
            continue
        for c in art.get("candidates") or []:
            rid = (c.get("rule") or {}).get("team_rule_id")
            if not rid:
                continue
            for m in unresolved:
                if m["provision_id"] in (c.get("provision_ids") or []):
                    out.setdefault(rid, []).append({"condition_id": m["condition_id"], "kind": m["kind"],
                                                    "statement": m["statement"], "target": m.get("target_ref", "")})
    return out


def temporal(rule: dict[str, Any], as_of: date) -> str:
    status = rule["status"]
    eff = rule.get("effective_date")
    if status == "in_force" and eff and date.fromisoformat(eff) > as_of:
        return "not_yet_effective"
    return status


def _where(rule: dict[str, Any]) -> str:
    return rule["jurisdiction"]


def evaluate(rule: dict[str, Any], facts: PropertyFacts, jurisdiction: dict[str, Any], as_of: date,
             gaps: list[dict[str, str]] | None = None) -> RuleOutcome:
    rid = rule["team_rule_id"]
    status = temporal(rule, as_of)
    if status == "failed":
        return RuleOutcome(rid, None, "status_failed")
    state, local = jurisdiction.get("state_jurisdiction"), jurisdiction.get("local_jurisdiction")
    if not state and not local:
        out = RuleOutcome(rid, UNKNOWN, None, [JURISDICTION_UNRESOLVED], ["jurisdiction"],
                          [{"role": "jurisdiction", "clause": "jurisdiction not resolved by M4",
                            "reasons": [JURISDICTION_UNRESOLVED]}])
        out.explanation = (f"Unknown: this address's jurisdiction could not be resolved from Census evidence, so it is "
                           f"not known whether this {_where(rule)} rule covers it.")
        return out
    if rule["jurisdiction"] not in (state, local):
        return RuleOutcome(rid, None, "other_jurisdiction")

    cov = [evaluate_clause(classify(t, "coverage", as_of), facts) for t in split_clauses(rule.get("coverage_conditions"))]
    exe = [evaluate_clause(classify(t, "exemption", as_of), facts) for t in split_clauses(rule.get("exemptions"))]
    coverage = and3(r.value for r in cov) if cov else T
    exempt = or3(r.value for r in exe) if exe else F
    causes = [{"role": r.clause.role, "clause": r.clause.text, "reasons": r.reasons} for r in cov + exe
              if r.value is U]
    if gaps:
        coverage = and3([coverage, U])
        causes += [{"role": "coverage" if g["kind"] == "coverage_condition" else "exemption",
                    "clause": f"{g['condition_id']}: {g['statement']} ({g['target']})", "reasons": [EXTRACTION_SCOPE]}
                   for g in gaps]
    out = RuleOutcome(rid, None, None, coverage=coverage, exempt=exempt)
    if coverage is F:
        clause = next(r.clause.text for r in cov if r.value is F)
        out.omitted_because = f"coverage_false: {clause[:120]}"
        return out
    if exempt is T:
        clause = next(r.clause.text for r in exe if r.value is T)
        out.omitted_because = f"exempt: {clause[:120]}"
        return out
    reasons = sorted({x for c in causes for x in c["reasons"]})
    out.reasons, out.causes = reasons, causes
    out.missing_facts = sorted({FACT_OF_REASON[x] for x in reasons if x in FACT_OF_REASON})
    where = _where(rule)
    if status == "pending":
        out.result = PENDING
        out.explanation = (f"Pending: a proposal for {where}, not law on {as_of.isoformat()}."
                           + (" Coverage would also depend on " + _list(reasons) + "." if reasons else ""))
    elif status == "not_yet_effective":
        out.result = NOT_YET
        out.explanation = (f"Not yet effective: enacted for {where}, effective {rule.get('effective_date')}."
                           + (" Coverage also depends on " + _list(reasons) + "." if reasons else ""))
    elif coverage is T and exempt is F:
        out.result = APPLIES
        out.explanation = f"Applies: in force on {as_of.isoformat()} in {where}" + _because(cov, exe, facts) + "."
    else:
        out.result = UNKNOWN
        out.explanation = (f"Unknown: in force in {where}, but whether it covers this property depends on "
                           f"{_list(reasons)}, which the data does not settle.")
    return out


def _list(reasons: list[str]) -> str:
    texts = [_REASON_TEXT.get(r, r) for r in reasons]
    return texts[0] if len(texts) == 1 else ", ".join(texts[:-1]) + " and " + texts[-1]


def _because(cov, exe, facts: PropertyFacts) -> str:
    bits = []
    decided = [r for r in cov if r.clause.kind == "property" and r.value is T]
    if decided:
        bits.append("its coverage condition is met (" + ", ".join(a.describe for r in decided
                                                                  for a in r.clause.atoms) + ")")
    ruled_out = [r for r in exe if r.clause.kind == "property" and r.value is F]
    if ruled_out:
        basis = ", ".join(x for x in (_units_text(facts), facts.use_class) if x) or "property facts"
        bits.append(f"{len(ruled_out)} property exemption(s) cannot apply ({basis})")
    return ("; " + "; ".join(bits)) if bits else ""


def _units_text(facts: PropertyFacts) -> str | None:
    if not facts.units_known:
        return None
    if facts.units_min == facts.units_max:
        return f"{facts.units_min} units"
    return f"{facts.units_min}{'+' if facts.units_max is None else '-' + str(facts.units_max)} units"
