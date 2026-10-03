"""Invariants of the official starter pack, and checks that the validator
actually fails when those invariants are broken."""

from __future__ import annotations

import copy
import csv
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from navigator import starter_pack as sp
from navigator.validation import ERROR, make_rule_validator, rule_records, validate_starter_pack

ROOT = sp.REPO_ROOT
PACK_DIRS = ("corpus", "data", "dev", "schema", "submission_templates")


@pytest.fixture(scope="module")
def report():
    return validate_starter_pack(ROOT)


@pytest.fixture(scope="module")
def rule_validator():
    return make_rule_validator(sp.read_json(ROOT / sp.SCHEMA_PATH))


@pytest.fixture(scope="module")
def manifest():
    return sp.read_csv(ROOT / sp.MANIFEST_PATH)[1]


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    for d in PACK_DIRS:
        shutil.copytree(ROOT / d, tmp_path / d)
    return tmp_path


def _errors(root: Path) -> list[str]:
    return [f.message for f in validate_starter_pack(root).findings if f.severity == ERROR]


# ------------------------------------------------------------------ whole pack

def test_official_pack_has_no_structural_errors(report):
    assert report.ok, [f.message for f in report.findings if f.severity == ERROR]


def test_cli_exits_zero_on_official_pack():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/validate_starter_pack.py")],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT: PASS" in proc.stdout


# ---------------------------------------------------------------------- schema

def test_schema_categories_and_statuses(rule_validator):
    props = rule_validator.schema["properties"]
    assert set(props["category"]["enum"]) == {
        "rent_increase_limits", "just_cause_eviction", "security_deposits",
        "application_screening_fees", "screening_restrictions", "algorithmic_rent_setting",
    }
    assert set(props["status"]["enum"]) == {"in_force", "not_yet_effective", "pending", "failed"}


def test_sample_rule_is_schema_valid(rule_validator):
    rule_validator.validate(sp.read_json(ROOT / sp.SAMPLE_RULE_PATH))


def test_every_template_rule_is_schema_valid(rule_validator):
    records = rule_records(sp.read_json(ROOT / sp.TEMPLATES_DIR / "rules.json"))
    assert records
    for rec in records:
        rule_validator.validate(rec)


@pytest.mark.parametrize("field, bad_value", [
    ("category", "rent_control"),
    ("status", "enacted"),
    ("level", "county"),
    ("quoted_span", "too short"),
    ("effective_date", "July 1, 2027"),
    ("confidence", 1.5),
])
def test_schema_rejects_invalid_values(rule_validator, field, bad_value):
    rec = copy.deepcopy(sp.read_json(ROOT / sp.SAMPLE_RULE_PATH))
    rec[field] = bad_value
    assert not rule_validator.is_valid(rec)


@pytest.mark.parametrize("field", ["team_rule_id", "citation", "source_url", "quoted_span"])
def test_schema_requires_provenance_fields(rule_validator, field):
    rec = copy.deepcopy(sp.read_json(ROOT / sp.SAMPLE_RULE_PATH))
    del rec[field]
    assert not rule_validator.is_valid(rec)


def test_rule_records_accepts_both_wrappers():
    rec = {"team_rule_id": "r-1"}
    assert rule_records([rec]) == [rec]
    assert rule_records({"rules": [rec]}) == [rec]
    assert rule_records({"records": [rec]}) is None


# ------------------------------------------------------------------- addresses

def test_address_columns_and_unique_ids():
    header, rows = sp.read_csv(ROOT / sp.ADDRESSES_PATH)
    assert tuple(header) == sp.ADDRESS_COLUMNS
    ids = [r["address_id"] for r in rows]
    assert rows and all(ids) and len(ids) == len(set(ids))


def test_address_fields_stay_raw_strings():
    """ZIPs and use codes keep leading zeros; missing values stay empty, not guessed."""
    rows = {r["address_id"]: r for r in sp.read_csv(ROOT / sp.ADDRESSES_PATH)[1]}
    assert any(r["zip"].startswith("0") for r in rows.values())
    assert any(r["use_code"].startswith("0") for r in rows.values())
    assert any(r["year_built"] == "" for r in rows.values())
    assert any(r["units"] == "" for r in rows.values())


# ---------------------------------------------------------------------- corpus

def test_manifest_doc_ids_unique(manifest):
    ids = [r["doc_id"] for r in manifest]
    assert len(ids) == len(set(ids))


def test_source_classification_partitions_manifest(manifest):
    kinds = [sp.classify_source(r) for r in manifest]
    assert set(kinds) <= {sp.SUPPLIED_TEXT, sp.LINK_ONLY, sp.CAPTURE_FAILED}
    assert kinds.count(sp.SUPPLIED_TEXT) == sum(bool(r["text_file"]) for r in manifest)


