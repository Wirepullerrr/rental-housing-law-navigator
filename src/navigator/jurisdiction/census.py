"""U.S. Census Geocoder client (no API key).

Two request kinds, against one benchmark and vintage:

- **Address batch** (`geographies/addressbatch`): one CSV request per attempt round. Returns
  the match indicator (Match / No_Match / Tie), match type (Exact / Non_Exact), the matched
  address, interpolated coordinates, the TIGER line and side, and the state/county/tract/block
  codes. Per the Census API documentation (02/2026), the batch geoLookup includes only
  state, county, tract and block. There is no way to request places in a batch, and the
  coordinates batch is limited the same way.
- **Point geoLookup** (`geographies/coordinates`): one request per distinct matched
  coordinate, for the States, Counties, County Subdivisions, Incorporated Places, Census
  Designated Places and 2020 Census Blocks layers.
- **Single-record address lookup** (`geographies/address`), with the same layers. Used only to
  list the candidates behind a batch `Tie` (same submitted components) and to verify an
  audited override. Never used as a new matching attempt.

Every raw response is cached under `cache/census/` (gitignored), keyed by the logical
request (URL, benchmark, vintage, layers and the exact CSV text or coordinates). Reruns are
therefore offline and reproducible. Without `live=True` a cache miss raises `CacheMiss`, so
nothing is ever fetched implicitly. Tests use an `httpx.MockTransport` and never open a socket.
"""

from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

from navigator.extraction.cache import ResponseCache, cache_key, sha256_hex

CLIENT_VERSION = "census-client/v1"
CENSUS_BASE = "https://geocoding.geo.census.gov/geocoder"
BATCH_URL = f"{CENSUS_BASE}/geographies/addressbatch"
POINT_URL = f"{CENSUS_BASE}/geographies/coordinates"
ADDRESS_URL = f"{CENSUS_BASE}/geographies/address"
BENCHMARK = "Public_AR_Current"
VINTAGE = "Current_Current"
# TIGERweb tigerWMS_Current layer ids. The response keys are the layer names below.
POINT_LAYERS = "80,82,22,28,30,12"
LAYER_STATES, LAYER_COUNTIES, LAYER_COUSUB = "States", "Counties", "County Subdivisions"
LAYER_PLACES, LAYER_CDPS, LAYER_BLOCKS = "Incorporated Places", "Census Designated Places", "2020 Census Blocks"

BATCH_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=30.0)
POINT_TIMEOUT = httpx.Timeout(30.0)
# Retry policy: transient failures only (429, any 5xx, timeouts, connection and protocol
# errors). Every other 4xx and every unparseable response fails at once.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (5.0, 20.0)
MAX_RETRY_AFTER_SECONDS = 120.0
SLEEP: Callable[[float], None] = time.sleep          # injectable for tests
_TRANSIENT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

MATCH, NO_MATCH, TIE = "Match", "No_Match", "Tie"
INDICATORS = (MATCH, NO_MATCH, TIE)
MATCH_TYPES = ("Exact", "Non_Exact")


class CensusError(RuntimeError):
    """A request failed (after the bounded retries) or returned something unparseable."""


class CacheMiss(CensusError):
    """Offline mode: the request is not in the cache."""


# ----------------------------------------------------------------- request inputs

@dataclass(frozen=True)
class AddressInput:
    """One row of a batch request: exactly the five Census batch fields."""

    id: str
    street: str
    city: str
    state: str
    zip: str

    def one_line(self) -> str:
        return ", ".join(p for p in (self.street, self.city, self.state, self.zip) if p)


