"""M4 address -> jurisdiction resolution, against a synthetic Census Geocoder served by an
httpx.MockTransport. No socket is ever opened (the autouse no_network fixture also checks)."""

from __future__ import annotations

import csv
import io
import json

import httpx
import pytest

from navigator.jurisdiction import census
from navigator.jurisdiction.address import compare, parse_street
from navigator.jurisdiction.census import (AddressInput, CacheMiss, CensusError, CensusGeocoder, batch_csv,
                                           parse_batch_response, parse_point_response)
from navigator.jurisdiction.crosswalk import Crosswalk, corpus_jurisdictions
from navigator.jurisdiction.resolve import (POSTAL_KNOWN, POSTAL_OTHER, POSTAL_SAME, RESOLVED, REVIEW, UNRESOLVED,
                                            resolve_addresses)

# ----------------------------------------------------------------- synthetic Census

LA = {"place": ("0644000", "Los Angeles city", "Los Angeles", "25", "A"), "state": ("06", "CA", "California"),
      "county": ("06037", "Los Angeles County"), "cousub": ("0603791750", "Los Angeles CCD", "Los Angeles", "S")}
GLENDALE = {**LA, "place": ("0630000", "Glendale city", "Glendale", "25", "A"),
            "cousub": ("0603792900", "Glendale CCD", "Glendale", "S")}
BOSTON = {"place": ("2507000", "Boston city", "Boston", "25", "A"), "state": ("25", "MA", "Massachusetts"),
          "county": ("25025", "Suffolk County"), "cousub": ("2502507000", "Boston city", "Boston", "A")}
JERSEY_CITY = {"place": ("3436000", "Jersey City city", "Jersey City", "25", "A"), "state": ("34", "NJ", "New Jersey"),
               "county": ("34017", "Hudson County"), "cousub": ("3401736000", "Jersey City city", "Jersey City", "A")}
UNINCORPORATED = {**LA, "place": None, "cdp": ("0620802", "East Los Angeles CDP", "East Los Angeles", "57", "S")}


def feature(geoid, name, basename, lsadc, funcstat, state, **extra):
    return {"GEOID": geoid, "NAME": name, "BASENAME": basename, "LSADC": lsadc, "FUNCSTAT": funcstat,
            "STATE": state, **extra}


def geographies(x, y, geo, block):
    fips, usps, sname = geo["state"]
    g = {"States": [feature(fips, sname, sname, "00", "A", fips, STUSAB=usps)],
         "Counties": [feature(geo["county"][0], geo["county"][1], geo["county"][1].split(" County")[0], "06", "A",
                              fips)],
         "County Subdivisions": [feature(*geo["cousub"][:3], "21", geo["cousub"][3], fips)],
         "2020 Census Blocks": [feature(block, "Block " + block[-4:], block[-4:], "BK", "S", fips)]}
    if geo.get("place"):
        g["Incorporated Places"] = [feature(*geo["place"], fips)]
    if geo.get("cdp"):
        g["Census Designated Places"] = [feature(*geo["cdp"], fips)]
    return {"result": {"input": {"location": {"x": float(x), "y": float(y)}}, "geographies": g}}