def test_supplied_text_files_exist_with_matching_provenance(manifest):
    supplied = [r for r in manifest if sp.classify_source(r) == sp.SUPPLIED_TEXT]
    assert supplied
    for r in supplied:
        doc = sp.parse_corpus_text((ROOT / sp.CORPUS_DIR / r["text_file"]).read_text(encoding="utf-8"))
        assert doc.source_url == r["url"], r["doc_id"]
        assert sp.header_timestamp_to_manifest(doc.retrieved) == r["retrieved_at"], r["doc_id"]
        assert doc.body.strip(), r["doc_id"]


def test_every_text_file_is_referenced(manifest):
    referenced = {Path(r["text_file"]).name for r in manifest if r["text_file"]}
    on_disk = {p.name for p in (ROOT / sp.TEXT_DIR).iterdir()}
    assert on_disk == referenced


def test_links_only_matches_manifest_rows_without_text(manifest):
    links = sp.read_csv(ROOT / sp.LINKS_ONLY_PATH)[1]
    no_text = {r["doc_id"] for r in manifest if not r["text_file"]}
    assert {r["doc_id"] for r in links} == no_text


def test_capture_yes_does_not_imply_text(manifest):
    """Regression guard: 'capture' alone is not a reliable availability signal."""
    failed = [r for r in manifest if sp.classify_source(r) == sp.CAPTURE_FAILED]
    assert all(not r["text_file"] for r in failed)


def test_parse_corpus_text_rejects_missing_header():
    with pytest.raises(ValueError):
        sp.parse_corpus_text("no header here\n\nbody")
    doc = sp.parse_corpus_text("SOURCE: https://x\nRETRIEVED: 2026-10-01 22:44 UTC\n\nline 1\nline 2")
    assert (doc.source_url, doc.body) == ("https://x", "line 1\nline 2")
    assert sp.header_timestamp_to_manifest(doc.retrieved) == "2026-10-01T22:44Z"


# ------------------------------------------------------------- dev + templates

def test_change_tests_structure():
    tests = sp.read_json(ROOT / sp.CHANGE_TESTS_PATH)
    ids = [t["test_id"] for t in tests]
    assert len(ids) == len(set(ids))
    for t in tests:
        assert t["rule_ids"] and t["expected_behavior"]
        if t["type"] == "as_of":
            assert date.fromisoformat(t["as_of_before"]) < date.fromisoformat(t["as_of_after"])
        else:
            date.fromisoformat(t["as_of"])


def test_lookups_template_uses_official_vocabulary():
    obj = sp.read_json(ROOT / sp.TEMPLATES_DIR / "lookups.json")
    date.fromisoformat(obj["as_of"])
    for items in obj["lookups"].values():
        for item in items:
            assert item["result"] in sp.LOOKUP_RESULT_VALUES
            assert isinstance(item["conflict_flag"], bool)


def test_changes_template_keys_are_known_tests():
    tests = {t["test_id"] for t in sp.read_json(ROOT / sp.CHANGE_TESTS_PATH)}
    obj = sp.read_json(ROOT / sp.TEMPLATES_DIR / "changes.json")
    assert set(obj) <= tests
    assert all(isinstance(v["affected_address_ids"], list) for v in obj.values())


# ------------------------------------------- validator catches broken packs

def test_detects_missing_text_file(pack_copy):
    (pack_copy / "corpus/text/D001.txt").unlink()
    assert any("D001" in m and "does not exist" in m for m in _errors(pack_copy))


def test_detects_provenance_header_mismatch(pack_copy):
    p = pack_copy / "corpus/text/D001.txt"
    p.write_text(p.read_text(encoding="utf-8").replace("SOURCE: https://", "SOURCE: http://", 1), encoding="utf-8")
    assert any("D001" in m and "SOURCE" in m for m in _errors(pack_copy))


def test_detects_duplicate_address_id(pack_copy):
    path = pack_copy / sp.ADDRESSES_PATH
    header, rows = sp.read_csv(path)
    rows[1]["address_id"] = rows[0]["address_id"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    assert any("duplicate address_id" in m for m in _errors(pack_copy))


def test_detects_missing_address_column(pack_copy):
    path = pack_copy / sp.ADDRESSES_PATH
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("year_built", "yr_built", 1), encoding="utf-8")
    assert any("missing required columns" in m for m in _errors(pack_copy))


def test_detects_schema_invalid_template_rule(pack_copy):
    path = pack_copy / sp.TEMPLATES_DIR / "rules.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"security_deposits"', '"deposits"'), encoding="utf-8")
    assert any("r-0007" in m for m in _errors(pack_copy))


def test_detects_invalid_lookup_result(pack_copy):
    path = pack_copy / sp.TEMPLATES_DIR / "lookups.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"applies"', '"not_applicable"'), encoding="utf-8")
    assert any("not_applicable" in m for m in _errors(pack_copy))


def test_detects_links_only_row_with_supplied_text(pack_copy):
    path = pack_copy / sp.LINKS_ONLY_PATH
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write('D001,"Berkeley, CA",https://example.org,official\n')
    assert any("D001" in m and "link-only" in m for m in _errors(pack_copy))
