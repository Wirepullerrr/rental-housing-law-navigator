# Rule applicability and lookups (M5)

*Not legal advice.* Deterministic Python only; no LLM decides applicability.

```sh
uv run python scripts/build_lookups.py            # as_of 2026-10-01; offline
```

**Inputs:**
- `outputs/m3/full_v2/rules.json` and its document artifacts;
- `outputs/m4/jurisdiction_resolutions.json`;
- `data/sample_addresses.csv`.

**Outputs** (`outputs/m5/`):
- `lookups.json`: the starter-pack wrapper `{as_of, lookups: {address_id: [{team_rule_id, result, explanation, conflict_flag}]}}`, with all 500 addresses;
- `lookups_audit.json`: per address, the facts, the jurisdiction status and, per rule, the result, reasons, missing facts, causes, citation, quote and source;
- `lookup_summary.json`;
- `rule_coverage_gaps.json`.

## Order of evaluation (`src/navigator/applicability/engine.py`)

1. **Failed proposals** are never surfaced.
2. **Jurisdiction (M4 only).** A rule is a candidate when its jurisdiction equals the address's resolved state (`"CA"`) or local jurisdiction (`"Los Angeles, CA"`). `postal_city` and `source_dataset` are never read.
   - Unresolved address (no M4 state or city): no rule set is selected, and every surfaceable rule is `unknown`, with reason `jurisdiction_unresolved`.
   - M4 `review_required` with a jurisdiction: that jurisdiction is used, and the explanation ends with a geography note.
3. **Temporal status:** the published `status`, plus `effective_date` vs `as_of` (an in-force record with a later date is `not_yet_effective`).
4. **Coverage clauses (AND)**, then 5. **exemption clauses (OR)**, three-valued (true / false / unknown).
6. **Result:**
   - coverage false or exemption true: omitted (definitely not applicable);
   - pending status: `pending`; not yet effective: `not_yet_effective`;
   - all clauses true: `applies`;
   - otherwise `unknown`, with the machine-readable reasons and missing facts.

## Conditions (`conditions.py`)

Published conditions are free text. Each clause (split at "; ") is classified by fixed patterns:

| class | effect |
|---|---|
| property, decidable | unit counts (duplex, single-family, one/two-family, ≤4 units); year built, only where the rule says "built"/"constructed" (the cutoff year itself is unknown); facility/condominium/mobilehome types vs. an apartment building |
| property, never in the data | certificate of occupancy, owner type, owner occupancy, affordable/subsidy restriction, local-ordinance coverage, local program coverage (RSO/JCO/rent control), rent history, unresolved operative condition: **unknown** |
| situational | a tenancy, event or transaction (termination ground, notice, lease date, tenant status, algorithm product). It does not decide whether the address is covered |
| generic | a restatement of the rule's own scope: true |
| unsupported | **unknown** (`coverage_condition_unsupported` / `exemption_condition_unsupported`) |

**Certificate-of-occupancy age (M5.1, challenge-data proxy).** The challenge README allows `year_built` to stand in for the age of the certificate of occupancy, with buildings in the cutoff year treated as unknown. This applies only to an age test ("certificate of occupancy within the previous N years"). It is a proxy for this dataset, not a legal equivalence.

| year_built vs cutoff year (cutoff = as_of − N years) | result |
|---|---|
| before | older, so the exemption does not apply |
| after | newer, so the exemption applies |
| in the cutoff year, or missing | unknown |

Other certificate tests stay unknown.

## Facts (`facts.py`)

**Units:** the recorded count. When it is missing, a range is taken only from a use description that states one in words ("Five or more apartments", "Apartment 5 to 14 Units", "APT 7-30 UNITS"). Coded NJ descriptions ("3S-F-D-6U-NH") are not parsed. A unit token there that differs from the recorded count (A0227: 2 vs "93U") makes units unknown.

**Use class:** "apartment" when the use description names an apartment or flat building. Owner, tenancy, subsidy and certificate facts are never approximated.

## Extraction gaps carried into M5

A published record whose M3 artifact left a scope condition unresolved is at best `unknown` (`extraction_scope_condition_unresolved`). This covers D027 (§ 12955 subsidy and income-inquiry conditions) and D065.

Not available at all:
- D067's held NJ eviction and deposit records;
- D069's rejected record;
- local rules for Cambridge, Hoboken, Jersey City and Newark.

See `outputs/m5/rule_coverage_gaps.json`.

## Not implemented

`superseded` is not derived: the stricter local rule's own coverage is unknown wherever it matters (RSO luxury exemption, certificate dates).

## Change tests (M6, `src/navigator/changes.py`)

```sh
uv run python scripts/build_changes.py            # -> outputs/m6/changes.json, changes_audit.json, change_summary.json
```

Organizer rule IDs (`CA-ALG-01`, …) are mapped to this project's evidence in `review/m6_change_test_map.json`, each with its basis:

| basis | used for |
|---|---|
| `published_records` | T4: MA bills, published as pending |
| `verified_held_records` | T1: AB 325, D022 records with exact quotes, held only for the missing effective date |
| `supplied_change_fact_plus_verified_effective_date` | T3: FAIR Act, D069 date resolved from verified text; the § 4 record stays rejected |
| `supplied_change_fact_only` | T2: Hoboken and Jersey City; T5: IP 25-21. No supplied source text |

`as_of` tests re-run the M5 engine at both supplied dates. When no verified date exists, the supplied window (before, after] is used. Addresses without an M4 jurisdiction are never placed in an affected set.
