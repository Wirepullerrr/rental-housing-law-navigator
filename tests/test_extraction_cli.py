"""CLI safety gates, network isolation, and the Gemini adapter exercised offline."""

from __future__ import annotations

import email.utils
import importlib.util
import json
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import FakeProvider, NetworkBlocked, make_candidate
from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache
from navigator.extraction.config import DEFAULT_GEMINI_MODEL, PROVIDER_GEMINI
from navigator.extraction.extractor import extract_document
from navigator.extraction.provider import MissingCredentialsError, ProviderError

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
    provider = FakeProvider({"rules": [make_candidate()]}, model=DEFAULT_GEMINI_MODEL)
    provider.name = PROVIDER_GEMINI  # same cache identity the CLI computes
    extract_document(d052, provider_name=PROVIDER_GEMINI, model=DEFAULT_GEMINI_MODEL, provider=provider,
                     cache=ResponseCache(tmp / "cache"))
    assert cli.main(["--doc-id", "D052", *args]) == 0
    artifact = json.loads((tmp / "out" / "artifact.json").read_text(encoding="utf-8"))
    assert artifact["cache_hit"] is True and artifact["accepted_count"] == 1
    assert "HIT" in capsys.readouterr().out


# ------------------------------------------------------------ network isolation

def test_network_guard_blocks_outbound_connections():
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("generativelanguage.googleapis.com", 443)


def test_pipeline_does_not_import_the_vendor_sdk():
    code = ("import sys; sys.path.insert(0, 'src'); import navigator.extraction.extractor; "
            "print('google.genai' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=sp.REPO_ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# --------------------------------------------------------- Gemini adapter offline

gemini = pytest.importorskip("navigator.extraction.gemini")
from google.genai import errors, types  # noqa: E402


def _response(text: str, finish=types.FinishReason.STOP) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]),
                                    finish_reason=finish)],
        model_version="test-model-001",
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=5,
                                                                  total_token_count=15),
    )


@pytest.fixture
def sleeps(monkeypatch):
    """Record requested backoff sleeps instead of sleeping."""
    recorded: list[float] = []
    monkeypatch.setattr(gemini, "SLEEP", recorded.append)
    return recorded


@pytest.fixture
def provider(monkeypatch, sleeps):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-offline-test-key")
    return gemini.GeminiProvider(model="test-model")


def _api_error(code: int, status: str, details: list | None = None, headers: dict | None = None):
    body = {"error": {"code": code, "message": f"{status} test", "status": status, "details": details or []}}
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, body, httpx.Response(code, headers=headers or {}, json=body))


def _patch_call(monkeypatch, provider, outcome):
    calls = []

    def fake_generate_content(*, model, contents, config):
        calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(provider._client.models, "generate_content", fake_generate_content)
    return calls


def test_gemini_provider_requires_key():
    with pytest.raises(MissingCredentialsError, match="GEMINI_API_KEY"):
        gemini.GeminiProvider()


def test_gemini_adapter_sends_structured_request(provider, monkeypatch):
    calls = _patch_call(monkeypatch, provider, _response('{"rules": []}'))
    schema = {"type": "object", "properties": {"rules": {"type": "array"}}, "required": ["rules"]}
    result = provider.generate(system_instruction="sys", prompt="doc", response_json_schema=schema,
                               settings={"temperature": None, "seed": 7})
    assert result.text == '{"rules": []}'
    assert result.metadata["finish_reason"] == "STOP" and result.metadata["usage"]["total_tokens"] == 15
    config = calls[0]["config"]
    assert (calls[0]["model"], config.response_mime_type, config.response_json_schema, config.seed) == \
           ("test-model", "application/json", schema, 7)
    assert config.automatic_function_calling.disable is True
    assert "dummy-offline-test-key" not in json.dumps(result.metadata)


def _generate(provider):
    return provider.generate(system_instruction="s", prompt="p", response_json_schema={}, settings={})


@pytest.mark.parametrize("code, status", [(429, "RESOURCE_EXHAUSTED"), (500, "INTERNAL"), (502, "BAD_GATEWAY"),
                                          (503, "UNAVAILABLE"), (504, "DEADLINE_EXCEEDED")])
def test_transient_errors_get_three_attempts_with_30s_then_60s_backoff(provider, monkeypatch, sleeps, code, status):
    calls = _patch_call(monkeypatch, provider, _api_error(code, status))
    with pytest.raises(ProviderError, match=str(code)):
        _generate(provider)
    assert len(calls) == gemini.MAX_ATTEMPTS == 3
    assert sleeps == [30.0, 60.0]


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_client_errors_are_never_retried(provider, monkeypatch, sleeps, code):
    calls = _patch_call(monkeypatch, provider, _api_error(code, "INVALID_ARGUMENT"))
    with pytest.raises(ProviderError, match=str(code)):
        _generate(provider)
    assert (len(calls), sleeps) == (1, [])


def test_retry_after_header_is_honored(provider, monkeypatch, sleeps):
    _patch_call(monkeypatch, provider, _api_error(503, "UNAVAILABLE", headers={"Retry-After": "7"}))
    with pytest.raises(ProviderError, match="retry after 7s"):
        _generate(provider)
    assert sleeps == [7.0, 7.0]


def test_retry_info_delay_is_honored(provider, monkeypatch, sleeps):
    info = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]
    calls = _patch_call(monkeypatch, provider, _api_error(429, "RESOURCE_EXHAUSTED", details=info))
    with pytest.raises(ProviderError):
        _generate(provider)
    assert (len(calls), sleeps) == (3, [37.0, 37.0])


def test_retry_after_beyond_bound_is_not_waited(provider, monkeypatch, sleeps):
    calls = _patch_call(monkeypatch, provider, _api_error(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": "3600"}))
    with pytest.raises(ProviderError, match="3600s"):
        _generate(provider)
    assert (len(calls), sleeps) == (1, [])


def test_network_errors_are_retried(provider, monkeypatch, sleeps):
    calls = _patch_call(monkeypatch, provider, httpx.ConnectError("connection refused"))
    with pytest.raises(ProviderError, match="network error"):
        _generate(provider)
    assert (len(calls), sleeps) == (3, [30.0, 60.0])


def test_retry_after_http_date_is_parsed():
    future = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    hint = gemini.retry_after_seconds(_api_error(503, "UNAVAILABLE", headers={"Retry-After": future}))
    assert 80 <= hint <= 90


def test_gemini_rejects_truncated_output(provider, monkeypatch):
    _patch_call(monkeypatch, provider, _response('{"rules": [', finish=types.FinishReason.MAX_TOKENS))
    with pytest.raises(ProviderError, match="MAX_TOKENS"):
        _generate(provider)
