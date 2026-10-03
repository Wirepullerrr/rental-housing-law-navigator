"""Provider-neutral interface for structured LLM generation.

The extraction pipeline depends only on this module; vendor SDKs are imported
solely by their adapter (e.g. gemini.py), which only the live CLI path loads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ProviderResult:
    text: str  # the raw JSON text returned by the model
    metadata: dict[str, Any] = field(default_factory=dict)  # model version, finish reason, token usage


class ProviderError(RuntimeError):
    """The provider could not return a usable response (after any transient retries)."""


class MissingCredentialsError(ProviderError):
    """Live mode was requested but the API key environment variable is not set."""


@runtime_checkable
class StructuredLLMProvider(Protocol):
    name: str
    model: str

    def generate(self, *, system_instruction: str, prompt: str,
                 response_json_schema: dict[str, Any], settings: dict[str, Any]) -> ProviderResult:
        """Return JSON text constrained to `response_json_schema`."""
        ...
