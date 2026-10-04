"""Structural validation of the official starter pack.

Severities:
  ERROR   - the pack is structurally broken; downstream stages cannot trust it.
  WARNING - an inconsistency in the official files worth reporting, but one
            the pipeline can handle explicitly (never silently "fixed").
  INFO    - facts worth surfacing (e.g. jurisdictions with no supplied text).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from navigator import starter_pack as sp

ERROR, WARNING, INFO = "ERROR", "WARNING", "INFO"

# USPS 3-digit prefix ranges for the states in the sample. Used only to flag
# ZIPs that cannot be the property's own ZIP (e.g. an owner mailing address),
# which matters for geocoding later. States not listed are not checked.
_STATE_ZIP_PREFIXES = {
    "CA": tuple(str(p) for p in range(900, 962)),
    "NJ": tuple(f"{p:03d}" for p in range(70, 90)),
    "MA": tuple(f"{p:03d}" for p in range(10, 28)),
}


def zip_outside_state(state: str, zip_code: str) -> bool | None:
    """True if a ZIP is outside its state's USPS prefix range; None if there is no ZIP or the
    state is not checked."""
    prefixes = _STATE_ZIP_PREFIXES.get(state)
    if not zip_code or not prefixes:
        return None
    return not zip_code.startswith(prefixes)


_JURISDICTION_RE = re.compile(r"^[A-Z]{2}$|^[A-Za-z .'-]+, [A-Z]{2}$")


@dataclass
class Finding:
    severity: str
    check: str
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)

    def add(self, severity: str, check: str, message: str) -> None:
        self.findings.append(Finding(severity, check, message))

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def ok(self) -> bool:
        return not self.by_severity(ERROR)


def make_rule_validator(schema: dict) -> Draft202012Validator:
    cls = validator_for(schema, default=Draft202012Validator)
    cls.check_schema(schema)
    return cls(schema)


def rule_records(obj: Any) -> list | None:
    """Unwrap a rules.json payload: a bare list (README section 5) or
    {"rules": [...]} (submission template). None if neither."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("rules"), list):
        return obj["rules"]
    return None


def _schema_errors(validator: Draft202012Validator, record: Any) -> list[str]:
    errs = sorted(validator.iter_errors(record), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errs]


def _load(report: Report, check: str, loader: Callable[[Path], Any], path: Path) -> Any:
    try:
        return loader(path)
    except Exception as exc:  # any failure to read an official file is an ERROR
        report.add(ERROR, check, f"cannot load {path.name}: {type(exc).__name__}: {exc}")
        return None


def _is_iso_date(value: Any) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- rules

def check_schema_and_rules(root: Path, report: Report, doc_ids: set[str]) -> set[str]:
    """Schema, sample record and rules template. Returns template rule ids."""
    schema = _load(report, "schema", sp.read_json, root / sp.SCHEMA_PATH)
    if schema is None:
        return set()
    try:
        validator = make_rule_validator(schema)
    except SchemaError as exc:
        report.add(ERROR, "schema", f"rule_record.schema.json is not a valid JSON Schema: {exc.message}")
        return set()
    props = schema.get("properties", {})
    report.counts["schema"] = {
        "dialect": schema.get("$schema"),
        "required_fields": len(schema.get("required", [])),
        "properties": len(props),
        "categories": props.get("category", {}).get("enum"),
        "statuses": props.get("status", {}).get("enum"),
    }

    sample = _load(report, "sample_rule", sp.read_json, root / sp.SAMPLE_RULE_PATH)
    if sample is not None:
        for msg in _schema_errors(validator, sample):
            report.add(ERROR, "sample_rule", msg)
        _check_rule_provenance(report, "sample_rule", sample, doc_ids)

    template = _load(report, "rules_template", sp.read_json, root / sp.TEMPLATES_DIR / "rules.json")
    if template is None:
        return set()
    records = rule_records(template)
    if records is None:
        report.add(ERROR, "rules_template", "rules.json is neither a list nor {'rules': [...]}")
        return set()
    shape = "list" if isinstance(template, list) else "{'rules': [...]}"
    report.counts["rules_template"] = {"records": len(records), "wrapper": shape}
    if shape != "list":
        report.add(WARNING, "rules_template",
                   f"template wraps records as {shape}; README section 5 says 'a list of rule records'. "
                   "Keep the output wrapper configurable.")
    ids: list[str] = []
    for i, rec in enumerate(records):
        label = rec.get("team_rule_id", f"#{i}") if isinstance(rec, dict) else f"#{i}"
        for msg in _schema_errors(validator, rec):
            report.add(ERROR, "rules_template", f"{label}: {msg}")
        if isinstance(rec, dict):
            ids.append(rec.get("team_rule_id"))
            _check_rule_provenance(report, f"rules_template {label}", rec, doc_ids)
    for rid, n in Counter(ids).items():
        if n > 1:
            report.add(ERROR, "rules_template", f"duplicate team_rule_id {rid!r} ({n}x)")
    return {i for i in ids if isinstance(i, str)}


