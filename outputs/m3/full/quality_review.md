# M3 full corpus: post-run quality review

These findings were recorded after the run. No extraction output, artifact, prompt, validator or resolver was changed during or after the run, and nothing listed here was resolved. Not legal advice.

**Run:**

- **Configuration:** `gemini-3.8-flash`, thinking `medium`, prompt `v7` / `v7-repair-1`, as_of 2026-10-01.
- **Coverage:** 53 of 54 supplied-text documents. D067 was not sent; see F9.
- **Outputs:**
  - `full_corpus_summary.json`, with per-document audit rows
  - `review_queue.json`
  - `rules.json`
  - `duplicate_clusters.json`

## What was inspected (section 9 checklist)

| Check | Coverage |
|---|---|
| A. Every `review_required` document | All 27 |
| B. Every rejected candidate | All 47, by class below |
| C. Every document that required repair | All 29: D001 D003 D004 D005 D006 D007 D008 D010 D013 D014 D016 D024 D025 D029 D040 D041 D042 D043 D049 D065 D066 D068 D073 D076 D079 D080 D083 D084 D085 |
| D. Relative-date resolutions | D065, D066, D069, D076 |
| D. Massachusetts session-status resolutions | D011, D045, D046, D047 |
| D. Cross-page reconstructed quotations | None needed. No accepted quote spans one of the page artifacts in D001, D014, D043 or D073 (27 in total). |
| D. Named-subject scope mappings | All 19 documents with mappings |
| E. At least 2 accepted rules per remaining complete document | D001 D008 D011 D025 D026 D045 D046 D047 D049 D050 D051 D052 D057 D058 D066 D073 D076 D081 D085. D009, D031, D036, D039, D048, D053 and D078 have verified no-rules results, and each justification was read. |

## Findings needing review (concrete classes)

### F1. A future sunset date is read as a future version, so in-force codified law is rejected (code review)

- **Documents:** D023 (Cal. Civ. Code § 1946.2, statewide just cause) and D024 (§ 1947.12, statewide rent cap).
- **Effect:** 27 of 27 candidates were rejected, so `rules.json` contains **no California statewide just-cause or rent-cap record**.
- **Cause:** the version note reads "Effective January 1, 2026. Repealed as of January 1, 2030, by its own provisions." The version check (`extractor.py`, `status: a version annotation is dated …`) rejects a record if *any* date in its version annotation is after as_of. A repeal (sunset) date is the end of the current text's life, not the start of a later version.
- **Notes:** the rejection fails safe; nothing false was published. Both documents' structural exemptions (D023 12/12, D024 7/7) were verified, and the quotes themselves were exact.

### F2. Whitespace the model inserts next to brackets or parentheses fails verification (code review)

- **D027 (Gov. Code § 12955):** the enactment_status_evidence "( Amended by Stats. 2023 … 2024. )" was given against the source's "(Amended … 2024.)". All 7 candidates were rejected, including source-of-income discrimination.
- **D069 (NJ P.L. 2026, c. 43, § 4, algorithmic rent setting):** the quote has newlines inside the amendment brackets "1[or otherwise facilitate]1". The act's only candidate was rejected. Its relative effective date resolved correctly to 2027-07-01 (not_yet_effective).
- **Cause:** safe normalization collapses existing whitespace runs but does not allow whitespace where the source has none.

### F3. Expired time-limited measures published as `in_force` (false positives in `rules.json`)

There is no end-date or expiry modelling. A dated range whose end is before as_of is still published as `in_force` from its start date:

| Record | Source text | Ended |
|---|---|---|
| D041 `r-233a0e58a8` and duplicate `r-9d1a9baefe` | "Effective March 30, 2020, through January 31, 2024, rent increases are prohibited …" | 2024-01-31 |
| D042 `r-52b71a07e4` | "… effective July 1, 2025, through June 30, 2026 is 3%" | 2026-06-30 |
| D080 `r-b6a7f5745b` | "… effective March 1, 2025 through February 28, 2026 is 1.4%" | 2026-02-28 |

D080 also publishes the current 1.6% rate, so San Francisco now has two in_force annual rates. These 4 records must be excluded or flagged before Module C uses `rules.json`.

### F4. The same text published twice in one document

The repair created records for inventory targets that primary records already covered but did not link by provision id. They carry different citation strings, so the team_rule_id dedupe does not merge them.

- **Identical text (6 clusters):**
  - D041: 4 (rent freeze, utilities, dependent increase, registration surcharge)
  - D043: 2 (Chart A, Chart B)
- **Same rate quoted from a table and from prose (2 clusters):** D083, for the 1.6% rent increase and the 4.2% deposit interest.
- **Related:** the same mechanism triggers false `incomplete_inventory` review reasons in D004 and D080, where the repair duplicates were rejected and the primary records were left unlinked.

