# Rule extraction (Module A): design

*Not legal advice.* This is an automated extraction pipeline for a research prototype.

```
supplied corpus text (manifest-verified; raw text never modified)
  -> canonical view (source_view.py): page furniture replaced by [[PAGE BREAK]], segments numbered [[SEGMENT n]]
  -> versioned prompt (prompt.py)
  -> content-addressed cache, or a live provider call (gemini.py, behind provider.py)
  -> document posture (posture.py) and enactment dates written in the raw text (temporal.py)
  -> provision inventory (ids, anchors, roles); global_scope conditions (evidence verified, quote
     parts allowed, targeted by provision id)
  -> per candidate:
       Pydantic ExtractedRule            the model's semantic fields only, with provision links
       quote parts (quotes.py, RAW text) one verbatim part, or two across exactly one page artifact,
                                         reconstructed into one exact raw span; every evidence field verified
       enactment status evidence         required (verified) for pending and failed
       effective-date evidence           classified; relative formulas resolved deterministically (temporal.py)
       status derivation                 deterministic, from enactment_status + effective_date + posture + as_of
       legislative status (proposals)    pending vs failed decided deterministically (legislative.py)
       scope propagation                 structural / explicit-reference conditions by provision id (scope.py);
                                         named-subject conditions only after a verified mapping
       trusted metadata + team_rule_id   from the repository, not from the model
       Pydantic RuleRecord               the internal mirror of the official schema
       official JSON Schema              schema/rule_record.schema.json
       review checks (review.py)         warnings only
  -> completeness checks (coverage.py): inventory coverage closure + subdivision guard + scope challenges
  -> repair targets = their merged union (repair.py)
  -> at most ONE repair request; every target classified in_scope / out_of_scope / uncertain
  -> both completeness checks rerun; every target resolved, or review_required
  -> ExtractionRun audit artifact, document_status complete | review_required
```

A candidate is **accepted only if every stage passes**. Failures are recorded per stage (`pydantic`, `provision link`, `citation`, `temporal`, `effective_date`, `status`, `official schema`, `duplicate`, `repair`) and rejected candidates are never retried or repaired. A candidate whose **only** problem is an unresolved time of application is **held**: it is preserved with all its evidence, never published, and makes the document `review_required`. Review checks add warnings and never change acceptance.

## Prompt v6 contract (M3.1)

The remedy rule of v5 is unchanged. v6 adds, in both prompts, the following (`EXTRACTION_PROMPT_VERSION = "v6"`, `REPAIR_PROMPT_VERSION = "v6-repair-1"`, so every v5 cache entry misses):

- **Document posture.** `document = {posture, evidence}`; the values are listed under *Source posture* below.
- **Inventory items.** Each item has an `id` (`P1`, `P2`, ... in document order), a verbatim `anchor` (its first words) and a functional `role`. The old `rule_indices` field is gone: records link the other way.
- **Records.** Each record has:
  - `provision_ids`, at least one;
  - `source_basis` (`operative_text`, `official_bill_summary`, `official_bill_history` or `official_explanatory_text`);
  - `enactment_status_evidence`, required for `pending` and `failed`.
- **Global scope conditions.** Each one names its `source_provision_id` and its `governed_provision_ids`. The value `null` means the condition governs every provision of the document. The free-text `governs` field is gone.
- **Functional test.** A provision is substantive, not a mere definition, when removing it would change whether conduct is lawful, permitted, required, prohibited or covered. Examples are grounds for termination, conditions for a rent increase, screening criteria, and triggers of an obligation. This holds however the document words it.
- **Bill status and summary pages.** For a bill without operative text, an official title, summary or history that states a concrete proposed change in a category becomes a `pending` record (or `failed`, when the document shows the bill failed). The quote is that official text, and no bill language, threshold, exemption or date may be invented.
- **Empty results.** `no_rules_justification = {reason, evidence}` is required when there are no rules.

The prompt still contains no document-specific wording.

## Source posture (`posture.py`)

The posture is internal audit metadata; the RuleRecord schema is unchanged. Its possible values are:

- `codified_current_law`
- `enacted_session_law`
- `pending_bill`
- `failed_bill`
- `bill_status_or_summary_page`
- `official_explanatory_page`
- `unknown`

The declared posture is **established** only if its evidence verifies in the raw text. A declared `codified_current_law` is not established when the raw text itself records an enactment date ("Approved June 18, 2021"), because that marks session-law material. Nothing is inferred from the URL.

