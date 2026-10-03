"""Gemini adapter for StructuredLLMProvider (google-genai SDK).

The only module that imports google.genai. Loaded only for live runs.
The API key is read from GEMINI_API_KEY and is never stored, logged or returned.
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
from google.genai import errors, types
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

from navigator.extraction.config import API_KEY_ENV, DEFAULT_GEMINI_MODEL, PROVIDER_GEMINI
from navigator.extraction.provider import MissingCredentialsError, ProviderError, ProviderResult

log = logging.getLogger(__name__)

# Retry policy: transport/capacity failures only (429, any 5xx, network timeouts and
# connection errors). 4xx such as 400/401/403/404 and every extraction-quality problem
# are never retried. The SDK does not retry unless retry_options are set, so this is
# the only retry layer.
MAX_ATTEMPTS = 3                 # total HTTP attempts, including the first
BACKOFF_SECONDS = (30.0, 60.0)   # wait after the 1st and 2nd transient failure
MAX_RETRY_AFTER_SECONDS = 120.0  # a server-requested delay above this is not waited out
REQUEST_TIMEOUT_MS = 180_000
SLEEP = time.sleep               # injectable for tests
_TRANSIENT_NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code == 429 or 500 <= (exc.code or 0) <= 599
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
    body = getattr(exc, "details", None)
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


def _log_retry(rs: RetryCallState) -> None:
    exc = rs.outcome.exception()
    source = "Retry-After" if retry_after_seconds(exc) is not None else "backoff"
    log.warning("attempt %d/%d failed (transient): %s -- waiting %.0fs (%s)",
                rs.attempt_number, MAX_ATTEMPTS, exc, rs.next_action.sleep, source)


class GeminiProvider:
    name = PROVIDER_GEMINI

    def __init__(self, model: str = DEFAULT_GEMINI_MODEL) -> None:
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise MissingCredentialsError(
                f"{API_KEY_ENV} is not set. Live extraction needs it: set the environment variable, "
                f"or run via `uv run --env-file .env ...` with a local, untracked .env file.")
        self.model = model
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))

    @staticmethod
    def build_config(system_instruction: str, response_json_schema: dict[str, Any],
                     settings: dict[str, Any]) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_json_schema=response_json_schema,
            temperature=settings.get("temperature"),
            seed=settings.get("seed"),
            # No tools are used; disabling AFC keeps the SDK on its single-request path.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def generate(self, *, system_instruction: str, prompt: str,
                 response_json_schema: dict[str, Any], settings: dict[str, Any]) -> ProviderResult:
        config = self.build_config(system_instruction, response_json_schema, settings)
        retrying = Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=wait_seconds,
            retry=retry_if_exception(should_retry),
            sleep=SLEEP,
            reraise=True,
            before_sleep=_log_retry,
        )
        try:
            response = retrying(self._client.models.generate_content,
                                model=self.model, contents=prompt, config=config)
        except errors.APIError as exc:
            hint = retry_after_seconds(exc)
            suffix = f" (server asked to retry after {hint:.0f}s)" if hint is not None else ""
            raise ProviderError(f"Gemini API error {exc.code} {exc.status}: {exc.message}{suffix}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error calling Gemini: {type(exc).__name__}: {exc}") from exc
        return self._to_result(response)

    def _to_result(self, response: types.GenerateContentResponse) -> ProviderResult:
        candidate = response.candidates[0] if response.candidates else None
        finish = candidate.finish_reason.name if candidate and candidate.finish_reason else None
        usage = response.usage_metadata
        metadata = {
            "model_version": response.model_version,
            "response_id": response.response_id,
            "finish_reason": finish,
            "usage": {
                "prompt_tokens": usage.prompt_token_count,
                "output_tokens": usage.candidates_token_count,
                "thinking_tokens": usage.thoughts_token_count,
                "total_tokens": usage.total_token_count,
            } if usage else None,
        }
        if finish not in (None, "STOP"):
            raise ProviderError(f"Gemini stopped with finish_reason={finish}; response not used")
        text = response.text
        if not text:
            block = response.prompt_feedback.block_reason if response.prompt_feedback else None
            raise ProviderError(f"Gemini returned no text (block_reason={block})")
        return ProviderResult(text=text, metadata=metadata)