See `duplicate_clusters.json`: `same_text_within_document` and `similar_text_within_document`. The `similar_text_within_document` type also lists distinct sibling provisions with parallel wording (D052, D073, D001, D065); those are not duplicates.

### F5. Numeric dates are not recognised in date evidence (code review, low priority)

- **Document:** D083.
- **What happened:** the evidence "3/01/26 – 2/28/27" does not "write" 2026-03-01, so 3 relocation-rate records were rejected.

### F6. Category boundaries applied inconsistently (prompt or policy review, manual)

- **Protected-class housing discrimination:**
  - D012 holds a general protected-class record ("You can't discriminate someone based on their: Race Color …") as `screening_restrictions`. D016 and D068 hold source-of-income records, which the category definition names explicitly.
  - D049 (M.G.L. c. 151B § 4) marks § 4(6), § 4(7), § 4(11) and § 4(18) out of scope as "general fair housing". Only § 4(10) (public assistance or rental subsidy) was extracted.
- **Tenant-notification ordinances:**
  - Boston's HSNA notice duties are `just_cause_eviction` (D013, D014).
  - The Cambridge equivalent (D031) was judged out of scope, with a verified no-rules result.
- **Broker fee:** the MA broker-fee rule (D057) is filed under `application_screening_fees`, which is borderline.

## Expected outcomes of approved policy (not defects)

### F7. Explanatory pages with undated statements are held

- **Volume:** 99 held records in 18 documents; 97 have official explanatory text as their basis.
- **Why:** the M3.1 posture policy does not assume an explanatory page's undated statement is in force.
- **Largest:** D041 (19), D043 (14), D005 (9), D006 (9), D007 (8), D016 (8).
- **Module C effect:** for Berkeley, Boston, LA, SF and Santa Ana, local coverage partly exists only as held records.
- **Also held:** D022 (AB 325, Ch. 338) holds 2 operative-text records, because the text states no effective date. The default California effective date is not inferred.

### F8. Source-capture limitations (starter-pack data, not extraction)

- **D078:** the supplied text is the SF Human Rights Commission homepage, not the Fair Chance Ordinance. The verified no-rules result is correct for that text.
- **D046 and D047:** identical captures of bill S.2983 (8-gram Jaccard 1.0). Each yields one pending record with identical quotes.
- **D009:** the coverage-by-unit-type table yields no rules. Coverage facts have no standalone rule form.

### F9. D067 not processed

- **Request size:** about 161k source characters. That is about 36k input tokens, 3.4% of the 1,048,576-token context.
- **Output projection:** about 98k–123k output and thinking tokens at every observed large-document density. That exceeds the model's 65,536 output-token limit; at about 310 tokens/s it would also exceed the 180 s request timeout.
- **Handling:** not sent, not truncated, not chunked. A chunking design decision is required.

## Confirmed working

- **Citation grounding:**
  - 201/201 published quotes are raw substrings: 183 exact, 18 normalized.
  - The two other quote failures (D005, D040) were stitched from non-contiguous text and were correctly rejected.
- **MA session resolver:**
  - D011: the model said pending; the resolver decided `failed` (193rd session, ended by 2025-02-01).
  - D045, D046, D047: `pending`, because the 194th session is current.
- **Relative dates:**
  - D065: approved 2021-06-18, effective 2022-01-01.
  - D066: approved 2026-01-20, effective 2026-05-01.
  - D069: approved 2026-07-20, effective 2027-07-01.
  - D076 has an unresolved formula, which does not matter because its records are pending.
- **Named-subject mappings:** 185 pairs: 160 applies, 24 does_not_apply, 0 uncertain, 1 unresolved (D065 S1→P6, evidence not source text; not applied). Sampled does_not_apply decisions are well founded:
  - D003: notice, penalty and posting duties
  - D043: payment timing and escrow
  - D068: religious preference vs source of income
  - D076: remedies section
  - D079: removal of housing services

  The D040 AB 1482 exemptions were not attached to the city ordinance's just-cause records.
- **Structural scope:**
  - D073: 12 exemptions document-wide.
  - D025: subdivision containers ((c) → P3–P10, (f) → P16–P21).
  - D052: § 15B(9).
  - D066: the NJ "subsection a." exemptions reached only the $50 fee cap, through verified mappings.
- **Repairs:**
  - At most one repair request per document; there were no integrity violations.
  - D073: 11 subdivision-guard targets and D025: 5 targets were all resolved by accepted records.
  - Of 32 inventory-uncovered targets, 19 stayed unresolved. That is mostly F7 (repair records held) and F4 (duplicates rejected).
