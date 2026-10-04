"""Census geography -> canonical corpus jurisdiction.

The candidate canonical jurisdictions come from the corpus itself: the `jurisdictions` column
of the manifest and the `jurisdiction` of every published rule, written "CA" (state) or
"Los Angeles, CA" (local). No national city list is used.

A Census incorporated place maps to a corpus local jurisdiction when its Census BASENAME and
its state equal the corpus jurisdiction's name and state. BASENAME is Census's own name field
without the legal/statistical area description (NAME "Los Angeles city", BASENAME
"Los Angeles"; NAME "Jersey City city", BASENAME "Jersey City"), so no suffix is ever stripped
from a string. A place that matches no corpus jurisdiction keeps the canonical form
"<BASENAME>, <ST>" and is marked `in_corpus=False`: it has no local rules in this corpus.
Mappings are keyed by place GEOID. Two GEOIDs claiming one canonical name is an ambiguity
that sends the affected addresses to review.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from navigator.jurisdiction.census import Area

JURISDICTION_RE = re.compile(r"^(?:(?P<local>[A-Za-z .'-]+), )?(?P<state>[A-Z]{2})$")


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name)).strip().casefold()


@dataclass(frozen=True)
class CorpusJurisdiction:
    name: str                      # exactly as written in the corpus
    state: str
    local: str | None              # None for a state-level jurisdiction
    manifest_docs: tuple[str, ...]
    published_rules: int


def corpus_jurisdictions(manifest_rows: Iterable[dict[str, str]], rules: Iterable[dict[str, Any]]
                         ) -> dict[str, CorpusJurisdiction]:
    docs: dict[str, list[str]] = {}
    for row in manifest_rows:
        docs.setdefault(row["jurisdictions"].strip(), []).append(row["doc_id"])
    counts = Counter(r["jurisdiction"] for r in rules)
    out: dict[str, CorpusJurisdiction] = {}
    for name in sorted(set(docs) | set(counts)):
        m = JURISDICTION_RE.match(name)
        if not m:
            raise ValueError(f"corpus jurisdiction {name!r} is not 'ST' or 'City, ST'")
        out[name] = CorpusJurisdiction(name, m["state"], m["local"], tuple(sorted(docs.get(name, ()))),
                                       counts.get(name, 0))
    return out


@dataclass(frozen=True)
class CrosswalkEntry:
    census_place_geoid: str
    census_place_name: str
    census_basename: str
    census_lsadc: str
    census_funcstat: str
    state: str
    canonical_jurisdiction: str
    in_corpus: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Crosswalk:
    def __init__(self, corpus: dict[str, CorpusJurisdiction]) -> None:
        self.corpus = corpus
        self._local = {(j.state, _key(j.local)): j for j in corpus.values() if j.local}
        self._states = {j.state: j for j in corpus.values() if j.local is None}
        self.entries: dict[str, CrosswalkEntry] = {}

    def state(self, stusab: str) -> tuple[str, bool]:
        """State jurisdiction in corpus form ("CA") and whether the corpus has it."""
        return stusab, stusab in self._states

    def place(self, area: Area, stusab: str) -> CrosswalkEntry:
        if area.geoid in self.entries:
            return self.entries[area.geoid]
        hit = self._local.get((stusab, _key(area.basename)))
        if hit:
            entry = CrosswalkEntry(area.geoid, area.name, area.basename, area.lsadc, area.funcstat, stusab,
                                   hit.name, True,
                                   f"Census incorporated place BASENAME '{area.basename}' in {stusab} equals corpus "
                                   f"jurisdiction '{hit.name}' ({len(hit.manifest_docs)} manifest docs, "
                                   f"{hit.published_rules} published rules)")
        else:
            entry = CrosswalkEntry(area.geoid, area.name, area.basename, area.lsadc, area.funcstat, stusab,
                                   f"{area.basename}, {stusab}", False,
                                   "incorporated place not represented in the corpus: canonical name from Census "
                                   "BASENAME and state; no local rules")
        self.entries[area.geoid] = entry
        return entry

    def ambiguous(self) -> set[str]:
        """Canonical names claimed by more than one Census GEOID."""
        counts = Counter(e.canonical_jurisdiction for e in self.entries.values())
        return {name for name, n in counts.items() if n > 1}

    def table(self) -> list[dict[str, Any]]:
        return [self.entries[g].as_dict() for g in sorted(self.entries)]