class FakeCensus:
    """`batch` maps the submitted (street, city, state, zip) to a reply; anything else is No_Match.
    A Match reply is (match_type, matched_address, lon, lat, geo, block_geoid). `single` maps the
    components of a single-record lookup to its candidates [(matched, lon, lat, geo, block)].
    A point near a matched point (the boundary audit) gets that point's geography unless
    `nearby(x, y)` returns another one."""

    def __init__(self, batch=None, fail_first=0, status=200, single=None, nearby=None):
        self.batch = batch or {}
        self.single = single or {}
        self.nearby = nearby or (lambda x, y: None)
        self.calls: list[tuple[str, object]] = []
        self.fail_first, self.status = fail_first, status
        self.points: dict[tuple[str, str], tuple[dict, str]] = {}

    def _point(self, x: str, y: str) -> tuple[dict, str]:
        if (x, y) in self.points:
            return self.points[(x, y)]
        (cx, cy), (geo, block) = min(self.points.items(), key=lambda kv: abs(float(kv[0][0]) - float(x))
                                     + abs(float(kv[0][1]) - float(y)))
        assert abs(float(cx) - float(x)) < 0.002 and abs(float(cy) - float(y)) < 0.002
        return (self.nearby(x, y) or geo), block

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_first:
            self.fail_first -= 1
            self.calls.append(("error", None))
            return httpx.Response(503, text="busy")
        if self.status != 200:
            self.calls.append(("error", None))
            return httpx.Response(self.status, text="bad request")
        if request.url.path.endswith("/addressbatch"):
            body = request.content.decode("utf-8")
            part = body.split('filename="addresses.csv"', 1)[1].split("\r\n\r\n", 1)[1]
            text = part.rsplit("\r\n--", 1)[0]
            rows = list(csv.reader(io.StringIO(text)))
            self.calls.append(("batch", rows))
            lines = []
            for rid, street, city, state, zip_code in rows:
                echo = ", ".join(p for p in (street, city, state, zip_code) if p)
                reply = self.batch.get((street, city, state, zip_code))
                if reply in (None, "No_Match", "Tie"):
                    lines.append(f'"{rid}","{echo}","{reply or "No_Match"}"')
                    continue
                mtype, matched, lon, lat, geo, block = reply
                self.points[(lon, lat)] = (geo, block)
                lines.append(f'"{rid}","{echo}","Match","{mtype}","{matched}","{lon},{lat}","111","L",'
                             f'"{block[:2]}","{block[2:5]}","{block[5:11]}","{block[11:]}"')
            return httpx.Response(200, text="\n".join(reversed(lines)) + "\n")   # Census returns any order
        if request.url.path.endswith("/address"):
            q = request.url.params
            key = (q.get("street", ""), q.get("city", ""), q.get("state", ""), q.get("zip", ""))
            self.calls.append(("address", key))
            matches = []
            for matched, lon, lat, geo, block in self.single.get(key, []):
                self.points.setdefault((lon, lat), (geo, block))
                matches.append({"matchedAddress": matched, "coordinates": {"x": float(lon), "y": float(lat)},
                                "tigerLine": {"tigerLineId": "222", "side": "L"},
                                "geographies": geographies(lon, lat, geo, block)["result"]["geographies"]})
            return httpx.Response(200, json={"result": {"addressMatches": matches}})
        x, y = request.url.params["x"], request.url.params["y"]
        self.calls.append(("point", (x, y)))
        geo, block = self._point(x, y)
        return httpx.Response(200, json=geographies(x, y, geo, block))


def geocoder(tmp_path, fake, live=True):
    return CensusGeocoder(tmp_path / "census", live=live, http_client=httpx.Client(
        transport=httpx.MockTransport(fake.handler)))


MANIFEST = [{"doc_id": "D1", "jurisdictions": "Los Angeles, CA"}, {"doc_id": "D2", "jurisdictions": "CA"},
            {"doc_id": "D3", "jurisdictions": "Boston, MA"}, {"doc_id": "D4", "jurisdictions": "MA"},
            {"doc_id": "D5", "jurisdictions": "Jersey City, NJ"}, {"doc_id": "D6", "jurisdictions": "NJ"}]
RULES = [{"jurisdiction": "Los Angeles, CA"}, {"jurisdiction": "CA"}, {"jurisdiction": "Boston, MA"}]


def crosswalk():
    return Crosswalk(corpus_jurisdictions(MANIFEST, RULES))


def row(aid, street, city, state, zip_code="", **facts):
    return {"address_id": aid, "street_address": street, "postal_city": city, "state": state, "zip": zip_code,
            "year_built": facts.get("year_built", "1950"), "units": facts.get("units", "12"),
            "use_code": facts.get("use_code", "0500"), "use_description": "", "source_dataset": "test",
            "retrieved_at": "2026-10-01T00:00Z"}


def resolve(tmp_path, fake, rows, **kw):
    res, meta = resolve_addresses(rows, geocoder(tmp_path, fake), crosswalk(), **kw)
    return {r["address_id"]: r for r in res}, meta


# ----------------------------------------------------------------- parsing

