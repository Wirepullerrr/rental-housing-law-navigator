"""CLI safety gates and network isolation. The Gemini adapter is tested in test_gemini_interactions.py."""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys

import pytest

from conftest import NETWORK_ATTEMPTS, FakeProvider, NetworkBlocked, make_candidate, provision
from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache
from navigator.extraction.config import DEFAULT_GEMINI_MODEL, PROVIDER_GEMINI
from navigator.extraction.extractor import extract_document

_spec = importlib.util.spec_from_file_location("extract_rules", sp.REPO_ROOT / "scripts" / "extract_rules.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture
def paths(tmp_path):
    return ["--cache-dir", str(tmp_path / "cache"), "--out", str(tmp_path / "out" / "artifact.json")], tmp_path


def test_live_mode_without_api_key_fails_clearly(paths, capsys):
    args, tmp = paths
    assert cli.main(["--doc-id", "D052", "--live", *args]) == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().err
    assert not (tmp / "out").exists()


def test_offline_mode_never_asks_for_a_key(paths, capsys):
    args, _ = paths
    assert cli.main(["--doc-id", "D052", *args]) == 2
    err = capsys.readouterr().err
    assert "--live" in err and "GEMINI_API_KEY" not in err


def test_force_requires_live(paths, capsys):
    args, _ = paths
    assert cli.main(["--doc-id", "D052", "--force", *args]) == 2
    assert "requires --live" in capsys.readouterr().err


@pytest.mark.parametrize("doc_id, message", [("D002", "no supplied local text"), ("all", "not a doc_id"),
                                             ("D052,D024", "not a doc_id")])
def test_only_one_supplied_document_is_accepted(paths, capsys, doc_id, message):
    args, _ = paths
    assert cli.main(["--doc-id", doc_id, *args]) == 2
    assert message in capsys.readouterr().err


def test_doc_id_is_required(capsys):
    with pytest.raises(SystemExit):
        cli.main([])


def test_offline_rerun_from_cache_writes_artifact(paths, d052, capsys):
    args, tmp = paths
    response = {"provisions": [provision("§ 15B(1)(b)")], "global_scope": [], "rules": [make_candidate()],
                "no_rules_justification": None}
    provider = FakeProvider(response, model=DEFAULT_GEMINI_MODEL)
    provider.name = PROVIDER_GEMINI  # same cache identity the CLI computes
    extract_document(d052, provider_name=PROVIDER_GEMINI, model=DEFAULT_GEMINI_MODEL, provider=provider,
                     cache=ResponseCache(tmp / "cache"))
    assert cli.main(["--doc-id", "D052", *args]) == 0
    artifact = json.loads((tmp / "out" / "artifact.json").read_text(encoding="utf-8"))
    assert artifact["cache_hit"] is True and artifact["accepted_count"] == 1
    assert (artifact["document_status"], artifact["repair"]) == ("complete", None)
    assert artifact["generation_settings"]["thinking_level"] == "medium"
    out = capsys.readouterr().out
    assert "HIT" in out and "COMPLETE" in out and "repair     : not needed" in out


# ------------------------------------------------------------ network isolation

def test_network_guard_blocks_and_records_outbound_attempts():
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("generativelanguage.googleapis.com", 443)
    assert len(NETWORK_ATTEMPTS) == 2 and "generativelanguage" in NETWORK_ATTEMPTS[1]
    NETWORK_ATTEMPTS.clear()  # deliberate attempts; any other test recording one fails at teardown


def test_pipeline_does_not_import_the_vendor_sdk():
    code = ("import sys; sys.path.insert(0, 'src'); import navigator.extraction.extractor; "
            "print('google.genai' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=sp.REPO_ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
