"""Gemini adapter (Interactions API) tested offline at the HTTP level.

The real google-genai SDK runs against an httpx.MockTransport: request
serialization, error mapping, the SDK's (disabled) internal retry and our
bounded retry policy are all exercised without any socket being opened.
"""

from __future__ import annotations

import email.utils
import gc
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import D052_POSTURE, make_candidate
from navigator.extraction import gemini
from navigator.extraction.cache import ResponseCache
from navigator.extraction.extractor import extract_document
from navigator.extraction.provider import MissingCredentialsError, ProviderError, StructuredLLMProvider

KEY = "dummy-offline-test-key"
MODEL = "gemini-3.8-flash"
SCHEMA = {"type": "object", "properties": {"rules": {"type": "array"}}, "required": ["rules"]}
ALLOWED_REQUEST_FIELDS = {"model", "input", "system_instruction", "response_format", "store", "stream",
                          "generation_config"}


def ok(text: str = '{"rules": []}', **overrides) -> httpx.Response:
    body = {"id": "int-test-1", "status": "completed", "model": MODEL,
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
            "usage": {"total_input_tokens": 100, "total_output_tokens": 20, "total_thought_tokens": 5,
                      "total_tokens": 125}}
    body.update(overrides)
    return httpx.Response(200, json=body)


def api_error(code: int, status: str = "ERR", headers: dict | None = None, details: list | None = None):
    body = {"error": {"code": code, "message": f"{status} test", "status": status, "details": details or []}}
    return httpx.Response(code, headers=headers or {}, json=body)


def raises(exc_type):
    def outcome(request):
        raise exc_type("simulated", request=request)
    return outcome


class MockAPI:
    """Scripted outcomes for successive HTTP requests (the last one repeats); records requests."""

    def __init__(self) -> None:
        self.script: list = []
        self.requests: list[httpx.Request] = []
        self.provider: gemini.GeminiProvider | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return outcome(request) if callable(outcome) else outcome

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(gemini, "SLEEP", recorded.append)
    return recorded


@pytest.fixture
def api(monkeypatch, sleeps) -> MockAPI:
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    mock = MockAPI()
    mock.provider = gemini.GeminiProvider(model=MODEL, http_client=httpx.Client(transport=httpx.MockTransport(mock.handler)))
    return mock


def generate(api: MockAPI, settings: dict | None = None):
    return api.provider.generate(system_instruction="SYS", prompt="DOC", response_json_schema=SCHEMA,
                                 settings=settings or {"temperature": None, "seed": 7})


# ------------------------------------------------------------------ request

def test_requires_key():
    with pytest.raises(MissingCredentialsError, match="GEMINI_API_KEY"):
        gemini.GeminiProvider()


def test_satisfies_provider_protocol(api):
    assert isinstance(api.provider, StructuredLLMProvider)


def test_sends_one_structured_interactions_request(api):
    api.script = [ok()]
    result = generate(api)
    assert len(api.requests) == 1
    req, body = api.requests[0], api.body()
    assert req.method == "POST" and req.url.path.endswith("/interactions")
    assert set(body) <= ALLOWED_REQUEST_FIELDS                    # no tools, agents, search
    assert (body["model"], body["input"], body["system_instruction"], body["store"]) == (MODEL, "DOC", "SYS", False)
    assert body["response_format"] == {"type": "text", "mime_type": "application/json", "schema": SCHEMA}
    assert body["generation_config"] == {"seed": 7}               # temperature None is not sent
    assert req.headers["x-goog-api-key"] == KEY and KEY not in str(req.url)
    assert result.text == '{"rules": []}'
    assert result.metadata == {"api": "interactions.create", "interaction_id": "int-test-1", "model": MODEL,
                               "status": "completed",
                               "usage": {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 5,
                                         "total_tokens": 125}}
    assert KEY not in json.dumps(result.metadata)


def test_thinking_level_is_sent_in_generation_config(api):
    api.script = [ok()]
    generate(api, settings={"temperature": None, "seed": 7, "thinking_level": "low"})
    assert api.body()["generation_config"] == {"seed": 7, "thinking_level": "low"}


def test_sdk_owned_http_client_survives_garbage_collection(monkeypatch):
    """Regression (live run 2026-10-03): with an SDK-owned httpx client, a provider that
    does not keep its genai.Client alive fails every request with "client has been closed".
    Uses the production construction path; the transport is swapped only after GC."""
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    provider = gemini.GeminiProvider(model=MODEL)
    gc.collect()
    owned = provider._client._api_client._httpx_client
    assert not owned.is_closed
    mock = MockAPI()
    mock.provider, mock.script = provider, [ok()]
    monkeypatch.setattr(owned, "_transport", httpx.MockTransport(mock.handler))
    assert generate(mock).text == '{"rules": []}' and len(mock.requests) == 1


