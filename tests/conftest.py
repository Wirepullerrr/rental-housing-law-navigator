"""Shared fixtures. Every test runs with outbound network blocked and no API key set."""

from __future__ import annotations

import json
import socket
import sys
from typing import Any

import pytest

from navigator.extraction.cache import ResponseCache
from navigator.extraction.extractor import extract_document, load_source
from navigator.extraction.provider import ProviderResult

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# Every non-loopback network attempt in this process, whether blocked by the
# patched socket functions below or seen by the audit hook (which also covers
# C-level socket calls that bypass the patches). A test that records one fails,
# even if the resulting exception was caught and wrapped by library code.
NETWORK_ATTEMPTS: list[str] = []


def _audit(event: str, args: tuple) -> None:
    if event == "socket.connect":
        address = args[1]
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            NETWORK_ATTEMPTS.append(f"connect {address!r}")
    elif event == "socket.getaddrinfo" and args[0] not in _LOOPBACK and args[0] is not None:
        NETWORK_ATTEMPTS.append(f"getaddrinfo {args[0]!r}")


sys.addaudithook(_audit)


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            NETWORK_ATTEMPTS.append(f"connect {address!r}")
            raise NetworkBlocked(f"network access attempted during tests: {address!r}")
        return real_connect(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in _LOOPBACK:
            NETWORK_ATTEMPTS.append(f"getaddrinfo {host!r}")
            raise NetworkBlocked(f"DNS lookup attempted during tests: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    NETWORK_ATTEMPTS.clear()
    yield
    attempts = list(NETWORK_ATTEMPTS)
    NETWORK_ATTEMPTS.clear()
    assert not attempts, f"test attempted network access: {attempts}"


class FakeProvider:
    """Deterministic stand-in for a StructuredLLMProvider. Counts calls."""

    name = "fake"

    def __init__(self, response: Any, model: str = "fake-model") -> None:
        self.model = model
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system_instruction, prompt, response_json_schema, settings) -> ProviderResult:
        self.calls.append({"system_instruction": system_instruction, "prompt": prompt,
                           "response_json_schema": response_json_schema, "settings": settings})
        text = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return ProviderResult(text=text, metadata={"fake": True})


# Verbatim passages from corpus/text/D052.txt (M.G.L. c. 186, s. 15B).
D052_DEPOSIT_SPAN = ("(iii) a security deposit equal to the first month's rent provided that such security "
                     "deposit is deposited as required by subsection (3)")
D052_DATE_EVIDENCE = "as amended by 2025, 9, Secs. 54 and 55 effective August 1, 2025"
D052_DOUBLE_SPACED = "before the termination date of such lease.  A lessor may, however, enter such premises:"


def make_candidate(**overrides: Any) -> dict[str, Any]:
    rule = {
        "category": "security_deposits",
        "title": "Security deposit limit",
        "requirement": "A lessor may not require a security deposit greater than the first month's rent.",
        "key_value": "1 month's rent",
        "coverage_conditions": "Residential tenancies",
        "exemptions": None,
        "scope_carve_outs": [],
        "operative_conditions": [],
        "interaction": None,
        "enactment_status": "enacted",
        "effective_date": None,
        "effective_date_evidence": None,
        "citation": "M.G.L. c. 186, § 15B(1)(b)(iii)",
        "quoted_span": D052_DEPOSIT_SPAN,
        "conflict_note": None,
        "version_note": None,
        "version_evidence": None,
    }
    rule.update(overrides)
    return rule


@pytest.fixture(scope="session")
def d052():
    return load_source("D052")


@pytest.fixture
def cache(tmp_path):
    return ResponseCache(tmp_path / "cache")


@pytest.fixture
def run_fake(d052, cache):
    """Run the real pipeline on D052 with a FakeProvider returning `response`."""

    def _run(response: Any, **kwargs: Any):
        provider = kwargs.pop("provider", None) or FakeProvider(response)
        run = extract_document(d052, provider_name=provider.name, model=provider.model, provider=provider,
                               cache=kwargs.pop("cache", cache), **kwargs)
        return run, provider

    return _run