- **Effect on status:** only established codified current law lets an enacted rule with no dated evidence count as `in_force`. For every other posture, including `unknown`, such a rule is **held** for temporal resolution and is not published as in force on every query date (D022 behaviour).
- **Consistency check:** a posture that implies an enactment status (codified or session law → enacted, pending bill → pending, failed bill → failed) is compared with each record. A mismatch is a review reason and is never corrected silently.

A record whose `source_basis` is not operative text is labelled at the start of its published `requirement`, e.g. "[Basis: official bill summary, not operative legal text] ...". The basis is also kept in the audit.

## Empty results

A document with no candidate at all is `complete` only when the response gives a `no_rules_justification` whose evidence verifies in the source. Otherwise it is `review_required` (D011 behaviour). Inventory provisions of `uncertain` scope also make the document `review_required`.

## Remedy and enforcement scope (prompt v5)

This rule is frozen for M3 and applies to both prompts. It is general: it names no document or citation.

A remedy or enforcement provision is a rule record **only** when it creates a concrete legal consequence directly tied to an in-scope housing rule. Such a record takes the category of that underlying rule. Concrete consequences include:

- monetary or statutory damages;
- a tenant or landlord entitlement;
- an affirmative defense;
- injunctive or equitable relief;
- attorney-fee or cost liability;
- another concrete consequence of violating the rule (e.g. a notice made void, or forfeiture of the right to retain a deposit).

None of the following is a standalone record: authorization to file a lawsuit, choice of forum, government enforcement authority, cumulative-remedies boilerplate, severability, generic procedure, or administrative machinery that creates no obligation, right or consequence applicable to a rental. These are classified `out_of_scope` with a reason. Where legally useful, they may be mentioned in the `interaction` of the records they affect.

The existing validation artifacts already satisfy this rule, so they were not rerun:

- **D052:** the treble damages in § 15B(2)(a) and (7), the forfeiture in (6) and the anti-waiver rule in (8) are in scope, under `security_deposits`.
- **D073:** § 98.0709(b) to (f) (relief, the affirmative defense, treble damages, damages and attorney fees) and the void-notice and anti-waiver provisions are in scope, under `just_cause_eviction`. § 98.0709(a) (authorization to sue), (g) (cumulative remedies) and (h) (City enforcement) are out of scope.

Because the prompt wording changed, the version moved to v5 (`v5-repair-1` for the repair prompt). Cached v4 responses therefore do not match v5 requests.

## Prompt v4 contract

The primary prompt (`prompt.py`, `EXTRACTION_PROMPT_VERSION = "v4"`) works in three generated steps, in this order:

1. **`provisions`:** an inventory of every substantive provision. Each item has `ref`, a neutral `summary`, a `scope` decision (`in_scope`, `out_of_scope` or `uncertain`), a `category` if in scope, a `reason` if not, and `rule_indices` (the positions in `rules` of the records produced from it).
2. **`global_scope`:** every exemption or coverage condition that governs a whole document, division or section, with `governs` (a provision ref, or `null` for the whole document) and verbatim `evidence_parts`.
3. **`rules`:** one record per distinct obligation, prohibition, entitlement, remedy, procedural requirement or monetary limit in scope. The record's `citation` names the inventory provision it comes from. Field order puts `citation` and `quote_parts` before the claims they support.

Contract details:

- A rule's own `coverage_conditions` and `exemptions` hold only conditions specific to that rule. Global conditions are attached by code. A rule escapes one only through a `scope_carve_outs` entry with verbatim evidence.
- **Quote parts:** see below. Every other evidence field is one verbatim passage within one segment.
- **Claims and quotes:** every assertion in `requirement` and `key_value` must be supported by the record's own quote.
- **Time:** six things are kept apart: operative text, a calendar effective date in operative text, a relative date formula, a legislative or codification history note, amendment or version history, and an operative (non-calendar) condition. See *Effective-date evidence* below.
- **`interaction`:** only a stated relationship with another legal regime. A mere citation of another statute or section does not count.
- **`conflict_note`:** only genuine conflicts or ambiguities that apply at the same time, never version history.
- **No model confidence.** It is not requested. If supplied anyway it is dropped with a warning, and the published `confidence` is `null`.

The thinking level (`--thinking-level`, default `medium`) is part of the cache key and the audit record. On D073 in M2.6, `low` used no thinking tokens and omitted core provisions.

