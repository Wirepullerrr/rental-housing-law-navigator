# Address → legal jurisdiction (M4)

*Not legal advice.* M4 resolves geography only: each sample address gets a state jurisdiction and a local (municipal) jurisdiction, with the Census evidence behind them. Which rules apply is M5.

```sh
uv run python scripts/resolve_jurisdictions.py          # offline: cached Census responses only (exit 2 on a miss)
uv run python scripts/resolve_jurisdictions.py --live   # fetch cache misses from the Census Geocoder (no key)
```

Outputs go to `outputs/m4/`: `jurisdiction_resolutions.json` / `.csv` (one row per address), `jurisdiction_summary.json` and `jurisdiction_review_queue.json`.

## Inputs and what is never used

Only `street_address`, `postal_city`, `state` and `zip` are read, and only those plus the `address_id` are sent to Census. `postal_city` is a search hint and an audit signal; it is **never** the jurisdiction. ZIP is never used to infer a municipality. `year_built`, `units`, `use_code` and the source dataset are property facts for M5 and are not read. `data/sample_addresses.csv` is never modified.

## Pipeline (`src/navigator/jurisdiction/`)

```
sample address
  -> attempt A  street + postal_city + state + ZIP            one Census batch for all rows
  -> attempt B  street + state + ZIP      (rows A did not match; skipped without a ZIP)
  -> attempt C  street + postal_city + state (rows still unmatched; skipped without a ZIP)
       usable = Census "Match" in the submitted state; the first usable attempt is used
  -> point geoLookup at the matched coordinates: State, County, County Subdivision,
     Incorporated Place, CDP, 2020 Block
  -> boundary audit: the place at four points ~80 m N/S/E/W (warning only)
  -> crosswalk: Census incorporated place -> corpus jurisdiction
  -> status: resolved / review_required / unresolved
  -> audited overrides (Census-verified), then the postal-city audit
```

- `census.py`: the client. It makes batch address requests, point geoLookups and single-record address lookups, uses a content-addressed cache, and retries transient failures only (429, 5xx, timeouts, connection errors) up to 3 attempts with 5 s / 20 s backoff, honouring `Retry-After` up to 120 s. Any other 4xx, and any unparseable response, fails at once and is never cached. Benchmark `Public_AR_Current`, vintage `Current_Current`.
- `resolve.py`: the attempt sequence, statuses, tie inspection, boundary audit and overrides.
- `address.py`: comparison of the submitted address with the matched one (audit only).
- `crosswalk.py`: corpus jurisdictions and the Census place mapping.

**Why a point lookup.** The Census batch geoLookup returns only state, county, tract and block, never the place. That is a documented limitation, and the coordinates batch has the same limit. So each distinct matched coordinate is looked up once at `geographies/coordinates`. The block GEOID from the point lookup must equal the batch's block, and the state must agree; otherwise the row is `review_required`.

**Ties.** A batch `Tie` says only that several candidates exist. For a row whose attempts only tied, the tied attempt's exact components are sent once to the single-record endpoint, which lists the candidates. This is not a new matching attempt. The row is always `review_required`. A jurisdiction is reported only when every candidate is in the submitted state and inside one incorporated place; the point itself stays unknown.

## Status

| status | when |
|---|---|
| `resolved` | a usable Exact match, or Non_Exact with only minor differences; exactly one active incorporated place; batch and point agree on state and block; no tie, conflict or crosswalk ambiguity |
| `review_required` | a usable match with a problem (meaningful address difference, tie, an earlier out-of-state match, no incorporated place, several places, a place that is not an active government, a county subdivision with an active government that differs from the place, state or block disagreement), or tied candidates |
| `unresolved` | no usable Census match after A, B and C. No jurisdiction is given, not even the source state |

**Meaningful address differences** (`address.py`): a different or missing house number, a different street name, a different suffix, a directional present on one side only, or both city and a plausible ZIP different. **Minor:** a range endpoint (`876-878` → 876), case and punctuation, USPS Publication 28 abbreviations, ordinal words and zero padding (`SEVENTH`, `05TH`), `ST`/`MT` = `SAINT`/`MOUNT`, a trailing unit designator, and a different city or ZIP alone. Census `Exact` with a meaningful difference by this comparison is only a warning.

## Canonical jurisdictions and the crosswalk

Canonical names are those of the corpus: the manifest `jurisdictions` column and every published rule's `jurisdiction` ("CA", "Los Angeles, CA"). Each row carries both `state_jurisdiction` ("CA") and `local_jurisdiction` ("Los Angeles, CA"). State law does not make the city disappear.

A Census incorporated place maps to a corpus jurisdiction when its Census `BASENAME` and state equal the corpus name and state. `BASENAME` is Census's own name field without the legal description: NAME "Jersey City city" has BASENAME "Jersey City". No suffix is stripped from a string. A place outside the corpus keeps "<BASENAME>, <ST>" with `local_in_corpus = false`. Two GEOIDs claiming one corpus name send the affected rows to review. A Census Designated Place is never a municipality: an address with no incorporated place is `review_required` with no local jurisdiction.

## Postal-city audit

`postal_city` is compared with the legal place only after resolution:

- `same_name`;
- `known_neighborhood_or_postal_difference`: the postal name differs from the place, but Census address data uses it as this address's mailing city;
- `other_difference`;
- `unresolved`.

The Census matched-address city is a postal name too. Many City of Los Angeles addresses come back as NORTH HOLLYWOOD, PANORAMA CITY and so on. Only the incorporated place is used.

## Boundary audit

For every matched point, the incorporated place is also looked up at four fixed offsets: about 80 m north, south, east and west. A different place, or no place, at an offset adds the warning `near_place_boundary` and makes the address a manual-review target. It never changes the result. The Census point is interpolated along the address range and placed on that range's side of the street, so an address on a boundary street depends on that side assignment. These flags matter for change test T2 (Hoboken vs Jersey City).

## Manual review and overrides (`review/m4_jurisdiction_review.json`)

Every `review_required` and `unresolved` row is a manual-review target. So is every row where the postal city differs from the legal place, every Non_Exact match, every source ZIP outside its state, every fallback match and every boundary flag. Notes never change a result.

An **override** is applied only if:

- its `original_census_evidence` still equals the current row (otherwise the run fails as stale);
- every `census_verification` query, re-run on each run through the cache, returns exactly one Census candidate, inside an incorporated place that maps to the corrected jurisdiction, in the corrected state.

A failed verification is an error, never a silent skip. The row keeps its own Census evidence. The override and its verification result are recorded beside it, and the row is `resolved`. Overrides are not created because a city "looks obvious", and the source dataset's name is not used as evidence.

## Cache and reproducibility

Raw Census responses are cached in `cache/census/` (gitignored). Each is keyed by the logical request: URL, benchmark, vintage, layers and the exact CSV text or parameters. Entries record the retrieval time, the number of HTTP attempts and the raw body. A rerun without `--live` is fully offline and reproduces the outputs byte for byte. `Current` benchmarks change twice a year. The cache pins the responses, and a `--live` run against an empty cache may differ. Tests use an `httpx.MockTransport` with synthetic Census responses and never open a socket.

## Known limitations

- Census points are interpolated along address ranges, not parcel centroids. Near a city line, the result depends on the address range's side assignment (see the boundary audit).
- Addresses without a house number cannot be geocoded and stay `unresolved`.
- Census address data can carry a mailing city on a segment in another municipality. Mount Prospect Ave is labelled "Newark 07102" but lies in Verona township. Such ties are reported, not resolved.
- The point geoLookup reflects current boundaries (vintage `Current_Current`), not boundaries on the query date.
