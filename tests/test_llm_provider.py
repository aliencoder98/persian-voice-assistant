import json

import httpx
import pytest

from app.agent.llm import DeepSeekProvider, LLMError

OK_BODY = {
    "choices": [
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_current_datetime", "arguments": "{}"},
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
}


def make(handler, retries=3):
    sleeps: list[float] = []
    provider = DeepSeekProvider(
        api_key="k",
        base_url="https://example.test",
        model="m",
        max_retries=retries,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    return provider, sleeps


def test_parses_tool_calls_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    provider, _ = make(handler)
    resp = provider.chat([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert resp.tool_calls[0].name == "get_current_datetime"
    assert resp.total_tokens == 120
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "m" and seen["body"]["tool_choice"] == "auto"


def test_retries_with_backoff_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        if calls["n"] == 2:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json=OK_BODY)

    provider, sleeps = make(handler)
    assert provider.chat([], []).tool_calls
    assert calls["n"] == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] - 0.5  # exponential, with jitter


def test_gives_up_after_max_retries():
    provider, sleeps = make(lambda r: httpx.Response(500, text="boom"), retries=2)
    with pytest.raises(LLMError):
        provider.chat([], [])
    assert len(sleeps) == 2


def test_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    provider, sleeps = make(handler)
    with pytest.raises(LLMError):
        provider.chat([], [])
    assert calls["n"] == 1 and not sleeps


def test_missing_key_is_rejected():
    with pytest.raises(LLMError):
        DeepSeekProvider(api_key="", base_url="https://x", client=httpx.Client())