## Page artifacts and quote parts (`source_view.py`, `quotes.py`)

PDF-derived text carries running headers and footers in the middle of legal text. They are detected **structurally**, with no legal or document-specific knowledge. A block is treated as page furniture only when all of the following hold:

- It has at least two lines, each of which repeats in the document once digits are normalized.
- The same block recurs at least 3 times.
- Across those occurrences the text is identical except for numbers that strictly increase (a page counter).
- The occurrences are at least 8 non-blank lines apart.

In the text the model reads, each block is replaced by `[[PAGE BREAK]]`, and each stretch of text between blocks is labelled `[[SEGMENT n]]`. Segments and artifacts tile the raw text exactly. Each removed block is recorded verbatim with its raw offsets (`source_view.artifacts`, `source_view.segments`). Over the 54 local documents this removes only page furniture (in D001, D014, D043 and D073), and consecutive segments are always separated by exactly one artifact.

The model supports each record with **`quote_parts`**, each a `segment_id` plus verbatim `quoted_text`:

- **One part (normal case):** the unchanged single-span policy. The text must be found verbatim (exact, or after safe NFC/whitespace normalization) in the raw source. It is looked up first in the named segment; a match elsewhere is accepted with a review warning. A match that overlaps removed page furniture is rejected.
- **Two parts (cross-page case):** all of the following must hold, otherwise the candidate is rejected:
  - the parts come from adjacent segments n and n+1, in source order;
  - exactly one detected page artifact lies between them;
  - part 1 runs flush to the end of segment n and part 2 starts flush at the beginning of segment n+1, so nothing but whitespace and that one artifact lies between them. No legal text may be skipped.
- **More than two parts:** always rejected.

For two parts, Python publishes `quoted_span = raw[start of part 1 : end of part 2]`. This is an exact, contiguous substring of the untouched source. It therefore **contains the running header**, exactly as the source does. The header is identified separately in the audit (`citation.crossed_artifacts`, `citation.parts` with raw offsets), so a UI can later display a legal-only rendering. Figure review checks run on the legal text of the parts, not on the header. As a final invariant, every published `quoted_span` must be a substring of the raw source.

A single quote that copies the marker, or that joins text across a removed artifact, is rejected with a diagnosis. Nothing is stitched, fuzzily matched or repaired.

## Completeness checks and the single repair pass (`coverage.py`, `repair.py`)

Order of one pipeline run:

```
primary extraction -> primary validation
  -> inventory coverage closure  +  subdivision guard
  -> merged, deduplicated repair targets
  -> at most ONE repair request
  -> normal validation of repair candidates
  -> coverage closure  +  subdivision guard, rerun
  -> complete | review_required
```

There is never a second repair request in one run. Unresolved targets after repair make the document `review_required`; they do not trigger another request.

**Inventory coverage closure.** Every `in_scope` inventory ref (ranges such as "(a)-(c)" expanded) is mapped to candidates deterministically:

- **Citation match:** the candidate's citation names that ref or a subdivision of it.
- **Declared link:** the inventory lists the candidate's index, *and* the candidate's citation names an ancestor of the ref (e.g. a "(b)(1)" record declared for "(b)(1)(A)"). A declared index whose candidate cites something unrelated is not counted; it is reported as a link mismatch.

**Subdivision guard.** The closure can only be as fine as the inventory. On D073 v4, the model inventoried § 98.0709 as one item, so records for (b) to (f) "covered" it, while (a), (g) and (h) had no record. When an in-scope ref is covered by accepted records only through some of its subdivisions, the guard scans the source near those records. It looks for line-initial labels of the same style ("(a)", "(A)" or "(1)") that continue the cited sequence, stopping at section headings. Each such subdivision without an accepted record is reported with its source range. Subdivisions the inventory itself lists as `out_of_scope` or `uncertain` are left to that decision. Offline, over earlier artifacts, the guard flags only genuine gaps in the D073 runs, and nothing on D052 or D069.

**Repair targets** are the union of both checks, merged by provision. A target found by both carries both sources.

- `inventory_uncovered`: an in-scope inventory ref with no candidate at all.
- `subdivision_guard`: a subdivision found by the guard, with no candidate at all.

A ref whose only candidates were rejected is never a target: rejected candidates are not retried. It stays unresolved, and the document is `review_required`.

**The repair request** carries the same source view, the verified global-scope metadata and the target list. The list says how each target was found; structural targets are explicitly described as possibly out of scope. The response schema is `{"target_resolutions": [...], "rules": [...]}`:

