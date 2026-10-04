"""Shared fixtures. Every test runs with outbound network blocked and no API key set."""

from __future__ import annotations

import json
import socket
import sys
from typing import Any

import pytest

from navigator.extraction.cache import ResponseCache, sha256_hex
from navigator.extraction.extractor import SourceDocument, extract_document, load_source
from navigator.extraction.models import SourceMeta
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


# Verbatim heading of corpus/text/D052.txt: evidence that D052 is codified current law.
D052_POSTURE = {"posture": "codified_current_law",
                "evidence": "General Law - Part II, Title I, Chapter 186, Section 15B"}


def codified(source) -> dict[str, Any]:
    """A codified_current_law posture evidenced by the first substantive line of `source`."""
    line = next(x for x in source.view.without_markers().splitlines() if len(x.strip()) > 20)
    return {"posture": "codified_current_law", "evidence": line.strip()}


class FakeProvider:
    """Deterministic stand-in for a StructuredLLMProvider. Returns `responses` in order
    (the last one repeats) and records every call. A primary response (a dict with
    `rules`) that has no `document` key is given `document` (default: D052's codified
    posture), so tests that are not about posture need not repeat it; posture tests set
    `document` in the response itself."""

    name = "fake"

    def __init__(self, *responses: Any, model: str = "fake-model", document: Any = D052_POSTURE) -> None:
        self.model = model
        self.responses = list(responses)
        self.document = document
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system_instruction, prompt, response_json_schema, settings) -> ProviderResult:
        self.calls.append({"system_instruction": system_instruction, "prompt": prompt,
                           "response_json_schema": response_json_schema, "settings": settings})
        response = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(response, dict) and "rules" in response and "target_resolutions" not in response:
            response = {"document": self.document, **response}
        text = response if isinstance(response, str) else json.dumps(response)
        return ProviderResult(text=text, metadata={"fake": True, "call": len(self.calls)})


# Verbatim passages from corpus/text/D052.txt (M.G.L. c. 186, s. 15B).
D052_DEPOSIT_SPAN = ("(iii) a security deposit equal to the first month's rent provided that such security "
                     "deposit is deposited as required by subsection (3)")
D052_DATE_EVIDENCE = "as amended by 2025, 9, Secs. 54 and 55 effective August 1, 2025"
D052_DOUBLE_SPACED = "before the termination date of such lease.  A lessor may, however, enter such premises:"


def parts(*texts: str, first_segment: int = 1) -> list[dict[str, Any]]:
    """Quote parts from consecutive segments, starting at `first_segment`."""
    return [{"segment_id": first_segment + i, "quoted_text": t} for i, t in enumerate(texts)]


def make_candidate(**overrides: Any) -> dict[str, Any]:
    """A prompt-v6 candidate linked to inventory id P1. `quoted_span=` is shorthand for one
    quote part in segment 1."""
    rule = {
        "category": "security_deposits",
        "citation": "M.G.L. c. 186, § 15B(1)(b)(iii)",
        "provision_ids": ["P1"],
        "source_basis": "operative_text",
        "quote_parts": parts(D052_DEPOSIT_SPAN),
        "title": "Security deposit limit",
        "requirement": "A lessor may not require a security deposit greater than the first month's rent.",
        "key_value": "1 month's rent",
        "coverage_conditions": "Residential tenancies",
        "exemptions": None,
        "scope_carve_outs": [],
        "operative_conditions": [],
        "interaction": None,
        "enactment_status": "enacted",
        "enactment_status_evidence": None,
        "version_note": None,
        "version_evidence": None,
        "effective_date_evidence": None,
        "effective_date_evidence_kind": None,
        "effective_date": None,
        "conflict_note": None,
    }
    if "quoted_span" in overrides:
        overrides["quote_parts"] = parts(overrides.pop("quoted_span"))
    rule.update(overrides)
    return rule


def provision(ref: str, scope: str = "in_scope", category: str | None = "security_deposits", *, id: str = "P1",
              anchor: str | None = None, role: str = "operative_rule", summary: str = "a provision",
              reason: str | None = None) -> dict[str, Any]:
    """A prompt-v6 inventory item. Records link to it through `provision_ids` = [id]. Without
    `anchor` the item's anchor is its ref (usually not source text: no region, no challenge)."""
    return {"id": id, "ref": ref, "anchor": anchor or ref, "summary": summary, "role": role, "scope": scope,
            "category": category if scope == "in_scope" else None,
            "reason": None if scope == "in_scope" else (reason or "not a requirement in an official category")}


def no_rules(reason: str = "The document has no in-scope legal content.",
             evidence: str = "General Law - Part II, Title I, Chapter 186, Section 15B") -> dict[str, Any]:
    """A no_rules_justification (default evidence: D052's heading)."""
    return {"reason": reason, "evidence": evidence}


@pytest.fixture(scope="session")
def d052():
    return load_source("D052")


@pytest.fixture
def cache(tmp_path):
    return ResponseCache(tmp_path / "cache")


@pytest.fixture
def run_fake(d052, cache):
    """Run the real pipeline on D052 with a FakeProvider returning `response`."""

    def _run(response: Any, *more: Any, **kwargs: Any):
        provider = kwargs.pop("provider", None) or FakeProvider(response, *more)
        run = extract_document(d052, provider_name=provider.name, model=provider.model, provider=provider,
                               cache=kwargs.pop("cache", cache), **kwargs)
        return run, provider

    return _run


# ------------------------------------------- synthetic paged documents (no corpus data)

WORDS = ["notice", "deposit", "tenant", "landlord", "payment", "premises", "lease", "inspection", "repair",
         "record", "receipt", "account", "transfer", "remedy", "waiver", "occupancy"]


def page_body(n: int, lines: int = 10) -> list[str]:
    """Distinct substantive-looking lines (words, not digits, vary)."""
    return [f"The {WORDS[(n * 3 + i) % 16]} rule requires the {WORDS[(n + 2 * i) % 16]} to follow the "
            f"{WORDS[(n * 5 + 3 * i) % 16]} procedure in {WORDS[i % 16]} matters." for i in range(lines)]


def paged(pages: list[list[str]], header) -> str:
    out: list[str] = []
    for n, body in enumerate(pages, start=1):
        out += header(n) + [""] + body + [""]
    return "\n".join(out) + "\n"


def running_header(n: int) -> list[str]:
    return ["Ch. Art. Div.", f"9 8 7 {n}", "Example City Municipal Code Chapter 9: Housing", "(3-2024)"]


def synthetic_source(body: str) -> SourceDocument:
    meta = SourceMeta(doc_id="DTEST", jurisdiction="Example City, CA", url="https://example.org/code",
                      source_type="official", retrieved_at="2026-10-01T00:00Z", text_file="text/DTEST.txt",
                      content_sha256=sha256_hex(body), body_chars=len(body))
    return SourceDocument(meta=meta, body=body)
