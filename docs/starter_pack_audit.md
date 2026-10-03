# Starter-pack audit (M1)

Audited against commit `ed19a38`. The counts below come from `scripts/validate_starter_pack.py`; the machine-readable report is `outputs/reports/starter_pack_validation.json`. No official file was modified.

## Counts

| Item | Count |
|---|---|
| Corpus manifest rows | 87: 54 with supplied text, 32 link-only, 1 capture failed (D056, HTTP 403) |
| `corpus/text/` files | 54, all referenced by the manifest, none missing |
| `links_only.csv` rows | 33, exactly the manifest rows without text (32 link-only + D056) |
| Sample addresses | 500 (CA 250, NJ 140, MA 110); `address_id` values are unique |
| Change tests | 5 (T1–T5): 2 `as_of`, 1 `boundary`, 1 `pending`, 1 `negative` |

Each supplied text file starts with `SOURCE:` and `RETRIEVED:` header lines, and both agree with the manifest for all 54 files.

## Discrepancies and data issues

1. **`rules.json` wrapper.** README §5 describes "a list of rule records", but `submission_templates/rules.json` uses `{"rules": [...]}`. The output wrapper therefore has to stay configurable.
2. **The manifest `sha256` matches none of the 54 `.txt` files.** It probably hashes the original PDF/HTML capture, so it can't be used to check text integrity.
3. **The `capture` column is not an availability signal.** D056 has `capture=yes` but no text (`status` is `manual: 403 ...`). Availability is decided from `text_file` and `status`.
4. **Duplicate source.** D002 (Berkeley) and D037 (Jersey City) are the same Morgan Lewis article. Both are link-only.
5. **Placeholders in the templates.** The sample and template rule use `source_doc_id: "D0xx"` and a placeholder `quoted_span`, and both still pass the schema. The schema cannot detect a fabricated span, so citation checks are needed.
6. **The templates are illustrative, not answer keys.** `lookups.json` puts NJ rule `r-0007` on A0001, which is a Los Angeles address, and references `r-0031`, which is not defined. `changes.json` lists A0001 under T3, an NJ test. None of these templates should be used as test oracles.
7. **ZIPs.** 130 addresses have no ZIP (all San Francisco and all Cambridge rows). 27 NJ rows have out-of-state ZIPs, for example Newark `11219`, which is probably a mailing ZIP. ZIP should not be relied on for geocoding.
8. **Postal city.** The postal city is not always the legal city. Boston's 60 rows use 10 different `postal_city` values: "Boston" (23 rows) plus neighborhoods such as Dorchester (13) and Roxbury (7). One San Diego row says "San Ysidro".
9. **Possible unit-count conflict.** A0227 (Hoboken) has `units=2`, but its `use_description` is `13B-93U-2C-G`. Units must not be inferred from these codes unless that decision is made explicitly and documented.
10. **Gaps in the change-test sources.** These matter for M2 and M3:
    - **T2:** There is no supplied local text for the Hoboken or Jersey City algorithmic ordinances. Their sources (D032–D035, D037) are link-only or `check-terms`, and the only Jersey City text (D036) does not mention them. Newark also has no supplied text.
    - **T5:** The only source for the MA ballot question (D059, WBUR) is link-only.
    - **T1:** SB 763 does not appear in any supplied text. AB 325 (D022) shows "Chaptered 10/06/25" but gives no explicit effective date.
    - **T3:** D069 defines the effective date relatively ("first day of the twelfth month next following the date of enactment", approved July 20, 2026). It also contains a municipal-conflict clause (§6b), which is relevant to the preemption flag.
    - **IDs:** `dev/change_tests.json` refers to organizer IDs (`CA-ALG-01`, `HOB-ALG-01`, …) that are not `team_rule_id` values, and no mapping between them is supplied.

## Address missingness (verified)

| Source dataset | Rows | No year_built | No units |
|---|---|---|---|
| Alameda County parcels (Berkeley) | 40 | 40 | 40 |
| Boston Property Assessment FY2026 | 60 | 8 | 60 |
| Cambridge Property Database FY2026 | 50 | 0 | 0 |
| DataSF (San Francisco) | 80 | 2 | 0 |
| LA County eGIS parcels | 80 | 6 | 3 |
| NJOGIS MOD-IV (Jersey City, Hoboken, Newark) | 140 | 106 | 139 |
| SANDAG/SanGIS (San Diego) | 50 | 50 | 0 |