- Every target gets a classification: `in_scope`, `out_of_scope` or `uncertain`, with a reason. For `out_of_scope`, verbatim `evidence_parts` from the target provision itself are also required.
- Records are allowed only for targets classified `in_scope`. The model is never told that a target must become a rule.

Every repair candidate goes through exactly the same evaluation as a primary candidate, with no special acceptance path. The one extra check can only reject: a repair candidate must cite a target, and not one that the same response classified as `out_of_scope` or `uncertain`. Duplicates of earlier accepted records are rejected by `team_rule_id`.

**Resolution.** After repair, both checks are rerun and each target gets a final resolution:

- `resolved_by_accepted_rule`: an accepted record cites the target or one of its subdivisions.
- `resolved_out_of_scope`: classified `out_of_scope` with a reason and verified verbatim evidence. For a guard target, the evidence must lie inside that subdivision's source text.
- `unresolved`: anything else, with the reason recorded. Examples: classified `uncertain`, omitted from the response, every candidate rejected, `out_of_scope` without verified evidence, or repair failed or did not run.

Per target, the audit (`repair.targets`) records:

- the ref and its sources;
- the pre-repair state;
- the repair's classification and reason;
- the evidence check;
- the produced and accepted candidate indices;
- the final resolution.

`coverage.repair_targets` counts targets before repair, resolved by a rule, resolved out of scope, and still unresolved. `coverage.after_primary`, `coverage.after_repair` and `coverage.final` record both checks at each stage.

**Repair cache identity.** The repair key includes:

- the primary cache key;
- `repair_prompt_version`, which is separate from the primary prompt version so that a repair-only change keeps the primary key;
- the rendered repair prompt;
- the complete target set as a sorted canonical list (`repair_targets`, each ref with its sources).

A different target set is always a cache miss, and the cached primary response is reused. If repair is needed but cannot run (no cached response and no live provider, or a provider error), this is recorded, never silently skipped.

`document_status` is `complete` only when all of the following hold:

- the inventory is present and well-formed;
- every target is resolved;
- no in-scope ref is left with no candidate or with only rejected candidates;
- the guard reports no unresolved subdivision.

Otherwise it is `review_required`, and `review_reasons` explains why.

## Effective-date evidence (`temporal.py`)

The model quotes `effective_date_evidence` and classifies it with `effective_date_evidence_kind`. The evidence must verify verbatim in the raw source; fabricated evidence rejects the candidate. Python then enforces the consequences:

| kind | effective_date | status |
|---|---|---|
| `explicit_operative_date` | kept only if the date is written in the evidence itself; otherwise rejected | from the date and `--as-of` |
| `relative_date_formula` | the deterministic resolution below, or null; a date the model computed is never used | from the resolved date and `--as-of`; unresolved: the record is held |
| `history_note` | always null (a date from the note is discarded with a warning); the enclosing note is kept verbatim as history evidence | unaffected; a note dated after `--as-of` rejects conservatively |
| `operative_condition` | always null | unaffected; published as an unresolved operative condition |
| null, with evidence present | rejected | n/a |

Two structural guards apply, both general and neither statute-specific:

- Evidence the model calls `explicit_operative_date` whose enclosing bracketed note has history structure (an amendment verb such as *added, amended, retitled* followed by "by" an instrument) is treated as `history_note`. A note's word "effective" therefore never becomes the start date of an obligation that may be older.
- Evidence called `history_note` must have that structure or a calendar date. Otherwise it is unresolved and the record is rejected, so a relative formula cannot be laundered into "in force".

Calendar dates are read as written ("August 1, 2025", "2025-08-01" or US numeric "8-1-2025"). The only computed date is the relative-date resolution below.

### Relative-date resolver (`resolve_relative`, rule `first-day-of-nth-month-next-following/v1`)

The resolver is deliberately narrow; it is not a general date interpreter. It resolves a formula only when all of the following hold:

1. **The formula evidence verifies in the raw text** and states exactly one formula of the grammar *take effect | become effective | become operative* [on] *the first day of the* ⟨N⟩ *month next following* [*the date of*] *enactment | approval*. N is an ordinal from 1 to 24, written in words or digits; case and whitespace do not matter. Verified evidence that matches this grammar is treated as a relative formula, whatever the model called it.
2. **The sentence has no qualification the grammar does not model:**
   - another effective or operative date;
   - an applicability clause;
   - a leading *except*, *notwithstanding*, *unless* or *subject to*.

   A trailing proviso about anticipatory administrative action is allowed.
