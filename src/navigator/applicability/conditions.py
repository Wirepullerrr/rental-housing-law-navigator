"""Deterministic classification and three-valued evaluation of the condition text in
published RuleRecords (`coverage_conditions`, `exemptions`).

The text is split into clauses (the extractor joins clauses with "; "). Each clause is
classified by fixed patterns into one of:

- property atoms that the data can decide: unit counts, year built (only where the rule says
  "built"/"constructed"), and facility/condominium property types vs. an apartment building;
- property atoms that the data never decides: certificate of occupancy, owner type, owner
  occupancy, affordable/subsidy restriction, local-ordinance coverage, local program
  coverage (RSO, JCO, rent-controlled units), rent history, an unresolved operative condition;
- situational: the clause describes a tenancy, an event or a transaction (a termination
  ground, a notice, a lease date, a tenant's status, an algorithm product). It governs when
  the rule's duties arise and does not decide whether the rule covers the address;
- generic: a restatement of the rule's own scope ("applies to residential property ...");
- unsupported: anything else. It evaluates to unknown, never to true or false.

Values are True / False / None (unknown). Missing data is never coerced to False.
A clause is the AND of its atoms. Coverage is the AND of its clauses. Exemptions are the OR
of their clauses, and an exemption clause with no property atom (situational) does not
exempt the address as a whole.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

from navigator.applicability.facts import PropertyFacts

T, F, U = True, False, None

# unknown reasons (machine-readable)
JURISDICTION_UNRESOLVED = "jurisdiction_unresolved"
YEAR_BUILT_MISSING = "year_built_missing"
YEAR_BUILT_AT_CUTOFF = "year_built_in_cutoff_year"
UNITS_MISSING = "units_missing"
OWNER_TYPE = "owner_type_unknown"
OWNER_OCCUPANCY = "owner_occupancy_unknown"
CERT_OCCUPANCY = "certificate_of_occupancy_unknown"
SUBSIDIZED = "subsidized_status_unknown"
PROPERTY_TYPE = "property_type_uncertain"
LOCAL_ORDINANCE = "local_ordinance_coverage_unknown"
LOCAL_PROGRAM = "local_program_coverage_unknown"
RENT_HISTORY = "rent_history_unknown"
REPLACEMENT_UNIT = "replacement_unit_status_unknown"
OPERATIVE = "operative_condition_unresolved"
COVERAGE_UNSUPPORTED = "coverage_condition_unsupported"
EXEMPTION_UNSUPPORTED = "exemption_condition_unsupported"
EXTRACTION_SCOPE = "extraction_scope_condition_unresolved"

FACT_OF_REASON = {
    YEAR_BUILT_MISSING: "year_built", YEAR_BUILT_AT_CUTOFF: "year_built (cutoff year)", UNITS_MISSING: "units",
    OWNER_TYPE: "owner_type", OWNER_OCCUPANCY: "owner_occupancy", CERT_OCCUPANCY: "certificate_of_occupancy_date",
    SUBSIDIZED: "subsidy_or_deed_restriction", PROPERTY_TYPE: "property_type", LOCAL_ORDINANCE:
    "local_ordinance_coverage", LOCAL_PROGRAM: "local_program_coverage", RENT_HISTORY: "rent_history",
    REPLACEMENT_UNIT: "replacement_unit_status", JURISDICTION_UNRESOLVED: "jurisdiction",
}


@dataclass(frozen=True)
class Atom:
    kind: str
    reason: str                                   # the unknown reason when it cannot be decided
    evaluate: Callable[[PropertyFacts], bool | None]
    describe: str


@dataclass(frozen=True)
class Clause:
    role: str                    # "coverage" or "exemption"
    text: str
    kind: str                    # "property", "situational", "generic", "unsupported", "operative"
    atoms: tuple[Atom, ...] = ()
    any_of: bool = False         # atoms combined with OR (only the RSO "built ... or replacement units" form)


def and3(values) -> bool | None:
    values = list(values)
    if any(v is F for v in values):
        return F
    return T if all(v is T for v in values) else U


def or3(values) -> bool | None:
    values = list(values)
    if any(v is T for v in values):
        return T
    return F if all(v is F for v in values) else U


# ----------------------------------------------------------------- atom evaluators

def _never(_facts: PropertyFacts) -> None:
    return U


def units_cmp(op: str, n: int) -> Callable[[PropertyFacts], bool | None]:
    def f(p: PropertyFacts) -> bool | None:
        if not p.units_known:
            return U
        lo, hi = p.units_min, p.units_max
        if op == "<=":
            return T if hi is not None and hi <= n else (F if lo > n else U)
        if op == "==":
            if lo == hi:
                return lo == n
            return F if lo > n or (hi is not None and hi < n) else U
        if op == ">=":
            return T if lo >= n else (F if hi is not None and hi < n else U)
        raise ValueError(op)
    return f


def built(op: str, cutoff: date) -> Callable[[PropertyFacts], bool | None]:
    """Year built against a date. The cutoff year itself is unknown (month/day not known)."""
    def f(p: PropertyFacts) -> bool | None:
        if p.year_built is None or p.year_built == cutoff.year:
            return U
        before = p.year_built < cutoff.year
        return before if op in ("before", "on or before") else not before
    return f


def not_apartment_type(p: PropertyFacts) -> bool | None:
    """A facility/condominium/mobilehome type: False for an apartment building, else unknown."""
    return F if p.use_class in ("apartment", "subsidized_housing") else U


def separately_alienable(p: PropertyFacts) -> bool | None:
    return F if p.use_class == "apartment" and p.units_min is not None and p.units_min >= 2 else U


# ----------------------------------------------------------------- patterns

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = rf"({_MONTHS}) (\d{{1,2}}), (\d{{4}})"
_BUILT = re.compile(rf"\b(?:first )?(?:built|constructed) (on or before|before|after|on or after) {_DATE}", re.I)

_PRODUCT = re.compile(r"\b(product|software|algorithmic device|report, study|market research|appraisal)\b", re.I)
_OPERATIVE = re.compile(r"^Operative condition \(unresolved", re.I)
_CO = re.compile(r"certificate of occupancy|produced in the last 15 years", re.I)
_OWNER_TYPE = re.compile(r"natural person|\bREIT\b|real estate investment trust|corporat|family trust|\bLLC\b|"
                         r"mobilehome park management|own(?:s)? (?:only )?(?:no more than )?two (?:residential )?"
                         r"rental propert", re.I)
_OWNER_OCC = re.compile(r"owner[- ]occupied|owner-occupant|owner occupied one|(?:landlord|owner) occupied one|"
                        r"occupied by the landlord|principal residence|shares bathroom or kitchen", re.I)
_SUBSIDY = re.compile(r"affordable housing|deed[- ]restricted|restricted by deed|regulatory restriction|subsid", re.I)
_LOCAL_ORD = re.compile(r"local (?:just cause|rent)|local ordinance|local rent or price control", re.I)
_LOCAL_PROGRAM = re.compile(r"rent-controlled units|\bRSO\b|\bJCO\b|Protected Units?\b", re.I)
_RENT_HISTORY = re.compile(r"monthly rent charged on or before", re.I)
_FACILITY = re.compile(r"hospital|nursing|health facility|religious facilit|extended care|residential care|"
                       r"adult residential|dormitor|transitional housing|substance abuse|hotel|transient", re.I)
_MOBILEHOME_SUBJECT = re.compile(r"^(?:homeowners? of (?:a )?mobilehomes?|a homeowner of a mobilehome|"
                                 r"mobile ?homes? (?:subject|spaces))", re.I)
_CONDO = re.compile(r"condominium|townhome", re.I)
_SEPARATE = re.compile(r"separately alienable", re.I)
_SINGLE = re.compile(r"single-family", re.I)
_DUPLEX = re.compile(r"duplex|two-unit propert", re.I)
_ONE_TWO = re.compile(r"one-family or two-family", re.I)
_FOUR_PREMISES = re.compile(r"premises of not more than four", re.I)
_REPLACEMENT = re.compile(r"replacement units?", re.I)
_LIST_OF_TYPES = re.compile(r"certain property types, including|property types such as", re.I)

_SITUATIONAL = re.compile(
    r"\b(when|if|upon|after|prior to|tenan\w*|lease\w*|notice|terminat\w*|evict\w*|occupan\w*|occupies|occupy|"
    r"service members?|transfer|inspection|repairs?|request|violation|demoli\w*|remodel|relocation|vacat\w*|"
    r"move-in|fees?|screening|applicant|security collected|interest|rent due|payment|cosmetic|death|forfeit\w*|"
    r"vacancy|habitab\w*|grounds|vacation|short-term|credit|mortgage|exceptions? provided|paragraphs?|"
    r"subdivision|subject to paragraphs)\b", re.I)
_GENERIC = re.compile(r"^(?:the ordinance )?applies to (?:all|any|security for|residential)|^residential units in|"
                      r"applies to all landlords", re.I)


def split_clauses(text: str | None) -> list[str]:
    if not text:
        return []
    return [c.strip() for c in re.split(r";\s+(?=[A-Z])", text) if c.strip()]


def _date(m) -> date:
    month = _MONTHS.split("|").index(m[2]) + 1
    return date(int(m[4]), month, int(m[3]))


def coo_age_proxy(years: int, as_of: date) -> Callable[[PropertyFacts], bool | None]:
    """README challenge-data proxy (M5.1), NOT a legal equivalence: for a test "certificate of
    occupancy issued within the previous N years", year_built stands in for the certificate
    date. cutoff = as_of - N years; built before the cutoff year -> False (older), after ->
    True (within N years), in the cutoff year or missing -> unknown."""
    cutoff_year = as_of.year - years

    def f(p: PropertyFacts) -> bool | None:
        if p.year_built is None or p.year_built == cutoff_year:
            return U
        return p.year_built > cutoff_year
    return f


_COO_AGE = re.compile(r"(?:certificate of occupancy within the previous|produced in the last) (\d+|fifteen) years",
                      re.I)


def classify(text: str, role: str, as_of: date | None = None) -> Clause:
    atoms: list[Atom] = []
    if _OPERATIVE.search(text):
        return Clause(role, text, "operative", (Atom("operative_condition", OPERATIVE, _never, text[:80]),))
    if role == "exemption" and _PRODUCT.search(text):
        return Clause(role, text, "situational")
    b = _BUILT.search(text)
    if b and _REPLACEMENT.search(text):        # "first built on or before D, replacement units, ..."
        when = _date(b)
        return Clause(role, text, "property", (
            Atom("year_built", YEAR_BUILT_MISSING, built(b[1].lower(), when), f"built {b[1]} {when}"),
            Atom("replacement_unit", REPLACEMENT_UNIT, _never, "replacement unit")), any_of=True)
    if b:
        when = _date(b)
        atoms.append(Atom("year_built", YEAR_BUILT_MISSING, built(b[1].lower(), when), f"built {b[1]} {when}"))
    age = _COO_AGE.search(text)
    if age and as_of is not None:
        n = 15 if age[1].lower() == "fifteen" else int(age[1])
        atoms.append(Atom("certificate_of_occupancy_proxy", CERT_OCCUPANCY, coo_age_proxy(n, as_of),
                          f"certificate of occupancy within {n} years (year_built proxy per README)"))
    elif _CO.search(text):
        atoms.append(Atom("certificate_of_occupancy", CERT_OCCUPANCY, _never, "certificate-of-occupancy date"))
    if _OWNER_TYPE.search(text):
        atoms.append(Atom("owner_type", OWNER_TYPE, _never, "owner type"))
    if _OWNER_OCC.search(text):
        atoms.append(Atom("owner_occupancy", OWNER_OCCUPANCY, _never, "owner occupancy"))
    if _SUBSIDY.search(text):
        atoms.append(Atom("subsidy_restriction", SUBSIDIZED, _never, "affordable/subsidy restriction"))
    if _LOCAL_ORD.search(text):
        atoms.append(Atom("local_ordinance", LOCAL_ORDINANCE, _never, "coverage by a local ordinance"))
    if _LOCAL_PROGRAM.search(text):
        atoms.append(Atom("local_program", LOCAL_PROGRAM, _never, "coverage by a local program (RSO/JCO/rent control)"))
    if _RENT_HISTORY.search(text):
        atoms.append(Atom("rent_history", RENT_HISTORY, _never, "rent history"))
    if _FACILITY.search(text) or _MOBILEHOME_SUBJECT.search(text) or _CONDO.search(text):
        atoms.append(Atom("property_type", PROPERTY_TYPE, not_apartment_type, "facility/condominium/mobilehome type"))
    if _SEPARATE.search(text):
        atoms.append(Atom("separately_alienable", PROPERTY_TYPE, separately_alienable, "separately alienable"))
    if _DUPLEX.search(text):
        atoms.append(Atom("units", UNITS_MISSING, units_cmp("==", 2), "exactly 2 units"))
    elif _SINGLE.search(text):
        atoms.append(Atom("units", UNITS_MISSING, units_cmp("==", 1), "a single-family dwelling"))
    if _ONE_TWO.search(text):
        atoms.append(Atom("units", UNITS_MISSING, units_cmp("<=", 2), "at most 2 units"))
    if _FOUR_PREMISES.search(text):
        atoms.append(Atom("units", UNITS_MISSING, units_cmp("<=", 4), "at most 4 units"))
    if atoms:
        # "does not apply to certain property types, including A, B, C": a list of alternatives
        return Clause(role, text, "property", tuple(atoms), any_of=bool(_LIST_OF_TYPES.search(text)))
    if _GENERIC.search(text):
        return Clause(role, text, "generic")
    if _SITUATIONAL.search(text):
        return Clause(role, text, "situational")
    return Clause(role, text, "unsupported", (Atom(
        "unsupported", COVERAGE_UNSUPPORTED if role == "coverage" else EXEMPTION_UNSUPPORTED, _never, text[:80]),))


@dataclass
class ClauseResult:
    clause: Clause
    value: bool | None
    reasons: list[str]


def evaluate_clause(c: Clause, facts: PropertyFacts) -> ClauseResult:
    if c.kind in ("situational", "generic"):
        return ClauseResult(c, T if c.role == "coverage" else F, [])
    values = [(a, a.evaluate(facts)) for a in c.atoms]
    value = (or3 if c.any_of else and3)(v for _, v in values)
    reasons = []
    if value is U:
        for a, v in values:
            if v is U:
                reason = a.reason
                if a.kind in ("year_built", "certificate_of_occupancy_proxy") and facts.year_built is not None:
                    reason = YEAR_BUILT_AT_CUTOFF
                if a.kind == "certificate_of_occupancy_proxy" and facts.year_built is None:
                    reason = YEAR_BUILT_MISSING
                if a.kind == "units" and facts.units_known:
                    reason = PROPERTY_TYPE
                reasons.append(reason)
    return ClauseResult(c, value, reasons)