def test_batch_csv_is_deterministic_sorted_and_keeps_empty_fields():
    rows = [AddressInput("A2", "1 MAIN ST", "", "NJ", "07030"), AddressInput("A1", "2 OAK, REAR", "Boston", "MA", "")]
    assert batch_csv(rows) == 'A1,"2 OAK, REAR",Boston,MA,\nA2,1 MAIN ST,,NJ,07030\n'
    assert batch_csv(rows) == batch_csv(list(reversed(rows)))


def test_batch_response_parsing_match_no_match_tie_in_any_order():
    text = ('"A2","1 X ST, Y, CA","No_Match"\n"A3","2 X ST, Y, CA","Tie"\n'
            '"A1","3 X ST, Y, CA, 90001","Match","Non_Exact","3 X ST, Y, CA, 90001","-118.1,34.2","9","R",'
            '"06","037","123456","1001"\n')
    rows = parse_batch_response(text, ["A1", "A2", "A3"])
    assert rows["A1"].match_type == "Non_Exact" and rows["A1"].longitude == "-118.1" and rows["A1"].latitude == "34.2"
    assert rows["A1"].block_geoid == "060371234561001"
    assert rows["A2"].indicator == "No_Match" and rows["A3"].indicator == "Tie" and rows["A3"].block_geoid is None
    with pytest.raises(CensusError, match="missing"):
        parse_batch_response(text, ["A1", "A2", "A3", "A4"])
    with pytest.raises(CensusError, match="HTML"):
        parse_batch_response("<html>error</html>", ["A1"])


def test_point_parsing_extracts_place_state_and_rejects_another_location():
    payload = geographies("-118.1", "34.2", LA, "060371234561001")
    geo = parse_point_response(payload, "-118.1", "34.2")
    place, = geo.get("Incorporated Places")
    assert (place.geoid, place.name, place.basename, place.funcstat) == ("0644000", "Los Angeles city",
                                                                         "Los Angeles", "A")
    assert geo.get("States")[0].stusab == "CA" and geo.get("Census Designated Places") == ()
    with pytest.raises(CensusError, match="not"):
        parse_point_response(payload, "-118.2", "34.2")


# ----------------------------------------------------------------- crosswalk

def test_corpus_jurisdictions_come_from_manifest_and_rules():
    corpus = corpus_jurisdictions(MANIFEST, RULES)
    assert set(corpus) == {"Los Angeles, CA", "CA", "Boston, MA", "MA", "Jersey City, NJ", "NJ"}
    assert corpus["Los Angeles, CA"].published_rules == 1 and corpus["Jersey City, NJ"].published_rules == 0
    with pytest.raises(ValueError):
        corpus_jurisdictions([{"doc_id": "D9", "jurisdictions": "Los Angeles"}], [])


def test_crosswalk_uses_census_basename_not_string_stripping():
    cw = crosswalk()
    payload = geographies("-74.0", "40.7", JERSEY_CITY, "340170001001000")
    place, = parse_point_response(payload, "-74.0", "40.7").get("Incorporated Places")
    entry = cw.place(place, "NJ")
    assert entry.canonical_jurisdiction == "Jersey City, NJ" and entry.in_corpus       # NAME "Jersey City city"
    assert entry.census_place_geoid == "3436000" and "BASENAME" in entry.reason
    glendale, = parse_point_response(geographies("-118.2", "34.1", GLENDALE, "060371234561001"), "-118.2",
                                     "34.1").get("Incorporated Places")
    other = cw.place(glendale, "CA")
    assert other.canonical_jurisdiction == "Glendale, CA" and not other.in_corpus
    assert cw.state("CA") == ("CA", True)


def test_two_census_places_claiming_one_corpus_name_is_ambiguous():
    cw = crosswalk()
    a, = parse_point_response(geographies("1", "1", LA, "060371234561001"), "1", "1").get("Incorporated Places")
    clone = {**LA, "place": ("0699999", "Los Angeles town", "Los Angeles", "43", "A")}
    b, = parse_point_response(geographies("2", "2", clone, "060371234561001"), "2", "2").get("Incorporated Places")
    cw.place(a, "CA"), cw.place(b, "CA")
    assert cw.ambiguous() == {"Los Angeles, CA"}


# ----------------------------------------------------------------- resolution

