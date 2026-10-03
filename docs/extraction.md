# Rule extraction (Module A): design

*Not legal advice.* This is an automated extraction pipeline for a research prototype.

```
supplied corpus text (manifest-verified)
  -> versioned prompt (prompt.py)
  -> content-addressed cache, or a live provider call (gemini.py, behind provider.py)
  -> provision inventory (audit only)
  -> per candidate:
       Pydantic ExtractedRule            the model's semantic fields only
       citation check                    quoted_span, date evidence and version evidence must be in the source
       status derivation                 deterministic, from enactment_status + effective_date + as_of
       trusted metadata + team_rule_id   from the repository, not from the model
       Pydantic RuleRecord               the internal mirror of the official schema
       official JSON Schema              schema/rule_record.schema.json
       review checks (review.py)         warnings only
  -> ExtractionRun audit artifact (outputs/m2/<doc_id>_extraction.json)
```

A candidate is **accepted only if every stage passes**. Failures are recorded per stage (`pydantic`, `citation`, `effective_date`, `status`, `official schema`, `duplicate`) and are never retried or repaired. Review checks add warnings and never change acceptance.

## Prompt v2 contract

The prompt (`prompt.py`, `EXTRACTION_PROMPT_VERSION = "v2"`) works in two steps. The model first fills `provisions`, an inventory of every operative provision with its official category or `null`. It then emits one record per distinct obligation, prohibition, entitlement, remedy, procedural requirement or monetary limit in scope. Both fields are generated in that order.

- **Exemptions** are attached to the `exemptions` field of each record they limit; they are not records of their own.
- **Claims and quotes:** every assertion in `requirement` and `key_value` must be supported by that record's `quoted_span`. If one quote is not enough, the model takes a longer quote or splits the record.
- **Temporal versions:** amendment history goes in `version_note`, with the verbatim annotation in `version_evidence`. It never goes in `conflict_note`. An amendment date applies only to the amended wording and is not the obligation's `effective_date`.
- **`interaction`** is only for relationships with other legal regimes (preemption, override, "in addition to"). Internal cross-references do not count.
- **`conflict_note`** is only for genuine conflicts or ambiguities that apply at the same time.
- **`confidence`** is per record and must not be a single default value.

`conflict_flag` is derived from `conflict_note` alone, so version history can never set it. If `version_evidence` is given, it must be found in the source, or the candidate is rejected. If a verified version annotation is dated after `--as-of`, the record is rejected conservatively, because the extracted (latest) wording may not apply yet.

## Review checks (`review.py`, warnings only)

- **Figures missing from the quote:** monetary amounts, percentages, durations and calendar dates in `requirement` or `key_value` that are absent from the `quoted_span`. Number words, "per cent" and the drafting style "ten (10) days" are normalized first.
- **Uniform confidence:** identical confidence on 3 or more accepted records.
- **Uncited provisions:** provisions the model marked in scope in its inventory that no accepted record cites.

Not implemented, because it would be brittle or statute-specific: entailment checks for non-numeric claims, keyword detection of version history in `conflict_note`, and validating citation formats. Known false positives: fractions written in words, ordinals ("first month" vs "1 month"), and inventory references written in a different style from the citation.

## Trust boundary

| Field(s) | Source |
|---|---|
| `source_doc_id`, `source_url`, `jurisdiction`, retrieval date | corpus manifest and text header |
| `level` | derived from the manifest jurisdiction (`MA` gives state; `Boston, MA` gives city) |
| `team_rule_id` | `ids.py`, deterministic |
| `status` | `normalize.derive_status`, deterministic and relative to `--as-of` (default 2026-10-01) |
| `overrides` | always `[]` at single-document extraction; precedence belongs to the rule engine |
| `category`, `title`, `requirement`, `key_value`, `coverage_conditions`, `exemptions`, `interaction`, `citation`, `quoted_span`, `confidence`, `conflict_note` | model |
| `version_note`, `version_evidence`, provision inventory | model; kept in the audit artifact only, not in rule records |

If the model returns any trusted field, the value is dropped and a warning is recorded. `conflict_flag` is true exactly when the model reports a `conflict_note`.

**Status.** The model never sees the query date. It reports `enactment_status` (enacted, pending or failed) and an `effective_date` only when the document states an explicit calendar date. A non-null `effective_date` requires verbatim `effective_date_evidence` that is found in the source. Relative wording (e.g. "the first day of the twelfth month next following the date of enactment") is never converted into a date by the model. Such rules are rejected with a `status:` reason until a deterministic resolver exists.

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

The key is a SHA-256 over canonical JSON of: source content hash, doc id, provider, model, prompt version, a hash of the rendered prompt, a hash of the generation schema, and the generation settings. It contains no timestamps. Entries store the raw response text, so every rerun re-parses and re-validates with the current deterministic code. `--force` bypasses the cache. An entry whose stored key fields do not hash to its file name is rejected.

## Provider, retries, network safety

- The pipeline depends only on the `StructuredLLMProvider` protocol (`provider.py`). `gemini.py` is the only module that imports `google.genai`, and only the `--live` CLI path loads it. Tests use a `FakeProvider`.
- `gemini.py` uses the Gemini **Interactions API** (`client.interactions.create`). Each call is one plain model interaction: `input` is the prompt, `system_instruction` is set, structured output goes through `response_format={"type": "text", "mime_type": "application/json", "schema": …}`, and `store=False`. There are no tools, agents, search or function calling. Only `status == "completed"` with text output is accepted. Interactions ending `failed`, `incomplete`, `budget_exceeded` or `cancelled` are rejected and not retried.
- The Interactions client retries on its own by default (up to 3 HTTP attempts). `HttpRetryOptions` cannot switch that off, because the SDK rewrites `attempts=0` to `1`. The adapter therefore sets the client's retry config to `"none"`. The error classes and the retry config are not exported publicly by google-genai 2.28.0, so the adapter imports them from the SDK's private `_gaos` package. That dependency is confined to `gemini.py`, pinned by `uv.lock`, and covered by HTTP-level tests (`tests/test_gemini_interactions.py`, which run the real SDK against an `httpx.MockTransport`).
- Retries cover transient failures only: HTTP 429, any 5xx, and network timeouts or connection errors. Other 4xx responses (400/401/403/404) are never retried, and neither are validation failures. At most 3 HTTP attempts are made in total. The wait is 30 s after the first failure and 60 s after the second; if the API supplies a delay, through a `Retry-After` header or `google.rpc.RetryInfo.retryDelay`, that delay is used instead. A requested delay longer than 120 s is not waited out: the run stops and reports it. Because the SDK's internal retry is disabled, this is the only retry layer.
- Tests block every non-loopback socket and DNS lookup and unset `GEMINI_API_KEY`. Each attempt is recorded, both by the patched socket functions and by a Python audit hook, and any test that records one fails at teardown, even if library code caught and wrapped the resulting error.

## Known limitations (to revisit in M3)

- Each document is extracted on its own. Cross-document conflicts, `overrides`, and jurisdiction scope for documents that cover several jurisdictions are not handled yet.
- `confidence` is the model's own estimate, not a calibrated probability.
- `citation` text is model-produced and is not yet checked against the source (only `quoted_span` and the date evidence are).
