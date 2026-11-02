"""OpenAI-compatible /chat/completions client (stdlib only).

Works with vLLM, TGI, LiteLLM, Ollama (/v1), llama.cpp server, and most
enterprise LLM gateways. If your gateway is NOT OpenAI-shaped, copy
custom_template.py and adapt the two marked functions.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from hsa import net
from hsa.llm.base import LLMClient, LLMError, LLMResponse, ToolCall, parse_json_loose

THINK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.S)


class OpenAICompatClient(LLMClient):
    def __init__(self, profile: Dict[str, Any]):
        super().__init__(profile)
        self.pcfg = dict(profile["provider_config"])
        self.base_url = self.pcfg.get("base_url", "").rstrip("/")
        self.model = profile.get("model")
        self.stream_enabled = bool(profile.get("stream", True))
        self.temperature = profile.get("temperature", 0.2)
        self.extra_body = profile.get("extra_body") or {}
        self.chat_path = self.pcfg.get("chat_path", "/chat/completions")

    # -- request ---------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        h = {}
        key = self.pcfg.get("api_key")
        if key and not key.startswith("<"):
            style = self.pcfg.get("auth_style", "bearer")
            if style == "bearer":
                h["Authorization"] = "Bearer " + key
            elif style == "api-key":
                h["api-key"] = key
            elif style == "x-api-key":
                h["x-api-key"] = key
        return h

    @staticmethod
    def _clean_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for m in messages:
            mm = {k: v for k, v in m.items() if not k.startswith("_")}
            if mm.get("content") is None:
                mm["content"] = ""
            if mm["role"] == "assistant" and not mm.get("tool_calls"):
                mm.pop("tool_calls", None)
            out.append(mm)
        return out

    def _chat(self, messages, tools, stream_cb, temperature, max_tokens, json_mode) -> LLMResponse:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": self._clean_messages(messages),
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_output_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode and self.pcfg.get("supports_json_mode", False):
            body["response_format"] = {"type": "json_object"}
        body.update(self.extra_body)
        url = self.base_url + self.chat_path
        use_stream = self.stream_enabled and stream_cb is not None

        def do():
            if use_stream:
                return self._do_stream(url, dict(body, stream=True), stream_cb)
            status, _, raw = net.post_json(url, body, self.pcfg, self._headers(),
                                           timeout=self.pcfg.get("timeout", 300))
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                raise LLMError("Non-JSON response from %s: %s" % (url, raw[:500]))
            return self._parse_full(data)

        try:
            return net.with_retries(do, retries=int(self.pcfg.get("max_retries", 3)))
        except net.HTTPError as e:
            if use_stream and e.status in (400, 404, 415, 422):
                # streaming not supported here - disable and retry once
                self.stream_enabled = False
                return self._chat(messages, tools, None, temperature, max_tokens, json_mode)
            raise LLMError(str(e))

    # -- response parsing -------------------------------------------------
    def _parse_full(self, data: Dict[str, Any]) -> LLMResponse:
        if "error" in data and not data.get("choices"):
            raise LLMError("API error: %s" % data["error"])
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError):
            raise LLMError("Unexpected response shape: %s" % str(data)[:800])
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):  # some gateways return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        calls = [self._mk_call(tc.get("id"), tc.get("function", {}).get("name"),
                               tc.get("function", {}).get("arguments"))
                 for tc in (msg.get("tool_calls") or [])]
        # legacy single function_call
        if not calls and msg.get("function_call"):
            fc = msg["function_call"]
            calls = [self._mk_call(None, fc.get("name"), fc.get("arguments"))]
        content, reasoning = _split_thinking(content, reasoning)
        return LLMResponse(content=content, tool_calls=[c for c in calls if c.name],
                           reasoning=reasoning, finish_reason=choice.get("finish_reason") or "",
                           usage=data.get("usage") or {}, raw=data)

    def _do_stream(self, url, body, cb) -> LLMResponse:
        resp = net.post_json(url, body, self.pcfg, self._headers(),
                             timeout=self.pcfg.get("timeout", 300), stream=True)
        content, reasoning, finish, usage = [], [], "", {}
        calls: Dict[int, Dict[str, Any]] = {}
        think = ThinkFilter()
        try:
            for payload in net.iter_sse(resp):
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if data.get("usage"):
                    usage = data["usage"]
                if not data.get("choices"):
                    continue
                ch = data["choices"][0]
                delta = ch.get("delta") or {}
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                r = delta.get("reasoning_content") or delta.get("reasoning")
                if r:
                    reasoning.append(r)
                    cb("reasoning", r)
                t = delta.get("content")
                if t:
                    content.append(t)
                    # hide inline <think> blocks from the live display
                    visible, hidden = think.feed(t)
                    if hidden:
                        cb("reasoning", hidden)
                    if visible:
                        cb("text", visible)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", len(calls))
                    slot = calls.setdefault(idx, {"id": None, "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        a = fn["arguments"]
                        slot["args"] += a if isinstance(a, str) else json.dumps(a)
        finally:
            resp.close()
        tail = think.flush()
        if tail:
            cb("text", tail)
        text, reas = _split_thinking("".join(content), "".join(reasoning))
        tcs = [self._mk_call(s["id"], s["name"], s["args"]) for _, s in sorted(calls.items())]
        return LLMResponse(content=text, tool_calls=[c for c in tcs if c.name], reasoning=reas,
                           finish_reason=finish, usage=usage)

    @staticmethod
    def _mk_call(cid, name, args) -> ToolCall:
        raw = args if isinstance(args, str) else json.dumps(args or {})
        err = None
        try:
            parsed = parse_json_loose(args) if args else {}
            if not isinstance(parsed, dict):
                parsed, err = {}, "arguments must be a JSON object"
        except ValueError as e:
            parsed, err = {}, "Arguments were not valid JSON (%s). Raw: %s" % (e, raw[:300])
        return ToolCall(id=cid or "", name=name or "", arguments=parsed, raw_arguments=raw,
                        parse_error=err)

    # -- misc -------------------------------------------------------------
    def list_models(self) -> List[str]:
        data = net.get_json(self.base_url + "/models", self.pcfg, self._headers())
        return [m.get("id") for m in data.get("data", [])]


class ThinkFilter:
    """Streaming filter that separates <think>...</think> (or <thinking>) text
    from visible text, even when tags are split across chunks."""
    OPEN = ("<think>", "<thinking>")
    CLOSE = ("</think>", "</thinking>")

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, chunk: str):
        self.buf += chunk
        visible, hidden = [], []
        while self.buf:
            tags = self.CLOSE if self.inside else self.OPEN
            idx, tag = min(((self.buf.find(t), t) for t in tags if self.buf.find(t) >= 0), default=(-1, ""))
            if idx >= 0:
                (hidden if self.inside else visible).append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # keep a possible partial tag at the end
            keep = 0
            for t in tags:
                for k in range(1, len(t)):
                    if self.buf.endswith(t[:k]):
                        keep = max(keep, k)
            out, self.buf = (self.buf[:-keep], self.buf[-keep:]) if keep else (self.buf, "")
            (hidden if self.inside else visible).append(out)
            break
        return "".join(visible), "".join(hidden)

    def flush(self) -> str:
        out, self.buf = ("" if self.inside else self.buf), ""
        return out


def _split_thinking(content: str, reasoning: str):
    found = THINK_RE.findall(content or "")
    if found:
        reasoning = (reasoning + "\n" + "\n".join(found)).strip()
        content = THINK_RE.sub("", content).strip()
    return content, reasoning
