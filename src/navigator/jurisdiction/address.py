"""Compare a submitted address with the address the Census geocoder matched.

Audit only: nothing here rewrites the submitted address or what is sent to Census. The
comparison classifies each difference as **minor** (another spelling of the same address) or
**meaningful** (the match may be a different address). A meaningful difference on a Non_Exact
match makes the resolution `review_required`.

- House number: equal is fine. An endpoint of a submitted range ("876-878" matched as 876) is
  minor. Anything else, including a missing number, is meaningful.
- Street: compared after these spelling equivalences only:
  - case and punctuation;
  - USPS Publication 28 suffix (Appendix C1) and directional (Appendix C2) abbreviations;
  - ordinal words and leading zeros ("SEVENTH" = "05TH" style -> "7TH", "5TH");
  - a leading "ST" / "MT" / "FT" = "SAINT" / "MOUNT" / "FORT";
  - a trailing secondary-unit designator (Appendix C2: "APT 0001", "LOT 2A-13", "#5").
  A suffix present on only one side is minor. A directional present on only one side, a
  different suffix or a different name is meaningful: those can be different streets.
- Locality: a different city or ZIP alone is minor (postal names and ZIP boundaries do not
  follow municipal lines). Both different, when both were submitted and the ZIP is plausible
  for the state, is meaningful.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# USPS Publication 28, Appendix C1 (street suffixes): common variants -> standard abbreviation.
_SUFFIX_VARIANTS = {
    "ALY": ("ALLEY", "ALLEE", "ALLY"),
    "AVE": ("AVENUE", "AV", "AVEN", "AVENU", "AVN", "AVNUE"),
    "BLVD": ("BOULEVARD", "BOUL", "BOULV"),
    "CIR": ("CIRCLE", "CIRC", "CIRCL", "CRCL", "CRCLE"),
    "CT": ("COURT",),
    "DR": ("DRIVE", "DRIV", "DRV"),
    "EXT": ("EXTENSION", "EXTN", "EXTNSN"),
    "HTS": ("HEIGHTS", "HT"),
    "HWY": ("HIGHWAY", "HIGHWY", "HIWAY", "HIWY", "HWAY"),
    "LN": ("LANE",),
    "PARK": ("PRK",),
    "PKWY": ("PARKWAY", "PARKWY", "PKWAY", "PKY"),
    "PL": ("PLACE",),
    "PLZ": ("PLAZA", "PLZA"),
    "RD": ("ROAD",),
    "ROW": (),
    "SQ": ("SQUARE", "SQR", "SQRE", "SQU"),
    "ST": ("STREET", "STRT", "STR"),
    "TER": ("TERRACE", "TERR"),
    "WALK": (),
    "WAY": ("WY",),
}
SUFFIXES = {v: std for std, variants in _SUFFIX_VARIANTS.items() for v in (std, *variants)}
# USPS Publication 28, Appendix C2 (directionals).
DIRECTIONALS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE", "NORTHWEST": "NW",
                "SOUTHEAST": "SE", "SOUTHWEST": "SW", **{d: d for d in ("N", "S", "E", "W", "NE", "NW", "SE", "SW")}}
# USPS Publication 28, Appendix C2 (secondary unit designators), the forms that take a number.
UNIT_DESIGNATORS = ("APT", "BLDG", "DEPT", "FL", "HNGR", "LOT", "PIER", "RM", "SLIP", "SPC", "STE", "STOP", "TRLR",
                    "UNIT")
_ORDINAL_WORDS = ("FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH", "SEVENTH", "EIGHTH", "NINTH", "TENTH",
                  "ELEVENTH", "TWELFTH", "THIRTEENTH", "FOURTEENTH", "FIFTEENTH", "SIXTEENTH", "SEVENTEENTH",
                  "EIGHTEENTH", "NINETEENTH", "TWENTIETH")


def _ordinal(n: int) -> str:
    return f"{n}{'TH' if 10 <= n % 100 <= 20 else {1: 'ST', 2: 'ND', 3: 'RD'}.get(n % 10, 'TH')}"


ORDINALS = {w: _ordinal(i) for i, w in enumerate(_ORDINAL_WORDS, start=1)}
NAME_PREFIXES = {"ST": "SAINT", "MT": "MOUNT", "FT": "FORT"}

_NUM = r"\d+(?:\.\d+)?[A-Z]?"
_HOUSE = re.compile(rf"^({_NUM})(?:\s*-\s*({_NUM}))?\s+(\S.*)$")
_UNIT = re.compile(rf"\s+(?:(?:{'|'.join(UNIT_DESIGNATORS)})\s+\S+|#\s*\S+)$")
_ZERO_ORDINAL = re.compile(r"^0+(\d+(?:ST|ND|RD|TH))$")


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).upper()
    text = re.sub(r",|(?<!\d)\.|\.(?!\d)", " ", text)         # keep decimal house numbers ("14.5")
    return re.sub(r"\s+", " ", text).strip()


def _token(t: str) -> str:
    m = _ZERO_ORDINAL.match(t)
    return m.group(1) if m else ORDINALS.get(t, t)


@dataclass(frozen=True)
class Street:
    low: str | None              # house number, or the low end of a range
    high: str | None             # high end of a range, else None
    predir: str | None
    name: tuple[str, ...]        # normalized street-name tokens
    suffix: str | None           # USPS standard suffix, if one is present
    postdir: str | None
    unit: str | None             # trailing secondary unit, as written

    @property
    def full_name(self) -> tuple[str, ...]:
        return tuple(t for t in (self.predir, *self.name, self.postdir) if t)


def parse_street(text: str) -> Street:
    cleaned = _clean(text)
    unit = None
    m = _UNIT.search(cleaned)
    if m and len(cleaned[:m.start()].split()) >= 2:
        unit, cleaned = m.group(0).strip(), cleaned[:m.start()]
    m = _HOUSE.match(cleaned)
    low, high, rest = (m.group(1), m.group(2), m.group(3)) if m else (None, None, cleaned)
    tokens = [_token(t) for t in rest.split()]
    predir = postdir = suffix = None
    if len(tokens) > 1 and tokens[0] in DIRECTIONALS:
        predir = DIRECTIONALS[tokens.pop(0)]
    if len(tokens) > 1 and tokens[-1] in DIRECTIONALS:
        postdir = DIRECTIONALS[tokens.pop()]
    if len(tokens) > 1 and tokens[-1] in SUFFIXES:
        suffix = SUFFIXES[tokens.pop()]
    if len(tokens) > 1 and tokens[0] in NAME_PREFIXES:
        tokens[0] = NAME_PREFIXES[tokens[0]]
    return Street(low, high, predir, tuple(tokens), suffix, postdir, unit)


@dataclass(frozen=True)
class MatchedAddress:
    street: str
    city: str
    state: str
    zip: str


def split_matched(matched: str) -> MatchedAddress:
    """`4600 SILVER HILL RD, WASHINGTON, DC, 20233` -> street, city, state, ZIP."""
    parts = [p.strip() for p in matched.split(",")]
    if len(parts) < 4:
        return MatchedAddress(matched.strip(), "", "", "")
    return MatchedAddress(", ".join(parts[:-3]), parts[-3], parts[-2], parts[-1])


@dataclass
class Comparison:
    minor: list[str] = field(default_factory=list)
    meaningful: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {"minor": list(self.minor), "meaningful": list(self.meaningful)}


def same_place_name(a: str, b: str) -> bool:
    return _clean(a) == _clean(b)


def compare(street: str, city: str, zip_code: str, matched: str, *, zip_plausible: bool = True) -> Comparison:
    """Differences between the submitted components (as sent in the attempt) and the match."""
    out = Comparison()
    m = split_matched(matched)
    given, found = parse_street(street), parse_street(m.street)
    if given.low is None:
        out.meaningful.append("house_number_missing_in_input")
    elif (found.low, found.high) == (given.low, given.high):
        pass
    elif given.high is not None and found.high is None and found.low in (given.low, given.high):
        out.minor.append(f"house_number_range_endpoint ({given.low}-{given.high} -> {found.low})")
    else:
        out.meaningful.append(f"house_number_differs ({street.split(' ')[0]} -> {m.street.split(' ')[0]})")
    if given.unit:
        out.minor.append(f"secondary_unit_in_input ({given.unit})")
    if given.name != found.name:
        out.meaningful.append(f"street_name_differs ({' '.join(given.full_name)} -> {' '.join(found.full_name)})")
    else:
        if (given.predir, given.postdir) != (found.predir, found.postdir):
            out.meaningful.append(f"street_directional_differs ({' '.join(given.full_name)} -> "
                                  f"{' '.join(found.full_name)})")
        if given.suffix != found.suffix:
            if given.suffix and found.suffix:
                out.meaningful.append(f"street_suffix_differs ({given.suffix} -> {found.suffix})")
            else:
                out.minor.append(f"street_suffix_only_on_one_side ({given.suffix or '-'} -> {found.suffix or '-'})")
    city_differs = bool(city) and not same_place_name(city, m.city)
    zip_differs = bool(zip_code) and zip_code != m.zip
    if city_differs:
        out.minor.append(f"city_differs ({city} -> {m.city})")
    if zip_differs:
        out.minor.append(f"zip_differs ({zip_code} -> {m.zip}){'' if zip_plausible else ' [input ZIP outside state]'}")
    if city_differs and zip_differs and zip_plausible:
        out.meaningful.append("city_and_zip_both_differ")
    return out
