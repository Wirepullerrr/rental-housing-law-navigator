# Setup

*Not legal advice.* The official participant guide is the repository `README.md` and is left unmodified.

## Environment

The project uses Python 3.11 (pinned in `.python-version`) and is managed with [uv](https://docs.astral.sh/uv/). The canonical dependency definition is `pyproject.toml` together with `uv.lock`.

```sh
uv sync                  # create/update .venv exactly from uv.lock (includes the dev group: pytest)
uv add <package>         # add a runtime dependency (updates pyproject.toml and uv.lock)
uv add --dev <package>   # add a development-only dependency
```

Do not edit `uv.lock` by hand.

`requirements.in` and `requirements.txt` are the pip-compatible lockfiles from M1. They are kept for now but do not include the M2 dependencies.

## Commands

```sh
uv run python scripts/validate_starter_pack.py        # structural checks; exit 1 on any ERROR
uv run pytest                                         # offline: all network access is blocked in tests
```

## Rule extraction (M2: one document at a time)

Live extraction calls Gemini. The key is read only from the `GEMINI_API_KEY` environment variable. To provide it locally, copy `.env.example` to `.env` (gitignored) and fill it in, then load it with `--env-file`:

```sh
uv run --env-file .env python scripts/extract_rules.py --doc-id D052 --live          # calls the API only on a cache miss
uv run --env-file .env python scripts/extract_rules.py --doc-id D052 --live --force  # bypass the cache
uv run python scripts/extract_rules.py --doc-id D052                                 # offline: re-validate the cached response
```

Without `--live` the script never contacts a provider. `--doc-id` takes exactly one document; there is no all-documents mode. The audit artifact goes to `outputs/m2/<doc_id>_extraction.json`, and raw responses are cached in `cache/extraction/`. The model can be chosen with `--model` (default in `src/navigator/extraction/config.py`). See [extraction.md](extraction.md) for the design.

## Jurisdiction resolution (M4)

Resolves every sample address to its state and municipal jurisdiction with the U.S. Census Geocoder (no key). See [jurisdiction.md](jurisdiction.md).

```sh
uv run python scripts/resolve_jurisdictions.py          # offline: cached Census responses only
uv run python scripts/resolve_jurisdictions.py --live   # fetch Census responses that are not cached
```

Raw Census responses are cached in `cache/census/` (gitignored). Audited overrides and manual-review notes live in `review/m4_jurisdiction_review.json`.

## Layout

| Path | Purpose |
|---|---|
| `corpus/`, `data/`, `dev/`, `schema/`, `submission_templates/` | Official starter pack. These files are read-only, and nothing in this project writes to them. |
| `src/navigator/` | Project code: `starter_pack.py` (loaders), `validation.py` (structural checks), `extraction/` (Module A), `jurisdiction/` (M4: address → jurisdiction). |
| `scripts/` | Command-line entry points. |
| `tests/` | pytest suite. |
| `cache/extraction/`, `cache/census/` | Local, gitignored caches of raw LLM and Census responses, keyed by content. Offline reruns work only on a machine that has the entries. |
| `review/` | Human-reviewed inputs: M4 jurisdiction overrides and review notes. |
| `outputs/` | Generated artifacts and validation reports. |
| `docs/` | Project documentation, including the [starter-pack audit](starter_pack_audit.md). |