def _check_rule_provenance(report: Report, check: str, rec: dict, doc_ids: set[str]) -> None:
    doc = rec.get("source_doc_id")
    if doc is not None and doc_ids and doc not in doc_ids:
        report.add(WARNING, check, f"source_doc_id {doc!r} is not in corpus_manifest.csv (placeholder?)")
    span = rec.get("quoted_span", "")
    if isinstance(span, str) and span.startswith("<") and span.endswith(">"):
        report.add(WARNING, check, "quoted_span is a placeholder; the schema alone cannot catch this, "
                                   "so citation checks must verify spans against source text")


# ----------------------------------------------------------------------- addresses

def check_addresses(root: Path, report: Report, covered_states: set[str]) -> set[str]:
    loaded = _load(report, "addresses", sp.read_csv, root / sp.ADDRESSES_PATH)
    if loaded is None:
        return set()
    header, rows = loaded
    missing = [c for c in sp.ADDRESS_COLUMNS if c not in header]
    if missing:
        report.add(ERROR, "addresses", f"missing required columns: {missing}")
        return set()
    extra = [c for c in header if c not in sp.ADDRESS_COLUMNS]
    if extra:
        report.add(WARNING, "addresses", f"unexpected extra columns: {extra}")

    ids = [r["address_id"] for r in rows]
    if any(not i for i in ids):
        report.add(ERROR, "addresses", f"{sum(not i for i in ids)} rows have an empty address_id")
    for aid, n in Counter(ids).items():
        if aid and n > 1:
            report.add(ERROR, "addresses", f"duplicate address_id {aid!r} ({n}x)")

    bad_year, bad_units, bad_state, foreign_zip = [], [], [], []
    by_dataset: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        aid = r["address_id"]
        if r["year_built"] and not re.fullmatch(r"\d{4}", r["year_built"]):
            bad_year.append(aid)
        if r["units"] and not (r["units"].isdigit() and int(r["units"]) > 0):
            bad_units.append(aid)
        if not re.fullmatch(r"[A-Z]{2}", r["state"]):
            bad_state.append(aid)
        if zip_outside_state(r["state"], r["zip"]):
            foreign_zip.append(aid)
        ds = by_dataset[r["source_dataset"]]
        ds["rows"] += 1
        ds["missing_year_built"] += not r["year_built"]
        ds["missing_units"] += not r["units"]
        ds["missing_zip"] += not r["zip"]
    for name, bad in (("year_built (expected YYYY or empty)", bad_year),
                      ("units (expected positive integer or empty)", bad_units),
                      ("state (expected 2-letter code)", bad_state)):
        if bad:
            report.add(ERROR, "addresses", f"malformed {name}: {bad[:10]}{' ...' if len(bad) > 10 else ''}")

    states = Counter(r["state"] for r in rows)
    uncovered = sorted(set(states) - covered_states) if covered_states else []
    if uncovered:
        report.add(WARNING, "addresses", f"states with addresses but no corpus documents: {uncovered}")
    if foreign_zip:
        report.add(WARNING, "addresses",
                   f"{len(foreign_zip)} rows have a ZIP outside their state's range (likely a mailing ZIP; "
                   f"do not use it for geocoding): {foreign_zip[:8]}{' ...' if len(foreign_zip) > 8 else ''}")
    report.counts["addresses"] = {
        "rows": len(rows),
        "by_state": dict(sorted(states.items())),
        "by_postal_city": dict(Counter(f"{r['postal_city']}, {r['state']}" for r in rows).most_common()),
        "by_source_dataset": {k: dict(v) for k, v in sorted(by_dataset.items())},
        "missing_year_built": sum(not r["year_built"] for r in rows),
        "missing_units": sum(not r["units"] for r in rows),
        "missing_zip": sum(not r["zip"] for r in rows),
        "out_of_state_zip": len(foreign_zip),
    }
    return {i for i in ids if i}


