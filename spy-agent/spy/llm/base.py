"""Provider-neutral LLM types and helpers.

Internally every conversation is a list of OpenAI-style message dicts:
  {"role": "system"|"user"|"assistant"|"tool", "content": str,
   "tool_calls": [{"id","type":"function","function":{"name","arguments"}}],
   "tool_call_id": str, "name": str}
Provider adapters translate this to whatever the backend wants.
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]
    id: str = ""
    raw_arguments: str = ""
    parse_error: Optional[str] = None

    def __post_init__(self):
        if not self.id:
            self.id = "call_" + uuid.uuid4().hex[:12]

    def to_message_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name,
                             "arguments": self.raw_arguments or json.dumps(self.arguments)}}


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    reasoning: str = ""
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None


class LLMError(Exception):
    pass


StreamCallback = Callable[[str, str], None]  # (kind: "text"|"reasoning", chunk)


class LLMClient:
    """Base class. Subclasses implement _chat()."""

    def __init__(self, profile: Dict[str, Any]):
        self.profile = profile
        self.name = profile.get("name", profile.get("model", "?"))
        ctx = profile.get("context_tokens", 32000)
        # "auto": ask the server (vLLM reports max_model_len on /models); fall back if it can't say
        self.context_auto = str(ctx).lower() == "auto"
        self.context_tokens = int(profile.get("context_fallback", 65536)) if self.context_auto else int(ctx)
        self.context_source = "fallback" if self.context_auto else "config"
        self.max_output_tokens = int(profile.get("max_output_tokens", 4096))
        self.tool_mode = profile.get("tool_mode", "auto")
        self.supports_vision = bool(profile.get("vision", True))
        self.image_max_px = int(profile.get("image_max_px", 1568))
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
             stream_cb: Optional[StreamCallback] = None, temperature: Optional[float] = None,
             max_tokens: Optional[int] = None, json_mode: bool = False) -> LLMResponse:
        if tools and self.tool_mode == "prompted":
            msgs = to_prompted_messages(messages, tools)
            resp = self._chat(msgs, None, stream_cb, temperature, max_tokens, json_mode)
            resp.content, resp.tool_calls = extract_tool_calls_from_text(resp.content)
        else:
            resp = self._chat(messages, tools, stream_cb, temperature, max_tokens, json_mode)
            if tools and not resp.tool_calls and self.tool_mode == "auto":
                # Many open models emit tool calls as text even in native mode.
                content, calls = extract_tool_calls_from_text(resp.content,
                                                              known={t["function"]["name"] for t in tools})
                if calls:
                    resp.content, resp.tool_calls = content, calls
        self.total_usage["calls"] += 1
        if not resp.usage:  # streaming servers often omit usage - estimate it
            resp.usage = {"prompt_tokens": estimate_tokens(messages) + (estimate_tokens(str(tools)) if tools else 0),
                          "completion_tokens": estimate_tokens(resp.content or "") + estimate_tokens(resp.reasoning or ""),
                          "estimated": True}
        for k in ("prompt_tokens", "completion_tokens"):
            try:
                self.total_usage[k] += int(resp.usage.get(k, 0) or 0)
            except (TypeError, ValueError):
                pass
        return resp

    def _chat(self, messages, tools, stream_cb, temperature, max_tokens, json_mode) -> LLMResponse:
        raise NotImplementedError

    def apply_context_limit(self, detected: Optional[int]) -> None:
        """Use the server-reported context length, optionally capped by `context_cap` (a smaller
        working window keeps local inference fast and leaves quality headroom)."""
        if detected and detected > 0:
            self.context_tokens = int(detected)
            self.context_source = "server"
        cap = self.profile.get("context_cap")
        if cap:
            self.context_tokens = min(self.context_tokens, int(cap))

    # Convenience for one-shot prompts used by research/compaction.
    def complete(self, prompt: str, system: str = "", max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return self.chat(msgs, max_tokens=max_tokens, temperature=temperature).content


# ---------------------------------------------------------------------------
# JSON repair - small models produce sloppy JSON. Be forgiving.
# ---------------------------------------------------------------------------

def parse_json_loose(text: str) -> Any:
    """Parse JSON that may be wrapped in code fences, have trailing commas,
    single quotes, Python literals, or surrounding prose."""
    if text is None:
        raise ValueError("empty")
    if isinstance(text, (dict, list)):
        return text
    s = text.strip()
    if not s:
        return {}
    m = re.search(r"```(?:json|JSON)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # extract first balanced {...} or [...]
    cand = _first_balanced(s)
    if cand:
        s = cand
    for attempt in (s, re.sub(r",\s*([}\]])", r"\1", s)):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass
        try:
            py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False",
                        re.sub(r"\bnull\b", "None", attempt)))
            val = ast.literal_eval(py)
            if isinstance(val, (dict, list)):
                return val
        except Exception:
            pass
    # unescaped newlines inside strings are a frequent failure mode
    try:
        return json.loads(s, strict=False)
    except json.JSONDecodeError as e:
        raise ValueError("could not parse JSON: %s" % e)


def _first_balanced(s: str) -> Optional[str]:
    start = None
    for i, ch in enumerate(s):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str, quote = True, ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


# ---------------------------------------------------------------------------
# Prompted tool calling (for models/servers without native tool support)
# ---------------------------------------------------------------------------

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
# Other formats seen in the wild from open-weight models
ALT_PATTERNS = [
    re.compile(r"<\|tool_call\|>\s*(.*?)\s*(?:<\|/tool_call\|>|$)", re.S),
    re.compile(r"<function_call>\s*(.*?)\s*</function_call>", re.S),
    re.compile(r"```tool_call\s*(.*?)```", re.S),
]


def extract_tool_calls_from_text(text: str, known: Optional[set] = None):
    """Return (remaining_text, [ToolCall]). Recognises <tool_call>{json}</tool_call>
    and a few common variants. If `known` is given, a bare JSON object whose
    "name" is a known tool is also accepted."""
    if not text:
        return text or "", []
    calls: List[ToolCall] = []
    remaining = text
    for pat in [TOOL_CALL_RE] + ALT_PATTERNS:
        found = pat.findall(remaining)
        if not found:
            continue
        for blob in found:
            call = _blob_to_call(blob)
            if call:
                calls.append(call)
        remaining = pat.sub("", remaining)
    if not calls and known:
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("```"):
            call = _blob_to_call(stripped)
            if call and call.name in known:
                calls.append(call)
                remaining = ""
    return remaining.strip(), calls


def _blob_to_call(blob: str) -> Optional[ToolCall]:
    try:
        obj = parse_json_loose(blob)
    except ValueError as e:
        m = re.search(r'"name"\s*:\s*"([^"]+)"', blob)
        if m:
            return ToolCall(name=m.group(1), arguments={}, raw_arguments=blob,
                            parse_error="Arguments were not valid JSON: %s" % e)
        return None
    if isinstance(obj, list) and obj:
        obj = obj[0]
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    if isinstance(name, dict):  # {"function": {"name":..,"arguments":..}}
        obj = name
        name = obj.get("name")
    if not name:
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("args", obj.get("input", {}))))
    if isinstance(args, str):
        try:
            args = parse_json_loose(args)
        except ValueError:
            return ToolCall(name=name, arguments={}, raw_arguments=args,
                            parse_error="Arguments were not valid JSON")
    if not isinstance(args, dict):
        args = {}
    return ToolCall(name=str(name), arguments=args)


def render_tools_for_prompt(tools: List[Dict[str, Any]]) -> str:
    lines = []
    for t in tools:
        f = t["function"]
        params = f.get("parameters", {})
        props = params.get("properties", {})
        req = set(params.get("required", []))
        plines = []
        for pname, p in props.items():
            typ = p.get("type", "any")
            if "enum" in p:
                typ += " one of " + "/".join(str(x) for x in p["enum"])
            plines.append("    - %s (%s%s): %s" % (pname, typ, ", required" if pname in req else "",
                                                   p.get("description", "")))
        lines.append("* %s: %s\n%s" % (f["name"], f.get("description", "").strip(),
                                       "\n".join(plines) or "    (no parameters)"))
    return "\n".join(lines)


PROMPTED_TOOLS_HEADER = """# Tools
You can call tools. To call a tool, output EXACTLY this format (one block per call; you may emit several):
<tool_call>
{"name": "TOOL_NAME", "arguments": {"param": "value"}}
</tool_call>
The arguments must be valid JSON (double quotes, escaped newlines as \\n). After you emit tool calls, STOP and wait: results come back in <tool_result> blocks. When you are done and need no more tools, reply normally without any <tool_call> block.