3. **The raw text records exactly one distinct base date** for that anchor. A base date is *approved*, *enacted* or *signed* [*by the Governor*] followed by a written calendar date. *Approved* serves both anchors; *enacted* and *signed* serve *enactment*. The base date is always raw source text, never a model value.

The result is the first day of the Nth calendar month after the base date's month. For example, June 18, 2021 + seventh month gives 2022-01-01, and July 20, 2026 + twelfth month gives 2027-07-01.

Every resolution records an audit in `temporal.relative_resolution`: the formula, its evidence and offsets, the base date and its evidence and offsets, the candidate base dates, the rule id, the resolved date, and the outcome with its reason. Anything outside the grammar stays unresolved with its evidence kept, and the record is held. Examples are "90 days after enactment" and "the third month following enactment" (without "next").

## Review checks (`review.py`, warnings only)

- **Figures missing from the quote:** monetary amounts, percentages, durations and calendar dates in `requirement` or `key_value` that are absent from the `quoted_span`. Number words, "per cent" and the drafting style "ten (10) days" are normalized first.
- **Coverage:** handled by coverage closure (above), which also lists inventory links that disagree with the rule's citation, and provisions marked `uncertain`.
- **Unresolved operative conditions**, **history-note dates not used**, and **quote parts found outside the named segment**.

Not implemented, because it would be brittle or statute-specific: entailment checks for non-numeric claims, keyword detection of version history in `conflict_note`, and validating citation formats. Known false positives: fractions written in words, and ordinals ("first month" vs "1 month").

## Trust boundary

| Field(s) | Source |
|---|---|
| `source_doc_id`, `source_url`, `jurisdiction`, retrieval date | corpus manifest and text header |
| `level` | derived from the manifest jurisdiction (`MA` gives state; `Boston, MA` gives city) |
| `team_rule_id` | `ids.py`, deterministic |
| `status` | `normalize.derive_status`, deterministic and relative to `--as-of` (default 2026-10-01) |
| `effective_date` | the model's date only if `temporal.py` admits it (explicit operative date written in verified evidence) |
| `quoted_span` | the verified raw source text located from the model's quote parts (`quotes.py`) |
| `overrides` | always `[]` at single-document extraction; precedence belongs to the rule engine |
| `category`, `title`, `requirement`, `key_value`, `coverage_conditions`, `exemptions`, `interaction`, `citation`, `conflict_note` | model |
| `version_note`, `version_evidence`, `effective_date_evidence(_kind)`, `operative_conditions`, `scope_carve_outs`, `global_scope`, provision inventory | model; verified and kept in the audit artifact. Global scope and operative conditions are also composed into the published `exemptions` and `coverage_conditions` text |
| `confidence` | never used; published as `null` |

If the model returns any trusted field, the value is dropped and a warning is recorded. `conflict_flag` is true exactly when the model reports a `conflict_note`.

**Status.** The model never sees the query date. It reports `enactment_status` (enacted, pending or failed). Python admits an `effective_date` only under the rules in *Effective-date evidence*. Relative wording (e.g. "the first day of the twelfth month next following the date of enactment") is never converted into a date. Such rules are rejected with a `status:` reason until a deterministic resolver exists.

## Two schemas

- **Generation schema** (`models.generation_json_schema`): `ExtractionResponse` as a self-contained JSON Schema with `$ref`s inlined, reduced to widely supported keywords (`type`, `properties`, `required`, `items`, `enum`, `anyOf`, `description`, `minimum`, `maximum`). It is sent to Gemini as `response_json_schema` for constrained decoding.
- **Authoritative schemas**: Pydantic `ExtractedRule` and `RuleRecord` (strict, with `extra="forbid"`), plus the official `schema/rule_record.schema.json`. Constraints that the generation schema omits (patterns, `minLength`, no extra fields) are enforced locally, so nothing depends on the API honouring them.

## Citation integrity (`citation.py`)

- `exact_match`: the span is a byte-for-byte substring of the source body.
- `normalized_match`: the span matches after NFC normalization and with each whitespace run treated as one space. This tolerates double spaces and line breaks in the legal text, nothing else. The published `quoted_span` is then the exact source text at the matched offsets, which is a true substring. The model's original span is kept in the audit record.
- `failed`: anything else (paraphrase, changed word, retyped quote marks). The candidate is rejected.

