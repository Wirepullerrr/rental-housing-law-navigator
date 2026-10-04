"""Sample address -> Census geography -> canonical legal jurisdiction (M4).

Inputs per address: only `street_address`, `postal_city`, `state` and `zip`. Property facts
(year built, units, use code) are never read here. `postal_city` is sent to the geocoder as a
search hint and compared afterwards as an audit signal. It is never the jurisdiction: the
legal municipality is the Census **incorporated place** that contains the matched point, or
an audited override.

Bounded attempt sequence. Every attempt is recorded, including skipped ones:

    A  street + postal_city + state + ZIP     (all rows, one batch)
    B  street + state + ZIP                   (rows A did not match; skipped without a ZIP)
    C  street + postal_city + state           (rows still unmatched; skipped without a ZIP:
                                               it would repeat A)

An attempt is usable when Census returns `Match` in the submitted state. The first usable
attempt is geolooked-up at its coordinates. A row whose attempts only tied is looked up once
more, with exactly the tied attempt's components, at the single-record endpoint. That lists
the tied candidates; it is not a new matching attempt. Nothing is invented: no ZIP or city is
fabricated, the street is never rewritten, and no other source is consulted.

Status:
- resolved: a usable Exact match, or a Non_Exact match with only minor differences; one
  active incorporated place; state and block consistent; no tie, conflict or ambiguity.
- review_required: a match with any of those problems, or a tie. Geography is reported but
  flagged. For a tie it is given only when every candidate lies in one incorporated place.
- unresolved: no usable match after the bounded sequence. No jurisdiction is given.

Audited overrides (`apply_overrides`) name the Census evidence they correct, and they are
re-verified on every run by a reviewer-written Census query.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable

from navigator.jurisdiction.address import compare, same_place_name, split_matched
from navigator.jurisdiction.census import (BENCHMARK, CLIENT_VERSION, LAYER_BLOCKS, LAYER_CDPS, LAYER_COUNTIES,
                                           LAYER_COUSUB, LAYER_PLACES, LAYER_STATES, MATCH, TIE, VINTAGE,
                                           AddressInput, Area, BatchRow, CacheMiss, Candidate, CensusError,
                                           CensusGeocoder, PointGeography)
from navigator.jurisdiction.crosswalk import Crosswalk
from navigator.validation import zip_outside_state

RESOLVER = "census-geocoder/m4-v1"
RESOLVED, REVIEW, UNRESOLVED = "resolved", "review_required", "unresolved"
ATTEMPTS = ("A", "B", "C")
POSTAL_SAME, POSTAL_KNOWN, POSTAL_OTHER, POSTAL_UNRESOLVED = (
    "same_name", "known_neighborhood_or_postal_difference", "other_difference", "unresolved")

# Boundary audit: the incorporated place at four points about 80 m from each matched point
# (0.0007 degrees of latitude; 0.0009 degrees of longitude at these latitudes). A different
# place nearby is a warning and a manual-review target. It never changes the result: the
# Census point, placed on the address range's side of the street, is the evidence.
BOUNDARY_OFFSETS = (("N", 0.0007, 0.0), ("S", -0.0007, 0.0), ("E", 0.0, 0.0009), ("W", 0.0, -0.0009))

# FIPS PUB 5-2 state codes -> USPS abbreviations (states, DC and territories).
STATE_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP",
    "72": "PR", "78": "VI",
}


@dataclass(frozen=True)
class SourceAddress:
    """The four sample-address fields M4 may use."""

    address_id: str
    street_address: str
    postal_city: str
    state: str
    zip: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "SourceAddress":
        return cls(row["address_id"], row["street_address"].strip(), row["postal_city"].strip(),
                   row["state"].strip(), row["zip"].strip())

    def as_dict(self) -> dict[str, str]:
        return {"street_address": self.street_address, "postal_city": self.postal_city, "state": self.state,
                "zip": self.zip}


def attempt_input(src: SourceAddress, attempt: str) -> AddressInput | str:
    """The batch row for an attempt, or the reason it is skipped."""
    if attempt == "A":
        return AddressInput(src.address_id, src.street_address, src.postal_city, src.state, src.zip)
    if not src.zip:
        return ("no ZIP in the source: street + state alone is not a supported Census input form" if attempt == "B"
                else "no ZIP in the source: identical to attempt A")
    if attempt == "B":
        return AddressInput(src.address_id, src.street_address, "", src.state, src.zip)
    if not src.postal_city:
        return "no postal city in the source: identical to attempt B"
    return AddressInput(src.address_id, src.street_address, src.postal_city, src.state, "")


def _usable(row: BatchRow | None, state: str) -> bool:
    return row is not None and row.indicator == MATCH and STATE_FIPS.get(row.state_fips or "") == state


@dataclass
class Rounds:
    attempts: dict[str, list[dict[str, Any]]]
    rows: dict[str, dict[str, BatchRow]]               # attempt -> id -> row
    requests: list[dict[str, Any]]                     # one per batch request


def run_attempts(sources: list[SourceAddress], geocoder: CensusGeocoder) -> Rounds:
    attempts: dict[str, list[dict[str, Any]]] = {s.address_id: [] for s in sources}
    rows: dict[str, dict[str, BatchRow]] = {}
    requests: list[dict[str, Any]] = []
    pending = list(sources)
    for name in ATTEMPTS:
        inputs: list[AddressInput] = []
        for src in pending:
            spec = attempt_input(src, name)
            if isinstance(spec, str):
                attempts[src.address_id].append({"attempt": name, "skipped": spec})
            else:
                inputs.append(spec)
        if not inputs:
            continue
        result, fetched = geocoder.batch(inputs)
        rows[name] = result
        requests.append({"attempt": name, "rows": len(inputs), "cache_key": fetched.key,
                         "cache_hit": fetched.cache_hit, "retrieved_at": fetched.entry["retrieved_at"]})
        for spec in inputs:
            r = result[spec.id]
            record: dict[str, Any] = {
                "attempt": name, "skipped": None,
                "sent": {"street": spec.street, "city": spec.city, "state": spec.state, "zip": spec.zip},
                "indicator": r.indicator, "match_type": r.match_type, "matched_address": r.matched_address,
                "longitude": r.longitude, "latitude": r.latitude, "tiger_line_id": r.tiger_line_id, "side": r.side,
                "state_fips": r.state_fips, "block_geoid": r.block_geoid, "census_input_echo": r.input_echo,
                "batch_cache_key": fetched.key,
            }
            if r.indicator == MATCH:
                record["state_consistent"] = STATE_FIPS.get(r.state_fips or "") == spec.state
            attempts[spec.id].append(record)
        pending = [s for s in pending if not _usable(result.get(s.address_id), s.state)]
    return Rounds(attempts, rows, requests)




def _area(areas) -> dict[str, Any] | None:
    return areas[0].evidence() if len(areas) == 1 else None


def postal_category(postal_city: str, place_basename: str | None, census_cities: Iterable[str]) -> str:
    if place_basename is None:
        return POSTAL_UNRESOLVED
    if same_place_name(postal_city, place_basename):
        return POSTAL_SAME
    if any(same_place_name(postal_city, c) for c in census_cities if c):
        return POSTAL_KNOWN
    return POSTAL_OTHER


def postal_audit(postal_city: str, place: Area | None, census_cities: list[str]) -> dict[str, Any]:
    category = postal_category(postal_city, place.basename if place else None, census_cities)
    return {"category": category, "postal_city": postal_city,
            "census_place_basename": place.basename if place else None,
            "census_matched_city": census_cities[0] if census_cities else None,
            "note": {POSTAL_SAME: "postal city equals the Census place name",
                     POSTAL_KNOWN: "postal city differs from the legal place; Census address data uses it as the "
                                   "mailing city of this address",
                     POSTAL_OTHER: "postal city differs from the legal place and is not the Census mailing city",
                     POSTAL_UNRESOLVED: "no Census place"}[category]}


def _geography(out: dict[str, Any], reasons: list[str], src: SourceAddress, geo: PointGeography,
               crosswalk: Crosswalk) -> Area | None:
    """Fill state, county, subdivision, CDP and place from one geoLookup; return the place."""
    states, counties = geo.get(LAYER_STATES), geo.get(LAYER_COUNTIES)
    places, cdps, cousubs = geo.get(LAYER_PLACES), geo.get(LAYER_CDPS), geo.get(LAYER_COUSUB)
    stusab = states[0].stusab if len(states) == 1 else None
    out["state"] = {"usps": stusab, "fips": states[0].geoid if len(states) == 1 else None,
                    "name": states[0].name if len(states) == 1 else None}
    out["county"] = _area(counties)
    out["county_subdivision"] = _area(cousubs)
    out["census_designated_place"] = _area(cdps)
    if stusab != src.state:
        reasons.append(f"state_inconsistent (input {src.state}, geoLookup {stusab})")
    if stusab:
        out["state_jurisdiction"] = crosswalk.state(stusab)[0]
    place = places[0] if len(places) == 1 else None
    if len(places) > 1:
        reasons.append(f"multiple_incorporated_places: {[p.name for p in places]}")
    elif not places:
        hint = (f"; inside CDP {cdps[0].name}" if cdps else "") + (
            f"; county subdivision {cousubs[0].name} has an active government"
            if cousubs and cousubs[0].funcstat == "A" else "")
        reasons.append(f"no_incorporated_place{hint}")
    if place is None:
        return None
    out["census_place"] = place.evidence()
    out["census_place_name"], out["census_place_geoid"] = place.name, place.geoid
    if place.funcstat != "A":
        reasons.append(f"place_not_active_government (FUNCSTAT {place.funcstat})")
    cousub = cousubs[0] if len(cousubs) == 1 else None
    if cousub and cousub.funcstat == "A" and not same_place_name(cousub.basename, place.basename):
        reasons.append(f"county_subdivision_differs_from_place ({cousub.name} vs {place.name})")
    if stusab:
        entry = crosswalk.place(place, stusab)
        out["local_jurisdiction"] = out["canonical_jurisdiction"] = entry.canonical_jurisdiction
        out["local_in_corpus"] = entry.in_corpus
        out["crosswalk_reason"] = entry.reason
    return place


def _tie(out: dict[str, Any], reasons: list[str], src: SourceAddress, tie: dict[str, Any],
         crosswalk: Crosswalk) -> None:
    """Tie-only rows: report the candidates. Geography is given only when every candidate is
    in the submitted state and inside one incorporated place; the point stays unknown."""
    cands: list[Candidate] = tie["candidates"]
    rows = []
    for c in cands:
        st, pl = c.geography.get(LAYER_STATES), c.geography.get(LAYER_PLACES)
        rows.append({"matched_address": c.matched_address, "longitude": c.longitude, "latitude": c.latitude,
                     "tiger_line_id": c.tiger_line_id, "side": c.side,
                     "state": st[0].stusab if len(st) == 1 else None,
                     "census_place_name": pl[0].name if len(pl) == 1 else None,
                     "census_place_geoid": pl[0].geoid if len(pl) == 1 else None,
                     "county_geoid": (_area(c.geography.get(LAYER_COUNTIES)) or {}).get("geoid")})
    out["tie_candidates"] = {"attempt": tie["attempt"], "cache_key": tie["cache_key"], "candidates": rows}
    out["match_indicator"] = TIE
    if not cands:
        reasons.append("tie_candidates_not_listed_by_single_record_lookup")
        out["resolution_status"] = UNRESOLVED
        out["postal_city_audit"] = postal_audit(src.postal_city, None, [])
        return
    keys = {(r["state"], r["census_place_geoid"]) for r in rows}
    place = None
    if len(keys) == 1 and rows[0]["state"] == src.state and rows[0]["census_place_geoid"]:
        place = _geography(out, reasons, src, cands[0].geography, crosswalk)
        for field_name, layer in (("county", LAYER_COUNTIES), ("county_subdivision", LAYER_COUSUB),
                                  ("census_designated_place", LAYER_CDPS)):
            if len({(_area(c.geography.get(layer)) or {}).get("geoid") for c in cands}) > 1:
                out[field_name] = None
        reasons.append(f"tie_candidates_share_place: {len(cands)} candidates, all inside {rows[0]['census_place_name']}")
    else:
        reasons.append(f"tie_candidates_disagree: {sorted(keys, key=str)}")
    out["evidence"] = {"source": "U.S. Census Geocoder", "benchmark": BENCHMARK, "vintage": VINTAGE,
                       "client": CLIENT_VERSION, "address_cache_key": tie["cache_key"],
                       "retrieved_at": tie["retrieved_at"]}
    out["postal_city_audit"] = postal_audit(src.postal_city, place,
                                            [split_matched(c.matched_address).city for c in cands])
    out["resolution_status"] = REVIEW


def boundary_neighbors(geocoder: CensusGeocoder, x: str, y: str) -> list[dict[str, Any]]:
    """Places at fixed offsets around a matched point (audit only, see BOUNDARY_OFFSETS)."""
    out = []
    for direction, dlat, dlon in BOUNDARY_OFFSETS:
        nx, ny = f"{float(x) + dlon:.6f}", f"{float(y) + dlat:.6f}"
        row: dict[str, Any] = {"direction": direction, "longitude": nx, "latitude": ny}
        try:
            geo = geocoder.point(nx, ny)[0]
        except CacheMiss:
            raise
        except CensusError as exc:
            out.append({**row, "error": str(exc)})
            continue
        places, cousubs = geo.get(LAYER_PLACES), geo.get(LAYER_COUSUB)
        out.append({**row, "census_place_geoid": places[0].geoid if len(places) == 1 else None,
                    "census_place_name": places[0].name if len(places) == 1 else None,
                    "county_subdivision_name": cousubs[0].name if len(cousubs) == 1 else None})
    return out


def _boundary(out: dict[str, Any], warnings: list[str], neighbors: list[dict[str, Any]] | None) -> None:
    if neighbors is None:
        return
    here = out["census_place_geoid"]
    other = [n for n in neighbors if "error" not in n and n["census_place_geoid"] != here]
    out["boundary_check"] = {"offsets": "about 80 m north, south, east and west", "neighbors": neighbors,
                             "different_place_nearby": bool(other)}
    if other:
        warnings.append("near_place_boundary: " + ", ".join(
            f"{n['direction']} -> {n['census_place_name'] or _no_place(n)}" for n in other))


def _no_place(n: dict[str, Any]) -> str:
    sub = n.get("county_subdivision_name")
    return f"no incorporated place ({sub})" if sub else "no incorporated place"


def resolve_one(src: SourceAddress, attempts: list[dict[str, Any]], point: PointGeography | None,
                point_meta: dict[str, Any] | None, crosswalk: Crosswalk,
                tie: dict[str, Any] | None = None,
                neighbors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    zip_flag = zip_outside_state(src.state, src.zip)
    input_flags = (["zip_missing"] if not src.zip else []) + (["zip_outside_state"] if zip_flag else [])
    run = [a for a in attempts if not a["skipped"]]
    used = next((a for a in run if a["indicator"] == MATCH and a.get("state_consistent")), None)
    for a in run:
        if a is used:
            break
        if a["indicator"] == TIE:
            reasons.append(f"tie_in_attempt_{a['attempt']}")
        elif a["indicator"] == MATCH:
            reasons.append(f"conflicting_attempt_{a['attempt']}: matched outside {src.state} "
                           f"(state FIPS {a['state_fips']})")
    out: dict[str, Any] = OrderedDict(
        address_id=src.address_id, input_address=src.as_dict(), input_flags=input_flags,
        resolution_status=None, review_reasons=reasons, warnings=warnings,
        state_jurisdiction=None, local_jurisdiction=None, canonical_jurisdiction=None, local_in_corpus=None,
        matched_address=None, match_indicator=None, match_type=None, attempt_used=None,
        latitude=None, longitude=None, state=None, county=None, census_place_name=None, census_place_geoid=None,
        census_place=None, county_subdivision=None, census_designated_place=None, block_geoid=None,
        address_differences=None, postal_city_audit=None, crosswalk_reason=None, boundary_check=None,
        tie_candidates=None,
        attempts=attempts, evidence=None, override=None, resolver=RESOLVER)
    if zip_flag:
        warnings.append(f"source ZIP {src.zip} is outside {src.state}'s USPS range (starter-pack audit)")
    if used is None:
        if tie is not None:
            _tie(out, reasons, src, tie, crosswalk)
            return out
        out["resolution_status"] = UNRESOLVED
        matched_elsewhere = any(a["indicator"] == MATCH for a in run)
        reasons.append("only_out_of_state_matches" if matched_elsewhere else "no_match_after_bounded_fallbacks")
        out["postal_city_audit"] = postal_audit(src.postal_city, None, [])
        out["match_indicator"] = run[-1]["indicator"] if run else None
        return out

    out.update(matched_address=used["matched_address"], match_indicator=used["indicator"],
               match_type=used["match_type"], attempt_used=used["attempt"],
               latitude=float(used["latitude"]), longitude=float(used["longitude"]))
    sent = used["sent"]
    diffs = compare(sent["street"], sent["city"], sent["zip"], used["matched_address"], zip_plausible=not zip_flag)
    out["address_differences"] = diffs.as_dict()
    if used["match_type"] != "Exact" and diffs.meaningful:
        reasons.extend(f"non_exact_meaningful_difference: {d}" for d in diffs.meaningful)
    elif used["match_type"] == "Exact" and diffs.meaningful:
        warnings.append(f"Census reports Exact, but the comparison found: {'; '.join(diffs.meaningful)}")
    if used["attempt"] != "A":
        warnings.append(f"matched by fallback attempt {used['attempt']}")
    if point is None:
        reasons.append("point_geolookup_failed")
        out["resolution_status"] = REVIEW
        out["evidence"] = point_meta
        out["postal_city_audit"] = postal_audit(src.postal_city, None, [])
        return out
    place = _geography(out, reasons, src, point, crosswalk)
    _boundary(out, warnings, neighbors)
    blocks = point.get(LAYER_BLOCKS)
    out["block_geoid"] = blocks[0].geoid if len(blocks) == 1 else None
    if STATE_FIPS.get(used["state_fips"] or "") != (out["state"] or {}).get("usps"):
        reasons.append(f"state_inconsistent (batch FIPS {used['state_fips']}, geoLookup {out['state']['usps']})")
    if out["block_geoid"] != used["block_geoid"]:
        reasons.append(f"block_mismatch (batch {used['block_geoid']}, geoLookup {out['block_geoid']})")
    census_cities = [split_matched(a["matched_address"]).city for a in run
                     if a["indicator"] == MATCH and a.get("state_consistent")]
    out["postal_city_audit"] = postal_audit(src.postal_city, place, census_cities)
    out["evidence"] = point_meta
    out["resolution_status"] = REVIEW if reasons else RESOLVED
    return out


def verify_override(o: dict[str, Any], geocoder: CensusGeocoder, crosswalk: Crosswalk) -> list[dict[str, Any]]:
    """Re-run the reviewer's Census queries (one, or one per frontage of a compound address).
    Each must return exactly one candidate, inside one incorporated place that maps to the
    corrected jurisdiction, in the corrected state."""
    queries = o["census_verification"]
    results = []
    for q in queries if isinstance(queries, list) else [queries]:
        spec = AddressInput(o["address_id"], q["street"], q.get("city", ""), q["state"], q.get("zip", ""))
        cands, fetched = geocoder.candidates(spec)
        if len(cands) != 1:
            raise ValueError(f"override {o['address_id']}: verification query {spec.one_line()!r} returned "
                             f"{len(cands)} Census candidates")
        c = cands[0]
        states, places = c.geography.get(LAYER_STATES), c.geography.get(LAYER_PLACES)
        if len(states) != 1 or len(places) != 1:
            raise ValueError(f"override {o['address_id']}: verification point has {len(places)} incorporated places")
        entry = crosswalk.place(places[0], states[0].stusab)
        if (entry.canonical_jurisdiction, states[0].stusab) != (o["corrected_jurisdiction"],
                                                                 o["corrected_state_jurisdiction"]):
            raise ValueError(f"override {o['address_id']}: Census places {spec.one_line()!r} in "
                             f"{entry.canonical_jurisdiction}, not {o['corrected_jurisdiction']}")
        results.append({"query": q, "matched_address": c.matched_address, "longitude": c.longitude,
                        "latitude": c.latitude, "census_place_name": places[0].name,
                        "census_place_geoid": places[0].geoid, "census_place_basename": places[0].basename,
                        "state": states[0].stusab, "in_corpus": entry.in_corpus, "cache_key": fetched.key,
                        "retrieved_at": fetched.entry["retrieved_at"]})
    return results


OVERRIDE_FIELDS = ("address_id", "original_census_evidence", "corrected_state_jurisdiction", "corrected_jurisdiction",
                   "authoritative_reason", "census_verification", "reviewer_note")


def apply_overrides(resolutions: list[dict[str, Any]], overrides: list[dict[str, Any]], geocoder: CensusGeocoder,
                    crosswalk: Crosswalk) -> None:
    """Apply audited overrides. Each must name the exact Census evidence it corrects and carry
    a Census verification query. A stale or unverifiable override is an error: it is never
    silently applied or silently dropped."""
    by_id = {r["address_id"]: r for r in resolutions}
    for o in overrides:
        missing = [f for f in OVERRIDE_FIELDS if not o.get(f) and f != "original_census_evidence"]
        if missing or "original_census_evidence" not in o:
            raise ValueError(f"override {o.get('address_id')} lacks {missing or ['original_census_evidence']}")
        r = by_id.get(o["address_id"])
        if r is None:
            raise ValueError(f"override for unknown address {o['address_id']}")
        current = {k: r.get(k) for k in o["original_census_evidence"]}
        if current != o["original_census_evidence"]:
            raise ValueError(f"override for {o['address_id']} is stale: evidence is now {current}")
        verified = verify_override(o, geocoder, crosswalk)
        first = verified[0]
        r["override"] = {"census_status": r["resolution_status"], "census_review_reasons": list(r["review_reasons"]),
                         **o, "verification_result": verified}
        r["state_jurisdiction"] = o["corrected_state_jurisdiction"]
        r["local_jurisdiction"] = r["canonical_jurisdiction"] = o["corrected_jurisdiction"]
        r["local_in_corpus"] = first["in_corpus"]
        r["postal_city_audit"] = postal_audit(r["input_address"]["postal_city"],
                                              Area("", first["census_place_geoid"], first["census_place_name"],
                                                   first["census_place_basename"], "", "A", ""),
                                              [split_matched(v["matched_address"]).city for v in verified])
        r["resolution_status"] = RESOLVED
        r["review_reasons"] = []
        r["warnings"].append("audited override applied (Census-verified reviewer query)")


def _used(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((a for a in attempts if not a["skipped"] and a["indicator"] == MATCH and a.get("state_consistent")),
                None)


def resolve_addresses(rows: list[dict[str, str]], geocoder: CensusGeocoder, crosswalk: Crosswalk,
                      overrides: Iterable[dict[str, Any]] = (), *, check_boundaries: bool = True
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sources = [SourceAddress.from_row(r) for r in rows]
    if len({s.address_id for s in sources}) != len(sources):
        raise ValueError("address_id values must be unique")
    rounds = run_attempts(sources, geocoder)
    points: dict[tuple[str, str], tuple[PointGeography | None, dict[str, Any]]] = {}
    neighbors: dict[tuple[str, str], list[dict[str, Any]] | None] = {}
    ties: dict[str, dict[str, Any]] = {}
    for src in sources:
        attempts = rounds.attempts[src.address_id]
        used = _used(attempts)
        if used is None:
            tied = next((a for a in attempts if not a["skipped"] and a["indicator"] == TIE), None)
            if tied is not None:
                s = tied["sent"]
                cands, fetched = geocoder.candidates(AddressInput(src.address_id, s["street"], s["city"], s["state"],
                                                                  s["zip"]))
                ties[src.address_id] = {"attempt": tied["attempt"], "candidates": cands, "cache_key": fetched.key,
                                        "retrieved_at": fetched.entry["retrieved_at"]}
            continue
        xy = (used["longitude"], used["latitude"])
        if xy in points:
            continue
        try:
            geo, fetched = geocoder.point(*xy)
            neighbors[xy] = boundary_neighbors(geocoder, *xy) if check_boundaries else None
            points[xy] = (geo, {"source": "U.S. Census Geocoder", "benchmark": BENCHMARK, "vintage": VINTAGE,
                                "client": CLIENT_VERSION, "point_cache_key": fetched.key,
                                "retrieved_at": fetched.entry["retrieved_at"]})
        except CacheMiss:
            raise
        except CensusError as exc:
            points[xy] = (None, {"source": "U.S. Census Geocoder", "error": str(exc)})
    out = []
    for src in sources:
        attempts = rounds.attempts[src.address_id]
        used = _used(attempts)
        geo, meta = points[(used["longitude"], used["latitude"])] if used else (None, None)
        if meta is not None:
            meta = {**meta, "batch_cache_key": used["batch_cache_key"]}
        out.append(resolve_one(src, attempts, geo, meta, crosswalk, ties.get(src.address_id),
                               neighbors.get((used["longitude"], used["latitude"])) if used else None))
    ambiguous = crosswalk.ambiguous()
    for r in out:
        if r["local_jurisdiction"] in ambiguous:
            r["review_reasons"].append(f"crosswalk_ambiguous: several Census places map to {r['local_jurisdiction']}")
            r["resolution_status"] = REVIEW
    apply_overrides(out, list(overrides), geocoder, crosswalk)
    return out, {"batch_requests": rounds.requests, "point_requests": len(points), "tie_lookups": len(ties),
                 "point_failures": sum(1 for g, _ in points.values() if g is None)}
