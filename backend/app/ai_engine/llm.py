"""OpenAI client wrapper.

Build spec section 5 requires explicit handling of: tool/function calling, structured outputs,
schema validation, retries, timeout handling, malformed response handling, token accounting and
deterministic tool results. All of that lives here, in one place, so the reasoners stay
readable.

The model can request tools; it can never execute anything. Tool calls are dispatched through
``ToolRegistry``, which validates the name against the exposure list and writes the ledger.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from app.core.config import settings
from app.core.errors import AIMalformedResponseError, AIUnavailableError

logger = logging.getLogger(__name__)

MAX_SCHEMA_REPAIR_ATTEMPTS = 1


@dataclass
class ToolLoopResult:
    final_text: str
    rounds: int
    tool_messages: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    hit_round_limit: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_text": self.final_text,
            "rounds": self.rounds,
            "tool_calls": len(self.tool_messages),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "hit_round_limit": self.hit_round_limit,
            "errors": self.errors,
        }


@dataclass
class LLMStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    failures: int = 0
    last_error: str = ""


class LLMClient:
    """Thin, defensive wrapper around the OpenAI chat completions API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.openai_api_key) or ""
        self.model = model or settings.openai_model
        self.timeout = timeout or settings.openai_timeout_seconds
        self.stats = LLMStats()
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key.strip())

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.available:
            raise AIUnavailableError(
                "OPENAI_API_KEY is not configured; the deterministic offline reasoner is in use"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise AIUnavailableError("the openai package is not installed") from exc
        self._client = OpenAI(
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=0,
            base_url=settings.openai_base_url.strip() or None,
        )
        return self._client

    # -- low level ---------------------------------------------------------------------
    def _call(self, **kwargs: Any) -> Any:
        client = self._ensure_client()
        attempts = max(1, settings.openai_max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = client.chat.completions.create(**kwargs)
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self.stats.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                    self.stats.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
                self.stats.calls += 1
                return response
            except Exception as exc:  # noqa: BLE001 - openai exception hierarchy varies by version
                last_error = exc
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                retryable = self._is_retryable(exc)
                logger.warning(
                    "LLM call failed (attempt %d/%d, retryable=%s): %s",
                    attempt + 1,
                    attempts,
                    retryable,
                    self.stats.last_error,
                )
                if not retryable or attempt == attempts - 1:
                    break
                self.stats.retries += 1
                # Exponential backoff with jitter: prevents synchronised retry storms.
                time.sleep(min(8.0, (2**attempt) * 0.5 + random.random() * 0.25))
        self.stats.failures += 1
        raise AIUnavailableError(
            f"OpenAI request failed after {attempts} attempt(s): {self.stats.last_error}"
        ) from last_error

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        if any(token in name for token in ("timeout", "connection", "ratelimit", "apiconnection")):
            return True
        if any(token in message for token in ("timeout", "timed out", "rate limit", "overloaded", "502", "503", "504")):
            return True
        if any(token in message for token in ("invalid api key", "incorrect api key", "401", "403")):
            return False
        return False

    # -- tool loop ---------------------------------------------------------------------
    def run_tool_loop(
        self,
        *,
        system: str,
        user: str,
        tools: Sequence[dict[str, Any]],
        executor: Callable[[str, dict[str, Any], int], dict[str, Any]],
        max_rounds: int | None = None,
        temperature: float | None = None,
        final_instruction: str = "",
    ) -> ToolLoopResult:
        """Drive a bounded tool-calling loop.

        ``executor(name, arguments, round_index) -> {"ok":bool, "data":..., "error_code":...}``.
        The loop is bounded by ``max_rounds`` so a confused model cannot burn the demo.
        """
        rounds_limit = max_rounds or settings.openai_max_tool_rounds
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        result = ToolLoopResult(final_text="", rounds=0)

        for round_index in range(rounds_limit):
            result.rounds = round_index + 1
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": settings.openai_temperature if temperature is None else temperature,
            }
            if tools:
                kwargs["tools"] = list(tools)
                kwargs["tool_choice"] = "auto"

            response = self._call(**kwargs)
            message = response.choices[0].message
            tool_calls = list(getattr(message, "tool_calls", None) or [])

            if not tool_calls:
                result.final_text = (message.content or "").strip()
                return result

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.function.name, "arguments": call.function.arguments},
                        }
                        for call in tool_calls
                    ],
                }
            )

            for call in tool_calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError) as exc:
                    arguments = {}
                    outcome = {
                        "ok": False,
                        "error_code": "AI_MALFORMED_RESPONSE",
                        "error_message": f"could not parse arguments for '{name}': {exc}",
                        "data": {},
                    }
                else:
                    outcome = executor(name, arguments, round_index)

                result.tool_messages.append(
                    {"round": round_index, "tool": name, "arguments": arguments, "outcome": outcome}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            {
                                "ok": outcome.get("ok", False),
                                "data": outcome.get("data", {}),
                                "error": outcome.get("error_message") or outcome.get("error_code"),
                            },
                            default=str,
                        )[:12000],
                    }
                )

            if round_index == rounds_limit - 2 and final_instruction:
                messages.append({"role": "system", "content": final_instruction})

        result.hit_round_limit = True
        result.final_text = (
            "Tool round limit reached before the model produced a final answer. "
            "The deterministic evidence analysis was used instead."
        )
        return result

    # -- structured decision -----------------------------------------------------------
    def structured_decision(
        self,
        *,
        system: str,
        user: str,
        function_name: str,
        schema: dict[str, Any],
        description: str = "",
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Force a single tool call so the response is schema-validated JSON.

        Using a forced function call rather than a bare JSON mode gives us validation for free
        and keeps the call compatible across SDK versions.
        """
        tool = {
            "type": "function",
            "function": {
                "name": function_name,
                "description": description or f"Return the {function_name} payload.",
                "parameters": schema,
            },
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        last_error = ""
        for attempt in range(MAX_SCHEMA_REPAIR_ATTEMPTS + 1):
            response = self._call(
                model=self.model,
                messages=messages,
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": function_name}},
                temperature=settings.openai_temperature if temperature is None else temperature,
            )
            message = response.choices[0].message
            calls = list(getattr(message, "tool_calls", None) or [])
            if calls:
                try:
                    payload = json.loads(calls[0].function.arguments or "{}")
                    if isinstance(payload, dict):
                        return payload
                    last_error = "top-level JSON was not an object"
                except json.JSONDecodeError as exc:
                    last_error = f"invalid JSON: {exc}"
            else:
                last_error = "the model did not return the required structured payload"

            if attempt < MAX_SCHEMA_REPAIR_ATTEMPTS:
                messages.append({"role": "assistant", "content": message.content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"That response was not usable ({last_error}). "
                            f"Call the {function_name} function exactly once with valid JSON matching "
                            f"the provided schema. Do not include prose."
                        ),
                    }
                )

        raise AIMalformedResponseError(
            f"structured decision '{function_name}' failed validation: {last_error}"
        )


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm_client() -> None:
    global _client
    _client = None
