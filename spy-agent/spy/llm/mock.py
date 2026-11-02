"""Offline mock model. Lets you verify the install (UI, tools, permissions)
with no API at all:  spy --model mock

Behaviour: if a script is queued (tests, or SPY_MOCK_SCRIPT=path.json holding a
list of responses), replay it. Otherwise, a tiny rule-based "model":
  "@tool NAME {json}"  -> calls that tool
  anything else        -> echoes the message back.
A scripted response is {"content": "...", "tool_calls": [{"name":..,"arguments":{..}}]}.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from spy.llm.base import LLMClient, LLMResponse, ToolCall


class MockClient(LLMClient):
    def __init__(self, profile: Dict[str, Any]):
        super().__init__(profile)
        self.script: List[Dict[str, Any]] = list(profile.get("script") or [])
        path = os.environ.get("SPY_MOCK_SCRIPT")
        if path and not self.script:
            with open(path, encoding="utf-8") as f:
                self.script = json.load(f)
        self.requests: List[Dict[str, Any]] = []

    def queue(self, *responses: Dict[str, Any]) -> None:
        self.script.extend(responses)

    def _chat(self, messages, tools, stream_cb, temperature, max_tokens, json_mode) -> LLMResponse:
        self.requests.append({"messages": messages, "tools": tools})
        if self.script:
            r = self.script.pop(0)
            if callable(r):
                r = r(messages, tools)
            calls = [ToolCall(name=c["name"], arguments=c.get("arguments", {}))
                     for c in r.get("tool_calls", [])]
            text = r.get("content", "")
            if stream_cb and text:
                stream_cb("text", text)
            return LLMResponse(content=text, tool_calls=calls, finish_reason="stop")
        last = messages[-1]
        if last["role"] == "tool":
            text = "Tool returned:\n" + (last.get("content") or "")[:2000]
            return LLMResponse(content=text, finish_reason="stop")
        user = last.get("content") or ""
        if user.startswith("@tool "):
            parts = user[6:].split(" ", 1)
            args = json.loads(parts[1]) if len(parts) > 1 else {}
            return LLMResponse(content="", tool_calls=[ToolCall(name=parts[0], arguments=args)])
        text = "(mock model) You said: " + user[:500]
        if stream_cb:
            stream_cb("text", text)
        return LLMResponse(content=text, finish_reason="stop")