def test_sdk_internal_retry_is_disabled(api, monkeypatch):
    assert api.provider._interactions.sdk_configuration.retry_config.strategy == "none"
    monkeypatch.setattr(gemini, "MAX_ATTEMPTS", 1)
    api.script = [api_error(503, "UNAVAILABLE")]
    with pytest.raises(ProviderError):
        generate(api)
    assert len(api.requests) == 1


# ------------------------------------------------------------------ retries

@pytest.mark.parametrize("code, status", [(429, "RESOURCE_EXHAUSTED"), (500, "INTERNAL"), (502, "BAD_GATEWAY"),
                                          (503, "UNAVAILABLE"), (504, "DEADLINE_EXCEEDED")])
def test_transient_errors_get_three_http_attempts_30s_then_60s(api, sleeps, code, status):
    api.script = [api_error(code, status)]
    with pytest.raises(ProviderError, match=f"{code} {status}"):
        generate(api)
    assert len(api.requests) == 3 and sleeps == [30.0, 60.0]


@pytest.mark.parametrize("code, status", [(400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"),
                                          (403, "PERMISSION_DENIED"), (404, "NOT_FOUND")])
def test_client_errors_are_never_retried(api, sleeps, code, status):
    api.script = [api_error(code, status)]
    with pytest.raises(ProviderError, match=f"{code} {status}"):
        generate(api)
    assert (len(api.requests), sleeps) == (1, [])


def test_recovers_after_one_transient_failure(api, sleeps):
    api.script = [api_error(503, "UNAVAILABLE"), ok()]
    assert generate(api).text == '{"rules": []}'
    assert (len(api.requests), sleeps) == (2, [30.0])


def test_retry_after_header_is_honored(api, sleeps):
    api.script = [api_error(503, "UNAVAILABLE", headers={"Retry-After": "7"})]
    with pytest.raises(ProviderError, match="retry after 7s"):
        generate(api)
    assert sleeps == [7.0, 7.0]


def test_retry_info_delay_is_honored(api, sleeps):
    info = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]
    api.script = [api_error(429, "RESOURCE_EXHAUSTED", details=info)]
    with pytest.raises(ProviderError):
        generate(api)
    assert (len(api.requests), sleeps) == (3, [37.0, 37.0])


def test_retry_after_beyond_bound_is_not_waited(api, sleeps):
    api.script = [api_error(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": "3600"})]
    with pytest.raises(ProviderError, match="3600s"):
        generate(api)
    assert (len(api.requests), sleeps) == (1, [])


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_errors_are_retried(api, sleeps, exc_type):
    api.script = [raises(exc_type)]
    with pytest.raises(ProviderError, match="network error"):
        generate(api)
    assert (len(api.requests), sleeps) == (3, [30.0, 60.0])


def test_retry_after_http_date_is_parsed():
    future = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    exc = type("E", (Exception,), {})()
    exc.response = api_error(503, headers={"Retry-After": future})
    assert 80 <= gemini.retry_after_seconds(exc) <= 90


# ----------------------------------------------------------------- response

@pytest.mark.parametrize("status", ["failed", "incomplete", "budget_exceeded", "cancelled"])
def test_unfinished_interaction_is_rejected_without_retry(api, sleeps, status):
    api.script = [ok(status=status, errors=[{"code": "X1", "message": "stopped"}])]
    with pytest.raises(ProviderError, match=f"status={status}"):
        generate(api)
    assert (len(api.requests), sleeps) == (1, [])


def test_completed_without_text_is_rejected(api):
    api.script = [ok(steps=[])]
    with pytest.raises(ProviderError, match="without text"):
        generate(api)


# ------------------------------------------------- end to end, still offline

def test_pipeline_runs_unchanged_through_interactions_adapter(api, d052, tmp_path):
    api.script = [ok(text=json.dumps({"document": D052_POSTURE, "rules": [make_candidate()]}))]
    cache = ResponseCache(tmp_path / "cache")
    run = extract_document(d052, provider_name="gemini", model=MODEL, provider=api.provider, cache=cache)
    assert (run.candidate_count, run.accepted_count, run.errors) == (1, 1, [])
    assert run.candidates[0].citation.status == "exact_match" and run.candidates[0].schema_valid
    assert run.provider_metadata["api"] == "interactions.create"
    body = api.body()
    assert "doc_id: D052" in body["input"] and body["system_instruction"].startswith("You are the rule-extraction")
    assert body["response_format"]["schema"]["properties"]["rules"]["items"]["properties"]["category"]["enum"]
    again = extract_document(d052, provider_name="gemini", model=MODEL, provider=api.provider, cache=cache)
    assert again.cache_hit and len(api.requests) == 1
