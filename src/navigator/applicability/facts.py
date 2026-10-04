"""Property facts available from data/sample_addresses.csv, and nothing else.

- year_built: the assessor's year built (never a certificate-of-occupancy date).
- units: the recorded unit count. When it is missing, a unit RANGE is taken only from a use
  description that states one in words ("Five or more apartments", "Apartment 5 to 14 Units",
  "APT 7-30 UNITS", "4-8-UNIT-APT"). Coded building descriptions such as the NJ MOD-IV
  "3S-F-D-6U-NH" are not parsed: the starter-pack audit warns against inferring units from them.
  A recorded count outside the stated range, or different from a unit token in a coded
  description ("13B-93U-2C-G"), is a conflict, and units become unknown.
- use_class: "apartment" when the use description (or the dataset's own label for the code,
  "Apartments (class 4C)") names an apartment/flat building; "subsidized_housing" for Boston's
  "SUBSD HOUSING"; otherwise unknown. Used only to rule out facility types (hospital, hotel,
  dormitory, mobilehome, condominium) that an apartment building is not.

Owner type, owner occupancy, certificate of occupancy, subsidy agreements, tenancy facts and
rent history are not in the data and are never approximated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
_RANGES = (
    (re.compile(r"\b(\w+) or more (?:apartments|units)\b", re.I), lambda m: (_n(m[1]), None)),
    (re.compile(r"\((\d+)\+ units\)", re.I), lambda m: (int(m[1]), None)),
    (re.compile(r"\b(\d+) to (\d+) units\b", re.I), lambda m: (int(m[1]), int(m[2]))),
    (re.compile(r"\b(\d+) units or more\b", re.I), lambda m: (int(m[1]), None)),
    (re.compile(r"\b(\d+) units or less\b", re.I), lambda m: (1, int(m[1]))),
    (re.compile(r"^(?:MXD )?(\d+)-(\d+)-UNIT-APT$", re.I), lambda m: (int(m[1]), int(m[2]))),
    (re.compile(r"^(?:MXD )?>(\d+)-UNIT-APT$", re.I), lambda m: (int(m[1]) + 1, None)),
    (re.compile(r"^APT (\d+)-(\d+) UNITS$", re.I), lambda m: (int(m[1]), int(m[2]))),
)
# Unit tokens in coded building descriptions ("3S-F-D-6U-NH"). Used ONLY to detect a conflict
# with a recorded count (which then becomes unknown), never to supply a unit count.
_CODED_UNITS = re.compile(r"(?<![A-Z0-9])(\d+)U(?=|-|G|/)")
_APARTMENT = re.compile(r"apartment|\bAPT\b|UNIT-APT|\bflats?\b|\(5\+ units\)", re.I)


def _n(word: str) -> int | None:
    return int(word) if word.isdigit() else _WORDS.get(word.lower())


@dataclass(frozen=True)
class PropertyFacts:
    address_id: str
    year_built: int | None
    units_min: int | None
    units_max: int | None
    units_source: str                    # "recorded", "use_description", "missing" or "conflict"
    use_class: str | None
    use_description: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def units_known(self) -> bool:
        return self.units_min is not None


def use_class(use_code: str, description: str, code_labels: dict[str, str]) -> str | None:
    if "SUBSD HOUSING" in description.upper():
        return "subsidized_housing"
    if _APARTMENT.search(description) or _APARTMENT.search(code_labels.get(use_code, "")):
        return "apartment"
    return None


def unit_range(description: str) -> tuple[int | None, int | None]:
    for rx, f in _RANGES:
        m = rx.search(description.strip())
        if m:
            lo, hi = f(m)
            if lo is not None:
                return lo, hi
    return None, None


def code_labels(rows: list[dict[str, str]]) -> dict[str, str]:
    """The dataset's own label for a use code, where some row spells it out
    (NJ: use_code 4C is described as "Apartments (class 4C)")."""
    labels: dict[str, str] = {}
    for r in rows:
        d = r["use_description"]
        if r["use_code"] and f"class {r['use_code']}" in d:
            labels[r["use_code"]] = d
    return labels


def facts_from_row(row: dict[str, str], labels: dict[str, str]) -> PropertyFacts:
    notes = []
    year = int(row["year_built"]) if re.fullmatch(r"\d{4}", row["year_built"] or "") else None
    lo, hi = unit_range(row["use_description"])
    if row["units"].isdigit() and int(row["units"]) > 0:
        n = int(row["units"])
        coded = {int(x) for x in _CODED_UNITS.findall(row["use_description"])}
        if coded and n not in coded:          # e.g. A0227: units 2, description "13B-93U-2C-G"
            notes.append(f"recorded units {n} conflict with unit token(s) {sorted(coded)} in "
                         f"'{row['use_description']}'")
            umin = umax = None
            source = "conflict"
        elif lo is not None and (n < lo or (hi is not None and n > hi)):
            notes.append(f"recorded units {n} conflict with use description '{row['use_description']}'")
            umin = umax = None
            source = "conflict"
        else:
            umin = umax = n
            source = "recorded"
    elif lo is not None:
        umin, umax, source = lo, hi, "use_description"
        notes.append(f"units {lo}{'-' + str(hi) if hi else '+'} from use description '{row['use_description']}'")
    else:
        umin = umax = None
        source = "missing"
    return PropertyFacts(row["address_id"], year, umin, umax, source,
                         use_class(row["use_code"], row["use_description"], labels), row["use_description"],
                         tuple(notes))
