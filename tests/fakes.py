"""A scripted LLM: replays pre-written assistant turns so the agent loop runs without network."""

import itertools
import json
from typing import Any

from app.agent.llm import LLMResponse, ToolCallRequest

_ids = itertools.count(1)


def call(_tool: str, **args: Any) -> ToolCallRequest:
    return ToolCallRequest(id=f"call_{next(_ids)}", name=_tool, arguments=json.dumps(args, ensure_ascii=False))


def turn(*calls: ToolCallRequest, content: str | None = None) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=list(calls), prompt_tokens=200, completion_tokens=30)


class ScriptedLLM:
    model = "scripted"

    def __init__(self, *turns: LLMResponse) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict]] = []  # messages passed on each call
        self.tool_names: list[str] = []

    def chat(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        self.tool_names = [t["function"]["name"] for t in tools]
        if not self.turns:
            raise AssertionError("agent asked for more turns than the script provides")
        return self.turns.pop(0)

    def last_tool_results(self) -> list[dict]:
        return [json.loads(m["content"]) for m in self.seen[-1] if m["role"] == "tool"]
