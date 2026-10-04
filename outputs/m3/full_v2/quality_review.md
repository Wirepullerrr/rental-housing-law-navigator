# M3.3 targeted corpus repair: quality review (`outputs/m3/full_v2`)

This rebuild applies the M3.3 deterministic fixes to the 53 documents already processed, replayed offline from the provider cache with zero new calls, and adds D067 through the new large-document mode. The original full run (`outputs/m3/full/`) is unchanged and remains the benchmark. Per-candidate changes against it are in `delta_vs_baseline.json`. Not legal advice.

## What changed against the benchmark (201 -> 228 published records)

| Change | Records | Documents |
|---|---|---|
| Recovered: future repeal date no longer read as a later version (validity window) | +27 | D023 (19, Civ. Code § 1946.2, just cause), D024 (8, § 1947.12, rent cap) |
| Recovered: layout-only whitespace next to parentheses in the enactment-status evidence (the quotes themselves were exact) | +7 | D027 (Gov. Code § 12955) |
| Removed: expired, now historical | −3 | D041 (2020–2024 rent freeze), D042 (3%, ended 2026-06-30), D080 (1.4%, ended 2026-02-28) |
| Removed: exact same-source duplicate | −4 | D041: freeze (second copy, also expired) and registration surcharge; D043: Chart A and Chart B |
| **Net** | **+27** | |

D005 has one more held record: its quote matched after a line break between "(b)" and "." was ignored.

D067 added 86 candidates and 0 published records (see below).

## Verifier over the whole cached corpus

| Check | Exact | Normalized | Layout-normalized | Failed |
|---|---|---|---|---|
| Quotes, all 433 candidates | 398 | 30 | 1 (D005 [6]) | 4 |
| Status, version and effective-date evidence | 341 | 9 | 7 (D027) | 1 |

The layout tier changed exactly 8 checks across 54 documents. Each was inspected and differs from the source only by whitespace next to a parenthesis or bracket. The tier did not let any previously failing quote of different text pass.

D069 is **not** recovered. Its source text reads "engage in 1 or otherwise facilitate 1" with no brackets; the model inserted "[" and "]" characters. That is not a layout difference, so the rejection is correct.

> Correction to the benchmark `quality_review.md` (F2): that file attributed D069 to inserted whitespace inside existing brackets. The source has no brackets at all.

## Documents whose repair could not be served from the cache (zero-call rule)

The fixes change which records validate, and that legitimately changes these documents' repair requests. A changed request has a new cache identity, and reusing the old response would be a cache-identity violation, so these repairs did not run:

| Document | New repair need | Effect |
|---|---|---|
| D023 | 2 subdivision-guard targets: § 1946.2(d)(1)(B), (d)(2)(B) | 19 records published; document review_required |
| D027 | 5 named-subject pairs (S1 "government rent subsidy" coverage of § 12955(o)(1)(A)/(B)(i)/(B)(ii); S3 source-of-income inquiry carve-out of (a) and (k)) | 7 records published **without** those two conditions attached; review_required |
| D005 | one more mapping pair, from the newly validated held record | all D005 records held (none published); its earlier mapping decisions are not reapplied |

One repair request each (about $0.02–0.05) would settle these. It was not sent because M3.3 forbids calls for the 53 processed documents.

## D067: large-document mode

- **Chunks:** 9 structural chunks of 10.6k–19.9k characters, cut at section headings. They tile the raw text exactly. All 9 were extracted, followed by one repair built from excerpts 2, 3, 6 and 7 only.
- **Cost:** $0.963 estimated, under the $1.50 gate.
- **Inventory:** 148 items, 66 in scope.
- **Candidates:** 86 in total:
  - 83 held: just_cause_eviction 53, security_deposits 20, rent_increase_limits 6, screening_restrictions 3, application_screening_fees 1;
  - 3 rejected: two "cross-page" quotes made of two parts inside one segment, and one unverifiable status evidence.
- **Why held:** the document is an official explanatory guide ("TRUTH IN RENTING", posture `official_explanatory_page`), and its statements give no effective date. Under the M3.1 posture policy they are held, not published.
- **Scope:** 8 of 9 conditions verified. One (c03.S1, the Security Deposit Law owner-occupied exemption) has evidence that is not source text, so it was not applied. All 48 named-subject pairs were decided "applies".
- **Review_required:** 2 inventory targets remain (Judgment for Possession, Self-help Evictions): the repair classified them in scope, but their only records are held.
- **M4 consequence:** D067 is the corpus's only text for the NJ Anti-Eviction Act grounds (N.J.S.A. 2A:18-61.1) and most NJ security-deposit law. No published record covers them. Publishing them would need a policy decision about official explanatory restatements of codified statutes; this milestone does not make it.

## Review queue (27 documents; `review_queue.json`)

| Code | Documents |
|---|---|
| held_rule / unresolved_effective_date | 19 (explanatory pages and D022, by policy) |
| incomplete_inventory | 12 |
| scope_mapping_uncertain | 4 |
| uncertain_scope | 4 |
| repair_failed | 3 (D005, D023, D027: no cached repair and no live call allowed) |
| uncovered_subdivision | 1 |

## Duplicate audit (`duplicate_clusters.json`)

- **suppressed_exact_duplicate (4):** suppressed by the pipeline; one record published each.
- **possible_semantic_duplicate (31):** never merged.
  - Real candidates: D083 table vs. prose (1.6% rate, 4.2% deposit interest), and D041 containment pairs (utilities, dependents).
  - Many others are distinct sibling provisions with parallel wording (D052, D073, D065), or D067 held records.
- **Cross-document clusters (all audit only):**
  - D046/D047: the same bill captured twice;
  - CA Civ. Code § 1950.6: D026 published, D005 held;
  - LAMC 151.09: D041/D043;
  - LA utilities rule: D041/D042;
  - SF 1.6% rate: D080/D083.

## Lower-priority items left in the queue (not addressed, per M3.3 section 15)

- D083: numeric dates ("3/01/26"), 3 relocation-rate records rejected.
- D049 and D031: category-boundary questions.
- Cross-document semantic duplicate merging.
