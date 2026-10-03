"""Gemini adapter for StructuredLLMProvider, via the Interactions API
(`client.interactions.create`) of the google-genai SDK.

The only module that imports google.genai. Loaded only for live runs.
The API key is read from GEMINI_API_KEY and is never stored, logged or returned.
One plain model interaction per attempt: no agents, tools, search or function calling.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from google import genai
from google.genai import types
# google-genai 2.28.0 exports neither the Interactions error classes nor its retry
# config publicly. These private imports are confined to this module, pinned by
# uv.lock, and exercised at the HTTP level in tests, so SDK drift fails loudly.
from google.genai._gaos import utils as _gaos_utils
from google.genai._gaos.lib.compat_errors import APIConnectionError, APIStatusError
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

from navigator.extraction.config import API_KEY_ENV, DEFAULT_GEMINI_MODEL, PROVIDER_GEMINI
from navigator.extraction.provider import MissingCredentialsError, ProviderError, ProviderResult

log = logging.getLogger(__name__)

API_SURFACE = "interactions.create"

# Retry policy: transient failures only (429, any 5xx, network timeouts and connection
# errors). 4xx such as 400/401/403/404, incomplete interactions and every
# extraction-quality problem are never retried. The SDK's own retry is disabled in
# __init__, so this is the only retry layer.
MAX_ATTEMPTS = 3                 # total HTTP attempts, including the first
BACKOFF_SECONDS = (30.0, 60.0)   # wait after the 1st and 2nd transient failure
MAX_RETRY_AFTER_SECONDS = 120.0  # a server-requested delay above this is not waited out
REQUEST_TIMEOUT_MS = 180_000
SLEEP = time.sleep               # injectable for tests
_TRANSIENT_NETWORK_ERRORS = (APIConnectionError, httpx.TimeoutException, httpx.NetworkError,
                             httpx.RemoteProtocolError)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or 500 <= exc.status_code <= 599
    return isinstance(exc, _TRANSIENT_NETWORK_ERRORS)


def retry_after_seconds(exc: BaseException) -> float | None:
    """Server-requested delay: the HTTP Retry-After header (seconds or HTTP-date),
    else google.rpc.RetryInfo.retryDelay in the error body (e.g. "37s")."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    value = headers.get("retry-after") if headers is not None else None
    if value:
        value = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", value):
            return float(value)
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    for item in (error.get("details") or []) if isinstance(error, dict) else []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("google.rpc.RetryInfo"):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(item.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def should_retry(exc: BaseException) -> bool:
    hint = retry_after_seconds(exc)
    return is_transient(exc) and (hint is None or hint <= MAX_RETRY_AFTER_SECONDS)


def wait_seconds(retry_state: RetryCallState) -> float:
    """Honor a server-requested delay if given, else 30s then 60s."""
    hint = retry_after_seconds(retry_state.outcome.exception())
    if hint is not None:
        return hint
    return BACKOFF_SECONDS[min(retry_state.attempt_number, len(BACKOFF_SECONDS)) - 1]


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, APIStatusError):
        error = exc.body.get("error") if isinstance(exc.body, dict) else None
        if isinstance(error, dict):
            head = " ".join(str(p) for p in (exc.status_code, error.get("status")) if p)
            return f"{head}: {error.get('message', '')}"
        return f"{exc.status_code}: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _log_retry(rs: RetryCallState) -> None:
    exc = rs.outcome.exception()
    source = "Retry-After" if retry_after_seconds(exc) is not None else "backoff"
    log.warning("attempt %d/%d failed (transient): %s -- waiting %.0fs (%s)",
                rs.attempt_number, MAX_ATTEMPTS, describe_error(exc), rs.next_action.sleep, source)


class GeminiProvider:
    name = PROVIDER_GEMINI

    def __init__(self, model: str = DEFAULT_GEMINI_MODEL, *, http_client: httpx.Client | None = None) -> None:
        """`http_client` exists for offline tests (an httpx.MockTransport); leave it None."""
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise MissingCredentialsError(
                f"{API_KEY_ENV} is not set. Live extraction needs it: set the environment variable, "
                f"or run via `uv run --env-file .env ...` with a local, untracked .env file.")
        self.model = model
        extra = {"httpx_client": http_client} if http_client is not None else {}
        options = types.HttpOptions(timeout=REQUEST_TIMEOUT_MS, **extra)
        # Keep the Client referenced: its __del__ closes the SDK-owned httpx client,
        # after which every request fails with "client has been closed".
        self._client = genai.Client(api_key=api_key, http_options=options)
        self._interactions = self._client.interactions
        # The Interactions client retries by default (up to 3 HTTP attempts), and
        # HttpRetryOptions cannot turn that off (the SDK rewrites attempts=0 to 1).
        # Disable it so the bounded policy in generate() is the only retry layer.
        self._interactions.sdk_configuration.retry_config = _gaos_utils.RetryConfig("none", None, False)

    @staticmethod
    def build_request(model: str, system_instruction: str, prompt: str,
                      response_json_schema: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        """Keyword arguments for interactions.create: one structured-output model call."""
        generation_config = {k: settings[k] for k in ("temperature", "seed", "thinking_level")
                             if settings.get(k) is not None}
        request: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "system_instruction": system_instruction,
            "response_format": {"type": "text", "mime_type": "application/json", "schema": response_json_schema},
            "store": False,   # no server-side retention of the interaction
            "stream": False,
        }
        if generation_config:
            request["generation_config"] = generation_config
        return request

    def generate(self, *, system_instruction: str, prompt: str,
                 response_json_schema: dict[str, Any], settings: dict[str, Any]) -> ProviderResult:
        request = self.build_request(self.model, system_instruction, prompt, response_json_schema, settings)
        retrying = Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=wait_seconds,
            retry=retry_if_exception(should_retry),
            sleep=SLEEP,
            reraise=True,
            before_sleep=_log_retry,
        )
        try:
            interaction = retrying(self._interactions.create, **request)
        except APIStatusError as exc:
            hint = retry_after_seconds(exc)
            suffix = f" (server asked to retry after {hint:.0f}s)" if hint is not None else ""
            raise ProviderError(f"Gemini API error {describe_error(exc)}{suffix}") from exc
        except (APIConnectionError, httpx.HTTPError) as exc:
            raise ProviderError(f"network error calling Gemini: {describe_error(exc)}") from exc
        return self._to_result(interaction)

    @staticmethod
    def _to_result(interaction: Any) -> ProviderResult:
        usage = interaction.usage
        status = str(interaction.status) if interaction.status is not None else None
        metadata = {
            "api": API_SURFACE,
            "interaction_id": interaction.id,
            "model": str(interaction.model) if interaction.model is not None else None,
            "status": status,
            "usage": {
                "input_tokens": usage.total_input_tokens,
                "output_tokens": usage.total_output_tokens,
                "thinking_tokens": usage.total_thought_tokens,
                "total_tokens": usage.total_tokens,
            } if usage else None,
        }
        if status != "completed":
            details = "; ".join(f"{e.code}: {e.message}" for e in (interaction.errors or []))
            raise ProviderError(f"Gemini interaction ended with status={status}"
                                f"{' (' + details + ')' if details else ''}; response not used")
        if not interaction.output_text:
            raise ProviderError("Gemini interaction completed without text output; response not used")
        return ProviderResult(text=interaction.output_text, metadata=metadata)
