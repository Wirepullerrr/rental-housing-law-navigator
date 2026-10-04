# Rule extraction (Module A): design

*Not legal advice.* This is an automated extraction pipeline for a research prototype.

```
supplied corpus text (manifest-verified; raw text never modified)
  -> canonical view (source_view.py): page furniture replaced by [[PAGE BREAK]], segments numbered [[SEGMENT n]]
  -> versioned prompt (prompt.py)
  -> content-addressed cache, or a live provider call (gemini.py, behind provider.py)
  -> provision inventory; global_scope conditions (evidence verified, quote parts allowed)
  -> per candidate:
       Pydantic ExtractedRule            the model's semantic fields only
       quote parts (quotes.py, RAW text) one verbatim part, or two across exactly one page artifact,
                                         reconstructed into one exact raw span; every evidence field verified
       effective-date evidence           classified; consequences enforced in temporal.py
       status derivation                 deterministic, from enactment_status + admitted effective_date + as_of
       scope propagation                 verified document-level conditions applied by citation, never similarity
       trusted metadata + team_rule_id   from the repository, not from the model
       Pydantic RuleRecord               the internal mirror of the official schema
       official JSON Schema              schema/rule_record.schema.json
       review checks (review.py)         warnings only
  -> completeness checks (coverage.py): inventory coverage closure + subdivision guard
  -> repair targets = their merged union (repair.py)
  -> at most ONE repair request; every target classified in_scope / out_of_scope / uncertain
  -> both completeness checks rerun; every target resolved, or review_required
  -> ExtractionRun audit artifact, document_status complete | review_required
```

A candidate is **accepted only if every stage passes**. Failures are recorded per stage (`pydantic`, `citation`, `temporal`, `effective_date`, `status`, `official schema`, `duplicate`, `repair`) and rejected candidates are never retried or repaired. Review checks add warnings and never change acceptance.

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
| `relative_date_formula` | always null (a computed date is discarded with a warning) | undetermined, so the record is rejected (D069 behaviour, unchanged) |
| `history_note` | always null (a date from the note is discarded with a warning); the enclosing note is kept verbatim as history evidence | unaffected; a note dated after `--as-of` rejects conservatively |
| `operative_condition` | always null | unaffected; published as an unresolved operative condition |
| null, with evidence present | rejected | n/a |

Two structural guards apply, both general and neither statute-specific:

- Evidence the model calls `explicit_operative_date` whose enclosing bracketed note has history structure (an amendment verb such as *added, amended, retitled* followed by "by" an instrument) is treated as `history_note`. A note's word "effective" therefore never becomes the start date of an obligation that may be older.
- Evidence called `history_note` must have that structure or a calendar date. Otherwise it is unresolved and the record is rejected, so a relative formula cannot be laundered into "in force".

Calendar dates are read as written ("August 1, 2025", "2025-08-01" or US numeric "8-1-2025"); nothing is computed.

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

## Known limitations (to revisit in M3)

- Each document is extracted on its own. Cross-document conflicts, `overrides`, and jurisdiction scope for documents that cover several jurisdictions are not handled yet.
- `citation` text is model-produced and is not yet checked against the source (only the quote and the evidence fields are).
- Cross-page quotes may span exactly one page artifact. A passage spanning two page breaks must be split into separate records.
- Records are not deduplicated semantically against parent-level records. Repair can only add records for provisions that had no candidate at all, which limits overlap but does not prove its absence.