## `team_rule_id`

`"r-" + sha256(canonical [source_doc_id, category, citation, verified quoted_span])[:10]`, with NFC normalization, collapsed whitespace and casefolding. The id is keyed on the verified span rather than the model's paraphrase so that it stays stable across reruns. It has 40 bits, so collisions are about n²/2⁴¹ (about 5e-7 at 1,000 rules). Identical inputs produce identical ids by design, which is how duplicates are flagged. These ids are not the organizer ids in `dev/change_tests.json`.

## Cache (`cache.py`)

The key is a SHA-256 over canonical JSON of: the pass (`primary` or `repair`), source content hash, view hash, doc id, provider, model, prompt version, a hash of the rendered prompt, a hash of the generation schema, and the generation settings. A repair key also includes the primary key. It contains no timestamps. Entries store the raw response text, so every rerun re-parses and re-validates with the current deterministic code. `--force` bypasses the cache. An entry whose stored key fields do not hash to its file name is rejected.

## Provider, retries, network safety

- The pipeline depends only on the `StructuredLLMProvider` protocol (`provider.py`). `gemini.py` is the only module that imports `google.genai`, and only the `--live` CLI path loads it. Tests use a `FakeProvider`.
- `gemini.py` uses the Gemini **Interactions API** (`client.interactions.create`). Each call is one plain model interaction: `input` is the prompt, `system_instruction` is set, structured output goes through `response_format={"type": "text", "mime_type": "application/json", "schema": …}`, and `store=False`. There are no tools, agents, search or function calling. Only `status == "completed"` with text output is accepted. Interactions ending `failed`, `incomplete`, `budget_exceeded` or `cancelled` are rejected and not retried.
- The Interactions client retries on its own by default (up to 3 HTTP attempts). `HttpRetryOptions` cannot switch that off, because the SDK rewrites `attempts=0` to `1`. The adapter therefore sets the client's retry config to `"none"`. The error classes and the retry config are not exported publicly by google-genai 2.28.0, so the adapter imports them from the SDK's private `_gaos` package. That dependency is confined to `gemini.py`, pinned by `uv.lock`, and covered by HTTP-level tests (`tests/test_gemini_interactions.py`, which run the real SDK against an `httpx.MockTransport`).
- Retries cover transient failures only: HTTP 429, any 5xx, and network timeouts or connection errors. Other 4xx responses (400/401/403/404) are never retried, and neither are validation failures. At most 3 HTTP attempts are made in total. The wait is 30 s after the first failure and 60 s after the second; if the API supplies a delay, through a `Retry-After` header or `google.rpc.RetryInfo.retryDelay`, that delay is used instead. A requested delay longer than 120 s is not waited out: the run stops and reports it. Because the SDK's internal retry is disabled, this is the only retry layer.
- Tests block every non-loopback socket and DNS lookup and unset `GEMINI_API_KEY`. Each attempt is recorded, both by the patched socket functions and by a Python audit hook, and any test that records one fails at teardown, even if library code caught and wrapped the resulting error.

## Corpus runner (`corpus.py`, `scripts/run_corpus.py`)

The runner is for M3 and processes an explicitly selected set of documents. It does not replace the single-document CLI.

```
uv run python scripts/run_corpus.py --list
uv run --env-file .env python scripts/run_corpus.py --doc-ids D065,D001 --out-dir outputs/m3/stage1 --live --budget 0.75
```

- **Selection:** only the given doc_ids are processed, one at a time, in the given order. There is no "all documents" option, and discovery reads the manifest rather than assuming a corpus size.
- **Pipeline:** each document runs the normal single-document pipeline: primary pass, both completeness checks, at most one repair, and a final status.
- **Budget gate:** every provider request goes through `MeteredProvider`.
  - It refuses to *start* a request once the stage's estimated new spend has reached `--budget`; a request already in flight is never interrupted.
  - Spend is recorded in `spend_ledger.json` in the stage directory, so the total survives interruptions.
  - It also refuses a second primary or repair request for the same document, raising an integrity violation.
- **Cost estimates:** `config.PRICE_PER_MTOK` holds $0.75 and $3.75 per 1M input and output tokens, with thinking billed as output, applied to API-reported usage. Cache hits cost nothing. These are estimates, never the account's billing balance.
- **Resume:**
  - An existing per-document artifact whose primary cache identity matches the current inputs is reused unchanged.
  - One whose identity differs is never overwritten unless `--replace <doc_id>` is given; the old file is then kept as `*.superseded-<time>.json`.
  - Provider responses already obtained are reused through the content-addressed cache.