Available tools:
"""


def to_prompted_messages(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert native-tool conversation into plain text conversation."""
    tool_text = PROMPTED_TOOLS_HEADER + render_tools_for_prompt(tools)
    out: List[Dict[str, Any]] = []
    sys_done = False
    for m in messages:
        role = m["role"]
        if role == "system" and not sys_done:
            out.append({"role": "system", "content": (m.get("content") or "") + "\n\n" + tool_text})
            sys_done = True
        elif role == "assistant":
            text = m.get("content") or ""
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                text += "\n<tool_call>\n%s\n</tool_call>" % json.dumps(
                    {"name": fn["name"], "arguments": _safe_args(fn.get("arguments"))})
            out.append({"role": "assistant", "content": text.strip()})
        elif role == "tool":
            block = '<tool_result name="%s">\n%s\n</tool_result>' % (m.get("name", ""), m.get("content", ""))
            if out and out[-1]["role"] == "user" and out[-1].get("_tool_results"):
                out[-1]["content"] += "\n" + block
            else:
                out.append({"role": "user", "content": block, "_tool_results": True})
        else:
            mm = {"role": role, "content": m.get("content") or ""}
            if m.get("_images"):
                mm["_images"] = m["_images"]
            out.append(mm)
    if not sys_done:
        out.insert(0, {"role": "system", "content": tool_text})
    for m in out:
        m.pop("_tool_results", None)
    return out