def batch_csv(rows: Sequence[AddressInput]) -> str:
    """Deterministic batch file: rows sorted by id, `Unique ID, Street, City, State, ZIP`,
    missing components kept as empty fields, no header (Census batch format)."""
    ids = [r.id for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("batch ids must be unique")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for r in sorted(rows, key=lambda r: r.id):
        writer.writerow([r.id, r.street, r.city, r.state, r.zip])
    return buf.getvalue()


# ----------------------------------------------------------------- parsed responses

@dataclass(frozen=True)
class BatchRow:
    id: str
    input_echo: str
    indicator: str
    match_type: str | None = None
    matched_address: str | None = None
    longitude: str | None = None
    latitude: str | None = None
    tiger_line_id: str | None = None
    side: str | None = None
    state_fips: str | None = None
    county_fips: str | None = None
    tract: str | None = None
    block: str | None = None

    @property
    def block_geoid(self) -> str | None:
        parts = (self.state_fips, self.county_fips, self.tract, self.block)
        return "".join(parts) if all(parts) else None


def parse_batch_response(text: str, submitted_ids: Sequence[str]) -> dict[str, BatchRow]:
    """Parse a geographies/addressbatch CSV. Every submitted id must appear exactly once."""
    if text.lstrip().startswith("<"):
        raise CensusError("batch response is HTML, not CSV")
    rows: dict[str, BatchRow] = {}
    for rec in csv.reader(io.StringIO(text)):
        if not rec or not any(f.strip() for f in rec):
            continue
        rec = [f.strip() for f in rec]
        if len(rec) < 3 or rec[2] not in INDICATORS:
            raise CensusError(f"unexpected batch row: {rec[:4]!r}")
        rid, echo, indicator = rec[0], rec[1], rec[2]
        if rid in rows:
            raise CensusError(f"batch response repeats id {rid}")
        if indicator == MATCH:
            if len(rec) < 12 or rec[3] not in MATCH_TYPES:
                raise CensusError(f"malformed Match row for {rid}: {rec!r}")
            lon, _, lat = rec[5].partition(",")
            if not lon or not lat:
                raise CensusError(f"Match row for {rid} has no coordinates")
            rows[rid] = BatchRow(rid, echo, indicator, rec[3], rec[4], lon.strip(), lat.strip(), rec[6], rec[7],
                                 rec[8] or None, rec[9] or None, rec[10] or None, rec[11] or None)
        else:
            rows[rid] = BatchRow(rid, echo, indicator)
    missing = sorted(set(submitted_ids) - set(rows))
    extra = sorted(set(rows) - set(submitted_ids))
    if missing or extra:
        raise CensusError(f"batch response ids differ from the request: missing {missing[:5]}, extra {extra[:5]}")
    return rows


@dataclass(frozen=True)
class Area:
    """One Census geography feature from a point geoLookup."""

    layer: str
    geoid: str
    name: str
    basename: str
    lsadc: str
    funcstat: str
    state: str
    stusab: str | None = None

    def evidence(self) -> dict[str, Any]:
        return {"geoid": self.geoid, "name": self.name, "basename": self.basename, "lsadc": self.lsadc,
                "funcstat": self.funcstat}


@dataclass(frozen=True)
class PointGeography:
    longitude: str
    latitude: str
    layers: dict[str, tuple[Area, ...]] = field(default_factory=dict)

    def get(self, layer: str) -> tuple[Area, ...]:
        return self.layers.get(layer, ())


def parse_point_response(payload: dict[str, Any], x: str, y: str) -> PointGeography:
    try:
        result = payload["result"]
        location = result["input"]["location"]
        geographies = result["geographies"]
    except (KeyError, TypeError) as exc:
        raise CensusError(f"point response lacks result/input/geographies ({exc})") from exc
    if abs(float(location["x"]) - float(x)) > 1e-9 or abs(float(location["y"]) - float(y)) > 1e-9:
        raise CensusError(f"point response is for {location}, not ({x}, {y})")
    layers: dict[str, tuple[Area, ...]] = {}
    for name, features in geographies.items():
        layers[name] = tuple(
            Area(layer=name, geoid=str(f.get("GEOID", "")), name=str(f.get("NAME", "")),
                 basename=str(f.get("BASENAME", "")), lsadc=str(f.get("LSADC", "")),
                 funcstat=str(f.get("FUNCSTAT", "")), state=str(f.get("STATE", "")),
                 stusab=f.get("STUSAB"))
            for f in features or ())
    return PointGeography(x, y, layers)


# ----------------------------------------------------------------- HTTP with cache

def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


@dataclass
class Fetched:
    key: str
    entry: dict[str, Any]
    cache_hit: bool


@dataclass
class CensusStats:
    requests: int = 0            # HTTP requests sent (including retries)
    cache_hits: int = 0
    cache_misses: int = 0
    failures: list[str] = field(default_factory=list)


class CensusGeocoder:
    def __init__(self, cache_dir: Path, *, live: bool = False, http_client: httpx.Client | None = None) -> None:
        """`live=False` serves only from the cache. `http_client` is for tests (MockTransport)."""
        self.cache = ResponseCache(cache_dir)
        self.live = live
        self._http = http_client
        self.stats = CensusStats()

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(headers={"User-Agent": f"rental-housing-law-navigator/{CLIENT_VERSION}"})
        return self._http

    def _send(self, describe: str, make: Callable[[httpx.Client], httpx.Response]) -> tuple[httpx.Response, int]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.stats.requests += 1
            try:
                resp = make(self._client())
            except _TRANSIENT as exc:
                if attempt == MAX_ATTEMPTS:
                    raise CensusError(f"{describe}: {type(exc).__name__} after {attempt} attempts") from exc
                SLEEP(BACKOFF_SECONDS[attempt - 1])
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                hint = _retry_after(resp)
                if attempt == MAX_ATTEMPTS or (hint is not None and hint > MAX_RETRY_AFTER_SECONDS):
                    raise CensusError(f"{describe}: HTTP {resp.status_code} after {attempt} attempts")
                SLEEP(hint if hint is not None else BACKOFF_SECONDS[attempt - 1])
                continue
            if resp.status_code != 200:
                raise CensusError(f"{describe}: HTTP {resp.status_code} (not retried)")
            return resp, attempt
        raise AssertionError("unreachable")

    def _fetch(self, key_fields: dict[str, Any], describe: str, make, validate) -> Fetched:
        key = cache_key(key_fields)
        entry = self.cache.get(key)
        if entry is not None:
            self.stats.cache_hits += 1
            validate(entry["body"])
            return Fetched(key, entry, True)
        self.stats.cache_misses += 1
        if not self.live:
            raise CacheMiss(f"{describe}: not cached (key {key[:12]}); rerun with --live to fetch it")
        started = time.monotonic()
        try:
            resp, attempts = self._send(describe, make)
            body = resp.text
            validate(body)                               # never cache an unparseable response
        except CensusError as exc:
            self.stats.failures.append(str(exc))
            raise
        entry = {"key": key, "key_fields": key_fields, "client": CLIENT_VERSION,
                 "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "elapsed_seconds": round(time.monotonic() - started, 2), "http_attempts": attempts,
                 "http_status": resp.status_code, "content_type": resp.headers.get("content-type", ""),
                 "body": body}
        self.cache.put(key, entry)
        return Fetched(key, entry, False)

    def batch(self, rows: Sequence[AddressInput]) -> tuple[dict[str, BatchRow], Fetched]:
        text = batch_csv(rows)
        ids = [r.id for r in rows]
        key_fields = {"kind": "addressbatch", "client": CLIENT_VERSION, "url": BATCH_URL, "benchmark": BENCHMARK,
                      "vintage": VINTAGE, "csv": text, "csv_sha256": sha256_hex(text)}
        make = lambda c: c.post(BATCH_URL, data={"benchmark": BENCHMARK, "vintage": VINTAGE},  # noqa: E731
                                files={"addressFile": ("addresses.csv", text.encode("utf-8"), "text/csv")},
                                timeout=BATCH_TIMEOUT)
        fetched = self._fetch(key_fields, f"address batch ({len(rows)} rows)", make,
                              lambda body: parse_batch_response(body, ids))
        return parse_batch_response(fetched.entry["body"], ids), fetched

    def point(self, x: str, y: str) -> tuple[PointGeography, Fetched]:
        params = {"x": x, "y": y, "benchmark": BENCHMARK, "vintage": VINTAGE, "layers": POINT_LAYERS,
                  "format": "json"}
        key_fields = {"kind": "coordinates", "client": CLIENT_VERSION, "url": POINT_URL, "params": params}
        make = lambda c: c.get(POINT_URL, params=params, timeout=POINT_TIMEOUT)  # noqa: E731

        def validate(body: str) -> PointGeography:
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise CensusError(f"point response for ({x}, {y}) is not JSON") from exc
            return parse_point_response(payload, x, y)

        fetched = self._fetch(key_fields, f"point geoLookup ({x}, {y})", make, validate)
        return validate(fetched.entry["body"]), fetched

    def candidates(self, spec: AddressInput) -> tuple[list[Candidate], Fetched]:
        """Single-record `geographies/address` lookup of exactly the submitted components.
        Lists every candidate the matcher found (a batch Tie only says there were several),
        each with its own geoLookup."""
        params = {"street": spec.street, "city": spec.city, "state": spec.state, "zip": spec.zip,
                  "benchmark": BENCHMARK, "vintage": VINTAGE, "layers": POINT_LAYERS, "format": "json"}
        params = {k: v for k, v in params.items() if v}
        key_fields = {"kind": "address", "client": CLIENT_VERSION, "url": ADDRESS_URL, "params": params}
        make = lambda c: c.get(ADDRESS_URL, params=params, timeout=POINT_TIMEOUT)  # noqa: E731

        def validate(body: str) -> list[Candidate]:
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise CensusError(f"address lookup for {spec.one_line()!r} is not JSON") from exc
            return parse_candidates(payload)

        fetched = self._fetch(key_fields, f"address lookup ({spec.one_line()})", make, validate)
        return validate(fetched.entry["body"]), fetched


@dataclass(frozen=True)
class Candidate:
    matched_address: str
    longitude: str
    latitude: str
    tiger_line_id: str
    side: str
    geography: PointGeography


def parse_candidates(payload: dict[str, Any]) -> list[Candidate]:
    try:
        matches = payload["result"]["addressMatches"]
    except (KeyError, TypeError) as exc:
        raise CensusError(f"address response lacks result/addressMatches ({exc})") from exc
    out = []
    for m in matches:
        x, y = repr(float(m["coordinates"]["x"])), repr(float(m["coordinates"]["y"]))
        geo = parse_point_response({"result": {"input": {"location": {"x": x, "y": y}},
                                               "geographies": m.get("geographies", {})}}, x, y)
        line = m.get("tigerLine", {})
        out.append(Candidate(m["matchedAddress"], x, y, str(line.get("tigerLineId", "")), str(line.get("side", "")),
                             geo))
    return out