- **Stops:** the batch stops at the budget gate, on a provider failure, or on an integrity violation. The integrity checks catch:
  - an accepted quote that is not raw source text;
  - a schema-invalid published rule;
  - propagation of an unverified global-scope condition;
  - a cache identity mismatch;
  - lost raw provenance;
  - a second provider request for one document.
- **What does not stop it:** a `review_required` document, or a document-level failure such as a missing supplied text. These are recorded and the batch continues.
- **Outputs:** `<out-dir>/documents/<doc_id>_extraction.json`, plus `<out-dir>/<stage name>_summary.json`, which is rewritten after every document. The summary has one row per document (status, calls, cache hits, quote and scope counts, tokens and estimated new cost) and corpus totals.

## Prompt v7 contract (M3.2)

v7 keeps all of v6 and changes two things (`EXTRACTION_PROMPT_VERSION = "v7"`, `REPAIR_PROMPT_VERSION = "v7-repair-1"`, so every v6 cache entry misses):

- **Document-level conditions** also declare a `scope_mode` (`structural`, `explicit_reference` or `named_subject`) and a verbatim `scope_quote` naming what they govern; for a lettered list, the `scope_quote` is the list's lead-in. The prompt says a named mechanism must never be stretched to nearby provisions that impose a different duty.
- **Legislative status:** the model reports legislative evidence (a status line, a history entry, a session label) in `enactment_status_evidence`. A referral for study does not by itself end a bill. The system decides pending vs failed.

The repair response gains `scope_mappings`, one decision per requested (condition, provision) pair: `applies`, `does_not_apply` or `uncertain`, each with a reason and evidence. The prompt has no document-specific examples and no bill numbers.

## Scope modes (`scope.py`)

Python decides each verified condition's mode from its verified **scope words**: the `scope_quote`, which must be raw text inside or up to 4,000 characters before the evidence. If there is no usable `scope_quote`, the evidence itself is used, plus the lead-in of a lettered list item. The model's declared mode is recorded but not trusted.

| mode | recognised by | propagation |
|---|---|---|
| `structural` | a container as the subject or frame of the clause: "This Division shall not apply", "the provisions of this section do not apply", "for purposes of this section", "nothing in this act". A cross-reference such as "as defined in Division 1 of this Code" does not count. | Deterministic, to a verified container: the whole document (governed ids null), or the smallest token-prefix group of the stating provision's ref. That group must contain every proposed id and every in-scope member. |
| `explicit_reference` | references parsed from the words: "Sections 6 and 7", "subsection (4)". A label-only reference is read relative to the stating section. | Deterministic, **only** to inventory provisions inside those references. Proposed ids outside them are recorded and not used. |
| `named_subject` | anything else, e.g. "The rent cap does not apply". Also any structural or explicit claim that cannot be verified. | **Never automatic.** Each proposed (condition, provision) pair whose provision is linked by a record becomes a `scope_mapping_challenge`. |

**Scope-mapping challenges:**

- They travel in the **same single repair request** as the other targets. The repair cache key names the sorted pair set.
- **Outcomes:**
  - `applies` needs a reason. Any evidence given must verify.
  - `does_not_apply` needs a reason and verified evidence.
  - `uncertain`, an omitted pair, unverified evidence or a repair that did not run each leave the pair `unresolved`. Nothing is propagated and the document is `review_required`.
- **After the repair:** only `applies` pairs propagate, and the affected records' scope text is recomposed. A mapping never creates a record.
- **Integrity:** the corpus runner treats a named-subject condition applied without an `applies` mapping as a violation.

## Legislative status of proposals (`legislative.py`)

Pending vs failed for a `pending` or `failed` record is decided here, never by the model. An `enacted` record keeps the enacted and effective-date logic.

