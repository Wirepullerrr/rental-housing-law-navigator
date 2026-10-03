# Setup

*Not legal advice.* The official participant guide is the repository `README.md` and is left unmodified.

## Environment

Python 3.11 (pinned in `.python-version`). Dependencies are declared in `requirements.in` and locked, cross-platform, in `requirements.txt`.

With [uv](https://docs.astral.sh/uv/):

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt   # Windows
uv pip install --python .venv/bin/python -r requirements.txt           # macOS / Linux
```

With plain pip:

```sh
python3.11 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows (.venv/bin/python elsewhere)
```

To change dependencies, edit `requirements.in`, then run `uv pip compile --universal requirements.in -o requirements.txt`.

## Commands

```sh
.venv/Scripts/python scripts/validate_starter_pack.py        # structural checks; exit 1 on any ERROR
.venv/Scripts/python scripts/validate_starter_pack.py --json outputs/reports/starter_pack_validation.json
.venv/Scripts/python -m pytest
```

## Layout

| Path | Purpose |
|---|---|
| `corpus/`, `data/`, `dev/`, `schema/`, `submission_templates/` | Official starter pack. These files are read-only, and nothing in this project writes to them. |
| `src/navigator/` | Project code. `starter_pack.py` holds the loaders; `validation.py` holds the structural checks. |
| `scripts/` | Command-line entry points. |
| `tests/` | pytest suite. |
| `outputs/` | Generated artifacts and validation reports. |
| `docs/` | Project documentation, including the [starter-pack audit](starter_pack_audit.md). |