# -------------------------------------------------------------------------- corpus

def check_corpus(root: Path, report: Report) -> list[dict[str, str]]:
    """Manifest, links_only.csv and corpus/text/. Returns manifest rows."""
    loaded = _load(report, "manifest", sp.read_csv, root / sp.MANIFEST_PATH)
    if loaded is None:
        return []
    header, rows = loaded
    missing = [c for c in sp.MANIFEST_COLUMNS if c not in header]
    if missing:
        report.add(ERROR, "manifest", f"missing required columns: {missing}")
        return []

    for did, n in Counter(r["doc_id"] for r in rows).items():
        if not did:
            report.add(ERROR, "manifest", f"{n} rows have an empty doc_id")
        elif n > 1:
            report.add(ERROR, "manifest", f"duplicate doc_id {did!r} ({n}x)")
    for r in rows:
        if not _JURISDICTION_RE.match(r["jurisdictions"]):
            report.add(WARNING, "manifest", f"{r['doc_id']}: unexpected jurisdiction format {r['jurisdictions']!r}")

    urls = defaultdict(list)
    for r in rows:
        urls[re.sub(r"^https?://(www\.)?", "", r["url"]).rstrip("/")].append(r["doc_id"])
    for url, docs in urls.items():
        if len(docs) > 1:
            report.add(WARNING, "manifest", f"same source URL listed under {docs}: {url}")

    availability = {r["doc_id"]: sp.classify_source(r) for r in rows}
    _check_text_files(root, report, rows)
    _check_links_only(root, report, rows, availability)

    by_jur: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_jur[r["jurisdictions"]][availability[r["doc_id"]]] += 1
    no_text = sorted(j for j, c in by_jur.items() if not c[sp.SUPPLIED_TEXT])
    if no_text:
        report.add(INFO, "manifest", f"jurisdictions with no supplied local text: {no_text}")
    failed = [f"{r['doc_id']} ({r['status']})" for r in rows if availability[r["doc_id"]] == sp.CAPTURE_FAILED]
    if failed:
        report.add(INFO, "manifest", f"capture attempted but failed, no text supplied: {failed}")

    report.counts["manifest"] = {
        "rows": len(rows),
        "by_availability": dict(Counter(availability.values())),
        "by_source_type": dict(Counter(r["source_type"] for r in rows)),
        "by_capture": dict(Counter(r["capture"] for r in rows)),
        "by_jurisdiction": {j: dict(c) for j, c in sorted(by_jur.items())},
    }
    return rows


def _check_text_files(root: Path, report: Report, rows: list[dict[str, str]]) -> None:
    text_dir = root / sp.TEXT_DIR
    on_disk = sorted(p for p in text_dir.iterdir() if p.is_file()) if text_dir.is_dir() else []
    if not text_dir.is_dir():
        report.add(ERROR, "corpus_text", f"{sp.TEXT_DIR} directory is missing")
    referenced: set[Path] = set()
    hash_mismatches: list[str] = []
    chars = 0
    for r in rows:
        if not r["text_file"]:
            continue
        did, path = r["doc_id"], (root / sp.CORPUS_DIR / r["text_file"]).resolve()
        referenced.add(path)
        if r["status"] != "ok":
            report.add(WARNING, "corpus_text", f"{did}: has text_file but status is {r['status']!r}")
        if not path.is_file():
            report.add(ERROR, "corpus_text", f"{did}: referenced text file {r['text_file']} does not exist")
            continue
        raw = path.read_bytes()
        try:
            doc = sp.parse_corpus_text(raw.decode("utf-8"))
        except UnicodeDecodeError:
            report.add(ERROR, "corpus_text", f"{did}: {r['text_file']} is not valid UTF-8")
            continue
        except ValueError as exc:
            report.add(ERROR, "corpus_text", f"{did}: {r['text_file']}: {exc}")
            continue
        chars += len(doc.body)
        if not doc.body.strip():
            report.add(ERROR, "corpus_text", f"{did}: {r['text_file']} has an empty body")
        if doc.source_url != r["url"]:
            report.add(ERROR, "corpus_text", f"{did}: header SOURCE {doc.source_url!r} != manifest url {r['url']!r}")
        if sp.header_timestamp_to_manifest(doc.retrieved) != r["retrieved_at"]:
            report.add(WARNING, "corpus_text",
                       f"{did}: header RETRIEVED {doc.retrieved!r} != manifest retrieved_at {r['retrieved_at']!r}")
        if r["sha256"] and hashlib.sha256(raw).hexdigest() != r["sha256"]:
            hash_mismatches.append(did)
    if hash_mismatches:
        report.add(WARNING, "corpus_text",
                   f"manifest sha256 does not match {len(hash_mismatches)}/{len(referenced)} text files; the hash "
                   "appears to be of the original capture (PDF/HTML), so text integrity cannot be verified "
                   "against it")
    unreferenced = [p.name for p in on_disk if p.resolve() not in referenced]
    if unreferenced:
        report.add(WARNING, "corpus_text", f"files in {sp.TEXT_DIR} not referenced by the manifest: {unreferenced}")
    report.counts["corpus_text"] = {"files_on_disk": len(on_disk), "referenced": len(referenced),
                                    "body_characters": chars}