- **Explicit terminal action:** a source line whose whole text is a terminal disposition gives `failed`. Examples are "No further action taken", "Withdrawn", "Leave to withdraw", "Rejected", "Defeated" and "Vetoed". A qualified line such as "No further action taken on the extension …" does not count.
- **Massachusetts session resolver (`ma-general-court-session/v1`):** it applies only for MA jurisdictions, using the **first** General Court label on the page, which is the bill's own.
  - **Session years:** "193rd (2023 - 2024)" or "194th (Current)". The n-th General Court sits in 2023 + 2(n − 193) and the following year. Written years must agree, or the label is ignored.
  - **Expiry:** a session counts as ended from February 1 after its second year. A query date on or after that, with no enactment evidence, gives `failed` (`session_expired`).
  - **Open session:** a query date within the session's years gives `pending` (`current_session_pending`), even after a study order or the end of formal sessions.
  - **Refiles:** a later-session refile or similar bill is a separate object and does not change this.
- **Enactment conflict:** if the text records an enactment (approval, signing or chapter line) but the record says pending or failed, the status is undetermined and the record is held for review.
- **Other cases:** with no deterministic basis (another jurisdiction, no label), the record keeps the model's status with verified evidence (`model_status_with_evidence`).
- **Overrides:** overriding the model from pending to failed (expired session, terminal action) is recorded as a warning. Overriding failed to pending is a review reason.

Each decision is recorded in `candidate.legislative`: session, years, query date, enactment and terminal evidence, resolver version, status basis, and any conflict.

## Provision ids, scope targeting and scope challenges (M3.1)

- **Propagation:** document-level conditions propagate by provision id only. Since v7, the provision ids are the ones `scope.py` verified (see Scope modes); named-subject conditions propagate only through verified mappings. A condition applies to a record when it is document-wide, or when the record links one of the provision ids it reaches. No citation, title or substring is compared, so descriptive titles containing commas are harmless. A condition whose governed ids are empty or not in the inventory is not propagated, and the document is `review_required`. An applied condition that shares no id with its record is a corpus-runner integrity violation.
- **Coverage closure:** a record counts for an in-scope provision only if it links the provision's id **and** its citation names the provision, a subdivision or an ancestor. A citation alone never counts.
- **Scope challenge (`coverage.scope_challenges`):** an `out_of_scope` inventory item is challenged when **all** of the following hold:
  - its role claims it is not operative (definition, procedure, history, boilerplate or uncertain);
  - a neighbouring item is in scope;
  - its own source region contains an enumeration;
  - the region also contains normative or grounds/conditions language.

  The region runs from its verified anchor to the next anchor. Glossaries (two or more "means"), and provisions stating a verified scope condition, are not challenged. A challenge publishes nothing: it becomes a `scope_challenge` repair target in the same single repair request. The repair classifies it:
  - **in_scope:** records must link the target's provision id;
  - **out_of_scope:** needs a reason and evidence inside the provision's region, and the result is preserved;
  - **uncertain:** the document stays `review_required`.

## Code-only replay (`replay.py`, `scripts/replay_cached.py`)

A cached prompt-v5 response can be re-evaluated under the current post-processing without a provider request. The adapter re-expresses what v5 stated:

- inventory ids follow inventory position;
- record links come from `rule_indices`;
- `governs` becomes document-wide when it is null. A string maps to an id only when it is **exactly** equal to one inventory ref; anything else is left untargeted.

Everything v5 never stated (posture, anchors, roles, source basis, status evidence) stays undeclared and is listed in `legacy_replay`. Responses older than v4 can only be replayed through the relative-date resolver (`--resolver-only`).

## Known limitations (to revisit in M3)

- Each document is extracted on its own. Cross-document conflicts, `overrides`, and jurisdiction scope for documents that cover several jurisdictions are not handled yet.
- `citation` text is model-produced and is not yet checked against the source (only the quote and the evidence fields are).
- Cross-page quotes may span exactly one page artifact. A passage spanning two page breaks must be split into separate records.
- The relative-date resolver supports one formula family. Other formulas stay unresolved, and their records are held.
- No unstated default effective date is derived (e.g. a state's default operative date for statutes). Session-law records without a stated date are held, and Module C must supply any change-test facts.
- **Structural containers** are verified from ref token prefixes. A governed-ids value of null with "this section" in a multi-section document is accepted as the whole document. The prompt instructs the model to use null only for the whole document.
- **Legislative status** has a deterministic session resolver for Massachusetts only. Elsewhere, pending vs failed rests on the model's verified status evidence.
- The scope-challenge heuristic is conservative and keyword-assisted. It cannot see a provision that the inventory omits altogether, or an out_of_scope item whose anchor is not source text.
- Records are not deduplicated semantically against parent-level records. Repair can only add records for provisions that had no candidate at all, which limits overlap but does not prove its absence.