LA_MATCH = ("Exact", "6238 DE LONGPRE AVE, LOS ANGELES, CA, 90028", "-118.32", "34.09", LA, "060371234561001")


def test_exact_match_resolves_with_state_and_local_jurisdiction_side_by_side(tmp_path):
    fake = FakeCensus({("6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028"): LA_MATCH})
    res, meta = resolve(tmp_path, fake, [row("A1", "6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028")])
    r = res["A1"]
    assert r["resolution_status"] == RESOLVED and r["review_reasons"] == []
    assert (r["state_jurisdiction"], r["local_jurisdiction"], r["canonical_jurisdiction"]) == (
        "CA", "Los Angeles, CA", "Los Angeles, CA")
    assert r["census_place_name"] == "Los Angeles city" and r["census_place_geoid"] == "0644000"
    assert r["state"]["usps"] == "CA" and r["county"]["name"] == "Los Angeles County"
    assert (r["latitude"], r["longitude"]) == (34.09, -118.32) and r["block_geoid"] == "060371234561001"
    assert r["postal_city_audit"]["category"] == POSTAL_SAME
    assert [a["attempt"] for a in r["attempts"]] == ["A"]
    assert meta["point_requests"] == 1


def test_van_nuys_style_postal_city_resolves_to_the_census_place(tmp_path):
    match = ("Exact", "6400 SEPULVEDA BLVD, VAN NUYS, CA, 91411", "-118.46", "34.18", LA, "060371234561002")
    fake = FakeCensus({("6400 SEPULVEDA BLVD", "Van Nuys", "CA", "91411"): match})
    r = resolve(tmp_path, fake, [row("A1", "6400 SEPULVEDA BLVD", "Van Nuys", "CA", "91411")])[0]["A1"]
    assert r["resolution_status"] == RESOLVED and r["local_jurisdiction"] == "Los Angeles, CA"
    assert r["postal_city_audit"]["category"] == POSTAL_KNOWN       # Census mailing city VAN NUYS


def test_dorchester_style_postal_city_resolves_to_boston(tmp_path):
    match = ("Exact", "12 ADAMS ST, BOSTON, MA, 02122", "-71.05", "42.29", BOSTON, "250250901001000")
    fake = FakeCensus({("12 ADAMS ST", "Dorchester", "MA", "02122"): match})
    r = resolve(tmp_path, fake, [row("A1", "12 ADAMS ST", "Dorchester", "MA", "02122")])[0]["A1"]
    assert r["resolution_status"] == RESOLVED and r["local_jurisdiction"] == "Boston, MA"
    assert r["state_jurisdiction"] == "MA"
    assert r["postal_city_audit"]["category"] == POSTAL_OTHER       # Census mailing city is BOSTON here


def test_postal_city_is_never_the_jurisdiction(tmp_path):
    match = ("Exact", "100 N BRAND BLVD, GLENDALE, CA, 91203", "-118.25", "34.15", GLENDALE, "060371234561003")
    fake = FakeCensus({("100 N BRAND BLVD", "Los Angeles", "CA", "91203"): match})
    r = resolve(tmp_path, fake, [row("A1", "100 N BRAND BLVD", "Los Angeles", "CA", "91203")])[0]["A1"]
    assert r["local_jurisdiction"] == "Glendale, CA" and r["local_in_corpus"] is False
    assert r["postal_city_audit"]["category"] == POSTAL_OTHER
    assert r["resolution_status"] == RESOLVED      # a non-corpus city is still a resolved jurisdiction


def test_bad_zip_falls_back_and_never_invents_a_zip(tmp_path):
    match = ("Exact", "876 S 14TH ST, NEWARK, NJ, 07103", "-74.2", "40.7", JERSEY_CITY, "340170001001001")
    fake = FakeCensus({("876 S 14TH ST", "Newark", "NJ", ""): match})       # only attempt C matches
    r = resolve(tmp_path, fake, [row("A1", "876 S 14TH ST", "Newark", "NJ", "11219")])[0]["A1"]
    assert [(a["attempt"], a["indicator"]) for a in r["attempts"]] == [("A", "No_Match"), ("B", "No_Match"),
                                                                       ("C", "Match")]
    assert [a["sent"]["zip"] for a in r["attempts"]] == ["11219", "11219", ""]
    assert [a["sent"]["city"] for a in r["attempts"]] == ["Newark", "", "Newark"]
    assert r["attempt_used"] == "C" and r["resolution_status"] == RESOLVED
    assert r["input_flags"] == ["zip_outside_state"]
    assert [c[0] for c in fake.calls] == ["batch", "batch", "batch"] + ["point"] * 5   # point + 4 boundary
    assert [len(c[1]) for c in fake.calls[:3]] == [1, 1, 1]