def content_with_images(text: str, images, max_px: int = 1568):
    """OpenAI-style multimodal content: text part + one image_url part per image file."""
    from spy.images import image_data_url
    parts = [{"type": "text", "text": text or ""}]
    for p in images or []:
        try:
            parts.append({"type": "image_url", "image_url": {"url": image_data_url(p, max_px)}})
        except OSError:
            parts[0]["text"] += "\n[image %s could not be read]" % p
    return parts


def _safe_args(a):
    if isinstance(a, dict):
        return a
    try:
        return json.loads(a or "{}")
    except Exception:
        return {"_raw": a}


def estimate_tokens(obj: Any) -> int:
    """Cheap token estimate (~3.5 chars/token) - no tokenizer dependency."""
    if isinstance(obj, str):
        return int(len(obj) / 3.5) + 1
    if isinstance(obj, list):
        return sum(estimate_tokens(x) for x in obj)
    if isinstance(obj, dict):
        n = 4
        for k in ("content", "reasoning"):
            if obj.get(k):
                v = obj[k]
                if isinstance(v, list):  # multimodal parts: count text, flat cost for media
                    v = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in v)
                    n += 1500 * sum(1 for p in obj[k] if isinstance(p, dict) and p.get("type") != "text")
                n += estimate_tokens(v if isinstance(v, str) else json.dumps(v))
        if obj.get("tool_calls"):
            n += estimate_tokens(json.dumps(obj["tool_calls"]))
        n += 1500 * len(obj.get("_images") or [])
        return n
    return 1
