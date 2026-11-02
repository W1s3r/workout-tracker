"""Template for a NON-OpenAI-compatible LLM gateway.

If `spy doctor` shows your work API does not speak the OpenAI
/chat/completions format, copy this file to e.g. spy/llm/mygateway.py, fill in
build_request() and parse_response(), and set in config:

    "providers": {"work": {"type": "custom", "module": "spy.llm.mygateway", ...}}

Use "prompted" tool_mode in the model profile unless your gateway has its own
native tool-call format (then map it in parse_response()).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from spy import net
from spy.llm.base import LLMClient, LLMError, LLMResponse, ToolCall


class CustomClient(LLMClient):
    def __init__(self, profile: Dict[str, Any]):
        super().__init__(profile)
        self.pcfg = profile["provider_config"]
        self.url = self.pcfg["base_url"]  # full endpoint URL

    # ---- 1. turn our message list into YOUR API's request -------------
    def build_request(self, messages: List[Dict[str, Any]], tools, temperature, max_tokens) -> Dict[str, Any]:
        # EXAMPLE for an API that takes a single prompt string:
        prompt = "\n\n".join("%s: %s" % (m["role"].upper(), m.get("content") or "") for m in messages)
        return {
            "model": self.profile.get("model"),
            "prompt": prompt + "\n\nASSISTANT:",
            "temperature": 0.2 if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_output_tokens,
        }

    # ---- 2. turn YOUR API's response into an LLMResponse ---------------
    def parse_response(self, data: Dict[str, Any]) -> LLMResponse:
        # EXAMPLE: {"output": "text..."}
        text = data.get("output") or data.get("text") or data.get("response") or ""
        calls: List[ToolCall] = []  # map native tool calls here if your API has them
        return LLMResponse(content=text, tool_calls=calls, raw=data)

    def _chat(self, messages, tools, stream_cb, temperature, max_tokens, json_mode) -> LLMResponse:
        body = self.build_request(messages, tools, temperature, max_tokens)
        headers = {}
        if self.pcfg.get("api_key"):
            headers["Authorization"] = "Bearer " + self.pcfg["api_key"]
        try:
            _, _, raw = net.with_retries(lambda: net.post_json(self.url, body, self.pcfg, headers))
        except net.HTTPError as e:
            raise LLMError(str(e))
        return self.parse_response(json.loads(raw.decode("utf-8", "replace")))