def test_missing_zip_skips_b_and_c_with_reasons(tmp_path):
    fake = FakeCensus()
    r = resolve(tmp_path, fake, [row("A1", "1 NOWHERE ST", "San Francisco", "CA")])[0]["A1"]
    assert [(a["attempt"], a["skipped"] is None) for a in r["attempts"]] == [("A", True), ("B", False), ("C", False)]
    assert "not a supported Census input form" in r["attempts"][1]["skipped"]
    assert r["input_flags"] == ["zip_missing"] and len(fake.calls) == 1


def test_no_match_after_bounded_fallbacks_is_unresolved_without_a_guess(tmp_path):
    fake = FakeCensus()
    r = resolve(tmp_path, fake, [row("A1", "1 NOWHERE ST", "Boston", "MA", "02101")])[0]["A1"]
    assert r["resolution_status"] == UNRESOLVED and r["review_reasons"] == ["no_match_after_bounded_fallbacks"]
    assert r["local_jurisdiction"] is None and r["state_jurisdiction"] is None   # postal city/state not reused
    assert [a["attempt"] for a in r["attempts"]] == ["A", "B", "C"]
    assert r["postal_city_audit"]["category"] == "unresolved"


def test_out_of_state_match_is_not_used(tmp_path):
    ny = ("Exact", "876 S 14TH ST, BROOKLYN, NY, 11219", "-73.9", "40.6", {**JERSEY_CITY, "state": ("36", "NY", "NY")},
          "360470001001000")
    fake = FakeCensus({("876 S 14TH ST", "", "NJ", "11219"): ny})
    r = resolve(tmp_path, fake, [row("A1", "876 S 14TH ST", "Newark", "NJ", "11219")])[0]["A1"]
    assert r["resolution_status"] == UNRESOLVED and r["review_reasons"] == ["conflicting_attempt_B: matched outside NJ "
                                                                            "(state FIPS 36)",
                                                                            "only_out_of_state_matches"]
    assert r["local_jurisdiction"] is None


def test_tie_then_match_requires_review(tmp_path):
    fake = FakeCensus({("10 MAIN ST", "Los Angeles", "CA", "90001"): "Tie",
                       ("10 MAIN ST", "", "CA", "90001"): ("Exact", "10 MAIN ST, LOS ANGELES, CA, 90001", "-118.2",
                                                          "34.0", LA, "060371234561004")})
    r = resolve(tmp_path, fake, [row("A1", "10 MAIN ST", "Los Angeles", "CA", "90001")])[0]["A1"]
    assert r["resolution_status"] == REVIEW and r["review_reasons"] == ["tie_in_attempt_A"]
    assert r["local_jurisdiction"] == "Los Angeles, CA"           # reported, but flagged


def test_non_exact_with_a_different_house_number_requires_review(tmp_path):
    match = ("Non_Exact", "6240 DE LONGPRE AVE, LOS ANGELES, CA, 90028", "-118.32", "34.09", LA, "060371234561001")
    fake = FakeCensus({("6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028"): match})
    r = resolve(tmp_path, fake, [row("A1", "6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028")])[0]["A1"]
    assert r["resolution_status"] == REVIEW
    assert r["review_reasons"] == ["non_exact_meaningful_difference: house_number_differs (6238 -> 6240)"]


def test_non_exact_with_only_spelling_differences_resolves(tmp_path):
    match = ("Non_Exact", "876 S 14TH ST, NEWARK, NJ, 07103", "-74.2", "40.7", JERSEY_CITY, "340170001001001")
    fake = FakeCensus({("876-878 SOUTH 14TH STREET", "Newark", "NJ", "07103"): match})
    r = resolve(tmp_path, fake, [row("A1", "876-878 SOUTH 14TH STREET", "Newark", "NJ", "07103")])[0]["A1"]
    assert r["resolution_status"] == RESOLVED
    assert r["address_differences"] == {"minor": ["house_number_range_endpoint (876-878 -> 876)"], "meaningful": []}