def _check_links_only(root: Path, report: Report, rows: list[dict[str, str]],
                      availability: dict[str, str]) -> None:
    loaded = _load(report, "links_only", sp.read_csv, root / sp.LINKS_ONLY_PATH)
    if loaded is None:
        return
    header, links = loaded
    missing = [c for c in sp.LINKS_ONLY_COLUMNS if c not in header]
    if missing:
        report.add(ERROR, "links_only", f"missing required columns: {missing}")
        return
    manifest = {r["doc_id"]: r for r in rows}
    listed = set()
    for link in links:
        did = link["doc_id"]
        listed.add(did)
        if did not in manifest:
            report.add(ERROR, "links_only", f"{did}: not in corpus_manifest.csv")
            continue
        if availability[did] == sp.SUPPLIED_TEXT:
            report.add(ERROR, "links_only", f"{did}: listed as link-only but manifest supplies text")
        for col in ("jurisdictions", "url", "source_type"):
            if link[col] != manifest[did][col]:
                report.add(WARNING, "links_only", f"{did}: {col} differs from manifest")
    unlisted = sorted(d for d, a in availability.items() if a != sp.SUPPLIED_TEXT and d not in listed)
    if unlisted:
        report.add(WARNING, "links_only", f"manifest rows without text missing from links_only.csv: {unlisted}")
    report.counts["links_only"] = {"rows": len(links)}


# --------------------------------------------------------------- dev + templates

def check_change_tests(root: Path, report: Report) -> set[str]:
    tests = _load(report, "change_tests", sp.read_json, root / sp.CHANGE_TESTS_PATH)
    if tests is None:
        return set()
    if not isinstance(tests, list):
        report.add(ERROR, "change_tests", "change_tests.json must be a list of test objects")
        return set()
    ids: list[str] = []
    for i, t in enumerate(tests):
        if not isinstance(t, dict):
            report.add(ERROR, "change_tests", f"entry #{i} is not an object")
            continue
        tid = t.get("test_id") or f"#{i}"
        ids.append(tid)
        for key in ("test_id", "title", "type", "expected_behavior"):
            if not isinstance(t.get(key), str) or not t.get(key):
                report.add(ERROR, "change_tests", f"{tid}: missing or non-string {key!r}")
        rule_ids = t.get("rule_ids")
        if not (isinstance(rule_ids, list) and rule_ids and all(isinstance(x, str) for x in rule_ids)):
            report.add(ERROR, "change_tests", f"{tid}: rule_ids must be a non-empty list of strings")
        if t.get("type") == "as_of":
            before, after = t.get("as_of_before"), t.get("as_of_after")
            if not (_is_iso_date(before) and _is_iso_date(after)):
                report.add(ERROR, "change_tests", f"{tid}: as_of test needs ISO as_of_before/as_of_after")
            elif date.fromisoformat(before) >= date.fromisoformat(after):
                report.add(ERROR, "change_tests", f"{tid}: as_of_before is not before as_of_after")
        elif not _is_iso_date(t.get("as_of")):
            report.add(ERROR, "change_tests", f"{tid}: missing ISO 'as_of' date")
        for key in ("states", "conflict_with"):
            if key in t and not isinstance(t[key], list):
                report.add(ERROR, "change_tests", f"{tid}: {key!r} must be a list")
    for tid, n in Counter(ids).items():
        if n > 1:
            report.add(ERROR, "change_tests", f"duplicate test_id {tid!r}")
    report.counts["change_tests"] = {
        "tests": len(ids),
        "ids": ids,
        "by_type": dict(Counter(t.get("type") for t in tests if isinstance(t, dict))),
    }
    return set(ids)


