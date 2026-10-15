"""LLM provider interface plus the DeepSeek (OpenAI-compatible) implementation."""

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx

from app.config import get_settings

log = logging.getLogger("llm")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # raw JSON string, exactly as the model produced it


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0  # prompt tokens served from the provider's cache

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_message(self) -> dict[str, Any]:
        """The assistant message to append to the conversation history."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        return msg


class LLMProvider(Protocol):
    model: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse: ...


class DeepSeekProvider:
    """Chat-completions client with a per-request timeout and exponential-backoff retries."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        recorder: Callable[..., None] | None = None,
    ) -> None:
        s = get_settings()
        self.api_key = api_key if api_key is not None else s.deepseek_api_key
        self.base_url = (base_url or s.deepseek_base_url).rstrip("/")
        self.model = model or s.deepseek_model
        self.max_retries = s.deepseek_max_retries if max_retries is None else max_retries
        self._client = client or httpx.Client(timeout=timeout_s or s.deepseek_timeout_s)
        self._sleep = sleep
        self._recorder = recorder
        if not self.api_key:
            raise LLMError("DEEPSEEK_API_KEY is not set.")

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.1}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            except httpx.TransportError as exc:  # timeouts, connection resets, DNS
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    parsed = self._parse(resp.json())
                    self._record(parsed, started, attempt)
                    return parsed
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in RETRYABLE_STATUS:
                    self._record(None, started, attempt, last_error)
                    raise LLMError(last_error)

            if attempt < self.max_retries:
                delay = min(2**attempt, 20) + random.uniform(0, 0.5)
                log.warning("LLM call failed (%s); retry %d in %.1fs", last_error, attempt + 1, delay)
                self._sleep(delay)
        self._record(None, started, self.max_retries, last_error)
        raise LLMError(f"LLM unavailable after {self.max_retries + 1} attempts: {last_error}")

    def _record(self, resp: "LLMResponse | None", started: float, retries: int, error: str | None = None) -> None:
        recorder = self._recorder
        if recorder is None:
            from app.agent.llm_log import record_llm_call as recorder
        recorder(
            model=self.model,
            prompt_tokens=resp.prompt_tokens if resp else 0,
            completion_tokens=resp.completion_tokens if resp else 0,
            cached_tokens=resp.cached_tokens if resp else 0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            status="ok" if resp else "error",
            error=error,
            retries=retries,
        )

    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMResponse:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Malformed LLM response: {str(data)[:200]}") from exc
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content"),
            tool_calls=[
                ToolCallRequest(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"].get("arguments") or "{}",
                )
                for tc in message.get("tool_calls") or []
            ],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cached_tokens=usage.get("prompt_cache_hit_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            or 0,
        )