def test_unincorporated_point_requires_review_and_gets_no_city(tmp_path):
    match = ("Exact", "1 WHITTIER BLVD, LOS ANGELES, CA, 90022", "-118.17", "34.02", UNINCORPORATED,
             "060375310001000")
    fake = FakeCensus({("1 WHITTIER BLVD", "Los Angeles", "CA", "90022"): match})
    r = resolve(tmp_path, fake, [row("A1", "1 WHITTIER BLVD", "Los Angeles", "CA", "90022")])[0]["A1"]
    assert r["resolution_status"] == REVIEW and r["local_jurisdiction"] is None and r["state_jurisdiction"] == "CA"
    assert r["review_reasons"] == ["no_incorporated_place; inside CDP East Los Angeles CDP"]


def test_property_facts_never_change_the_resolution(tmp_path):
    fake = FakeCensus({("6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028"): LA_MATCH})
    a = resolve(tmp_path / "a", fake, [row("A1", "6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028")])[0]
    b = resolve(tmp_path / "b", fake, [row("A1", "6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028",
                                           year_built="2020", units="2", use_code="0100")])[0]
    strip = lambda r: {k: v for k, v in r.items() if k not in ("attempts", "evidence")}  # noqa: E731
    assert strip(a["A1"]) == strip(b["A1"])


# ----------------------------------------------------------------- cache, retry, overrides

def test_cache_serves_reruns_offline_and_misses_raise(tmp_path):
    fake = FakeCensus({("6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028"): LA_MATCH})
    rows = [row("A1", "6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028")]
    first = resolve_addresses(rows, geocoder(tmp_path, fake), crosswalk())[0]
    n = len(fake.calls)
    offline = CensusGeocoder(tmp_path / "census", live=False, http_client=httpx.Client(
        transport=httpx.MockTransport(fake.handler)))
    again = resolve_addresses(rows, offline, crosswalk())[0]
    assert again == first and len(fake.calls) == n and offline.stats.requests == 0
    assert offline.stats.cache_hits == 6                       # batch, point, 4 boundary points
    with pytest.raises(CacheMiss):
        resolve_addresses([row("A2", "1 OTHER ST", "Los Angeles", "CA", "90028")], offline, crosswalk())
    assert len(fake.calls) == n