def check_lookups_template(root: Path, report: Report, address_ids: set[str], rule_ids: set[str]) -> None:
    obj = _load(report, "lookups_template", sp.read_json, root / sp.TEMPLATES_DIR / "lookups.json")
    if obj is None:
        return
    if not (isinstance(obj, dict) and isinstance(obj.get("lookups"), dict)):
        report.add(ERROR, "lookups_template", "expected {'as_of': ..., 'lookups': {address_id: [...]}}")
        return
    if not _is_iso_date(obj.get("as_of")):
        report.add(ERROR, "lookups_template", f"as_of {obj.get('as_of')!r} is not an ISO date")
    entries = 0
    for aid, items in obj["lookups"].items():
        if address_ids and aid not in address_ids:
            report.add(WARNING, "lookups_template", f"address {aid!r} not in sample_addresses.csv")
        if not isinstance(items, list):
            report.add(ERROR, "lookups_template", f"{aid}: value must be a list")
            continue
        for item in items:
            entries += 1
            if not isinstance(item, dict):
                report.add(ERROR, "lookups_template", f"{aid}: entry is not an object")
                continue
            rid = item.get("team_rule_id")
            if not isinstance(rid, str):
                report.add(ERROR, "lookups_template", f"{aid}: entry lacks team_rule_id")
            elif rule_ids and rid not in rule_ids:
                report.add(WARNING, "lookups_template",
                           f"{aid}: {rid!r} is not in the rules template (templates are illustrative only)")
            if item.get("result") not in sp.LOOKUP_RESULT_VALUES:
                report.add(ERROR, "lookups_template", f"{aid}/{rid}: result {item.get('result')!r} not in "
                                                      f"{list(sp.LOOKUP_RESULT_VALUES)}")
            if not isinstance(item.get("explanation"), str):
                report.add(ERROR, "lookups_template", f"{aid}/{rid}: explanation must be a string")
            if not isinstance(item.get("conflict_flag"), bool):
                report.add(ERROR, "lookups_template", f"{aid}/{rid}: conflict_flag must be a boolean")
    report.counts["lookups_template"] = {"as_of": obj.get("as_of"), "addresses": len(obj["lookups"]),
                                         "entries": entries}


def check_changes_template(root: Path, report: Report, address_ids: set[str], test_ids: set[str]) -> None:
    obj = _load(report, "changes_template", sp.read_json, root / sp.TEMPLATES_DIR / "changes.json")
    if obj is None:
        return
    if not isinstance(obj, dict):
        report.add(ERROR, "changes_template", "expected {test_id: {...}}")
        return
    for tid, body in obj.items():
        if test_ids and tid not in test_ids:
            report.add(WARNING, "changes_template", f"{tid!r} is not a test_id in change_tests.json")
        if not isinstance(body, dict):
            report.add(ERROR, "changes_template", f"{tid}: value must be an object")
            continue
        if "affected_address_ids" not in body:
            report.add(ERROR, "changes_template", f"{tid}: missing affected_address_ids")
        for key in ("affected_address_ids", "conflict_flag_address_ids"):
            if key not in body:
                continue
            ids = body[key]
            if not (isinstance(ids, list) and all(isinstance(x, str) for x in ids)):
                report.add(ERROR, "changes_template", f"{tid}: {key} must be a list of strings")
                continue
            unknown = [x for x in ids if address_ids and x not in address_ids]
            if unknown:
                report.add(WARNING, "changes_template", f"{tid}: {key} not in sample: {unknown}")
        if "notes" in body and not isinstance(body["notes"], str):
            report.add(ERROR, "changes_template", f"{tid}: notes must be a string")
    report.counts["changes_template"] = {"tests": sorted(obj)}


# ------------------------------------------------------------------------- driver

def validate_starter_pack(root: Path = sp.REPO_ROOT) -> Report:
    report = Report()
    manifest = check_corpus(root, report)
    doc_ids = {r["doc_id"] for r in manifest}
    covered_states = {r["jurisdictions"][-2:] for r in manifest}
    rule_ids = check_schema_and_rules(root, report, doc_ids)
    address_ids = check_addresses(root, report, covered_states)
    test_ids = check_change_tests(root, report)
    check_lookups_template(root, report, address_ids, rule_ids)
    check_changes_template(root, report, address_ids, test_ids)
    return report