def test_transient_errors_are_retried_and_client_errors_are_not(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(census, "SLEEP", waits.append)
    fake = FakeCensus({("6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028"): LA_MATCH}, fail_first=2)
    r = resolve(tmp_path, fake, [row("A1", "6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028")])[0]["A1"]
    assert r["resolution_status"] == RESOLVED and waits == [5.0, 20.0]
    bad = FakeCensus(status=400)
    gc = geocoder(tmp_path / "bad", bad)
    with pytest.raises(CensusError, match="HTTP 400"):
        gc.batch([AddressInput("A1", "1 X ST", "Y", "CA", "")])
    assert len(bad.calls) == 1 and not list((tmp_path / "bad").rglob("*.json"))   # nothing cached


def test_unparseable_response_is_not_cached(tmp_path):
    gc = CensusGeocoder(tmp_path / "census", live=True, http_client=httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html>maintenance</html>"))))
    with pytest.raises(CensusError, match="HTML"):
        gc.batch([AddressInput("A1", "1 X ST", "Y", "CA", "")])
    assert not list((tmp_path / "census").rglob("*.json"))


WHITTIER = ("Exact", "1 WHITTIER BLVD, LOS ANGELES, CA, 90022", "-118.17", "34.02", UNINCORPORATED,
            "060375310001000")


def override(**kw):
    o = {"address_id": "A1", "original_census_evidence": {"census_place_geoid": None,
                                                         "resolution_status": "review_required"},
         "corrected_state_jurisdiction": "CA", "corrected_jurisdiction": "Los Angeles, CA",
         "authoritative_reason": "synthetic", "reviewer_note": "test",
         "census_verification": [{"street": "1 WHITTIER BLVD", "city": "Los Angeles", "state": "CA", "zip": ""}]}
    return {**o, **kw}


def test_override_is_verified_by_census_and_recorded(tmp_path):
    single = {("1 WHITTIER BLVD", "Los Angeles", "CA", ""): [("1 WHITTIER BLVD, LOS ANGELES, CA, 90023", "-118.19",
                                                             "34.02", LA, "060371234561005")]}
    fake = FakeCensus({("1 WHITTIER BLVD", "Los Angeles", "CA", "90022"): WHITTIER}, single=single)
    rows = [row("A1", "1 WHITTIER BLVD", "Los Angeles", "CA", "90022")]
    r = resolve(tmp_path, fake, rows, overrides=[override()])[0]["A1"]
    assert r["resolution_status"] == RESOLVED and r["override"]["census_status"] == REVIEW
    assert (r["state_jurisdiction"], r["local_jurisdiction"], r["local_in_corpus"]) == ("CA", "Los Angeles, CA", True)
    assert r["override"]["verification_result"][0]["census_place_geoid"] == "0644000"
    assert r["census_place_geoid"] is None                   # the pipeline's own evidence is kept as it was


def test_override_fails_loudly_when_stale_or_contradicted_by_census(tmp_path):
    rows = [row("A1", "1 WHITTIER BLVD", "Los Angeles", "CA", "90022")]
    fake = FakeCensus({("1 WHITTIER BLVD", "Los Angeles", "CA", "90022"): WHITTIER})
    with pytest.raises(ValueError, match="stale"):
        resolve(tmp_path / "a", fake, rows,
                overrides=[override(original_census_evidence={"resolution_status": "resolved"})])
    with pytest.raises(ValueError, match="0 Census candidates"):
        resolve(tmp_path / "b", fake, rows, overrides=[override()])
    elsewhere = FakeCensus({("1 WHITTIER BLVD", "Los Angeles", "CA", "90022"): WHITTIER}, single={
        ("1 WHITTIER BLVD", "Los Angeles", "CA", ""): [("1 N BRAND BLVD, GLENDALE, CA", "-118.25", "34.15", GLENDALE,
                                                         "060371234561003")]})
    with pytest.raises(ValueError, match="Glendale, CA, not Los Angeles, CA"):
        resolve(tmp_path / "c", elsewhere, rows, overrides=[override()])
    with pytest.raises(ValueError, match="lacks"):
        resolve(tmp_path / "d", fake, rows, overrides=[override(census_verification=None)])


def test_tie_candidates_in_one_place_give_a_flagged_jurisdiction(tmp_path):
    single = {("90 KENSINGTON AVE", "Jersey City", "NJ", "07302"): [
        ("90 KENSINGTON AVE, JERSEY CITY, NJ, 07304", "-74.075", "40.723", JERSEY_CITY, "340170001001001"),
        ("90 KENSINGTON AVE, JERSEY CITY, NJ, 07306", "-74.078", "40.724", JERSEY_CITY, "340170001001002")]}
    fake = FakeCensus({("90 KENSINGTON AVE", "Jersey City", "NJ", "07302"): "Tie"}, single=single)
    r = resolve(tmp_path, fake, [row("A1", "90 KENSINGTON AVE", "Jersey City", "NJ", "07302")])[0]["A1"]
    assert r["resolution_status"] == REVIEW and r["local_jurisdiction"] == "Jersey City, NJ"
    assert r["latitude"] is None and r["block_geoid"] is None           # the point stays unknown
    assert r["review_reasons"][-1] == "tie_candidates_share_place: 2 candidates, all inside Jersey City city"
    assert ("address", ("90 KENSINGTON AVE", "Jersey City", "NJ", "07302")) in fake.calls   # same components


def test_tie_candidates_in_different_places_give_no_jurisdiction(tmp_path):
    cambridge = {**BOSTON, "place": ("2511000", "Cambridge city", "Cambridge", "25", "A"),
                 "cousub": ("2501711000", "Cambridge city", "Cambridge", "A")}
    single = {("5 WESTERN AVE", "Cambridge", "MA", ""): [
        ("5 WESTERN AVE, CAMBRIDGE, MA, 02163", "-71.118", "42.364", BOSTON, "250250001001001"),
        ("5 WESTERN AVE, CAMBRIDGE, MA, 02139", "-71.105", "42.365", cambridge, "250173531001000")]}
    fake = FakeCensus({("5 WESTERN AVE", "Cambridge", "MA", ""): "Tie"}, single=single)
    r = resolve(tmp_path, fake, [row("A1", "5 WESTERN AVE", "Cambridge", "MA")])[0]["A1"]
    assert r["resolution_status"] == REVIEW and r["local_jurisdiction"] is None
    assert r["review_reasons"][-1].startswith("tie_candidates_disagree")


def test_boundary_audit_flags_a_nearby_place_without_changing_the_result(tmp_path):
    fake = FakeCensus({("6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028"): LA_MATCH},
                      nearby=lambda x, y: GLENDALE if float(y) < 34.09 else None)
    r = resolve(tmp_path, fake, [row("A1", "6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028")])[0]["A1"]
    assert r["resolution_status"] == RESOLVED and r["local_jurisdiction"] == "Los Angeles, CA"
    assert r["boundary_check"]["different_place_nearby"] is True
    assert r["warnings"] == ["near_place_boundary: S -> Glendale city"]
    assert [n["direction"] for n in r["boundary_check"]["neighbors"]] == ["N", "S", "E", "W"]


# ----------------------------------------------------------------- address comparison

@pytest.mark.parametrize("given, matched, minor, meaningful", [
    ("1065 SUMMIT AVENUE", "1065 SUMMIT AVE, JERSEY CITY, NJ, 07307", [], []),
    ("128 ST. PAULS AVE.", "128 SAINT PAULS AVE, JERSEY CITY, NJ, 07306", [], []),
    ("314 SEVENTH ST.", "314 7TH ST, JERSEY CITY, NJ, 07302", [], []),
    ("397 05TH AV", "397 5TH AVE, SAN FRANCISCO, CA, 94118", [], []),
    ("17106 CHATSWORTH ST   APT 0001", "17106 CHATSWORTH ST, GRANADA HILLS, CA, 91344",
     ["secondary_unit_in_input (APT 0001)"], []),
    ("585 5TH ST", "585 N 5TH ST, NEWARK, NJ, 07107", [], ["street_directional_differs (5TH -> N 5TH)"]),
    ("65-71 NORFLOK ST", "71 NORFOLK ST, NEWARK, NJ, 07103", ["house_number_range_endpoint (65-71 -> 71)"],
     ["street_name_differs (NORFLOK -> NORFOLK)"]),
    ("14.5-16 Vandine St", "16 VANDINE ST, CAMBRIDGE, MA, 02141", ["house_number_range_endpoint (14.5-16 -> 16)"], []),
    ("Harvard ST", "1 HARVARD ST, CAMBRIDGE, MA, 02139", [], ["house_number_missing_in_input"]),
    ("10 OAK ST", "10 OAK AVE, X, CA, 90001", [], ["street_suffix_differs (ST -> AVE)"]),
])
def test_address_comparison(given, matched, minor, meaningful):
    c = compare(given, "", "", matched)
    assert (c.minor, c.meaningful) == (minor, meaningful)


def test_city_and_zip_both_differing_is_meaningful_unless_the_zip_was_implausible():
    assert compare("1 A ST", "Hoboken", "07030", "1 A ST, NEWARK, NJ, 07103").meaningful == ["city_and_zip_both_differ"]
    assert compare("1 A ST", "Hoboken", "10003", "1 A ST, NEWARK, NJ, 07103", zip_plausible=False).meaningful == []
    assert parse_street("342-344 IRVINE TURNER BLV").suffix is None          # not a USPS suffix form


def test_resolutions_are_json_serializable(tmp_path):
    fake = FakeCensus({("6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028"): LA_MATCH})
    res, _ = resolve_addresses([row("A1", "6238 DE LONGPRE AVE", "Los Angeles", "CA", "90028")],
                               geocoder(tmp_path, fake), crosswalk())
    assert json.loads(json.dumps(res)) == res
