"""`spy doctor` - probe the LLM API and search sources and report
what works, so you know which settings to use (tool_mode, stream, etc.)."""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time

from spy.llm import make_client
from spy.llm.base import LLMError

TEST_TOOL = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get the weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]


def _ok(msg):
    print("  [ OK ] " + msg)


def _bad(msg):
    print("  [FAIL] " + msg)


def _note(msg):
    print("  [info] " + msg)


def run_doctor(config, model_name=None):
    print("Spy doctor")
    print("=" * 60)
    print("Python %s at %s" % (platform.python_version(), sys.executable))
    print("Platform: %s" % platform.platform())
    print("Config files: %s" % ", ".join(config.sources))
    for mod in ("prompt_toolkit", "rich", "openpyxl", "pypdf", "PyPDF2", "fitz", "colorama"):
        try:
            __import__(mod)
            _ok("optional module %s available" % mod)
        except Exception:
            _note("optional module %s not available (fallback used)" % mod)
    for exe in ("git", "rg", "pdftotext"):
        (_ok if shutil.which(exe) else _note)("%s %s" % (exe, "on PATH" if shutil.which(exe) else "not found"))

    names = [model_name] if model_name else sorted(set(config["roles"].values()))
    for name in names:
        print("\nModel '%s'" % name)
        try:
            prof = config.model_profile(name)
        except KeyError as e:
            _bad(str(e))
            continue
        pc = prof["provider_config"]
        if "<" in str(pc.get("base_url", "")) or "<" in str(prof.get("model", "")):
            _bad("placeholders not filled in (base_url=%s, model=%s)" % (pc.get("base_url"), prof.get("model")))
            continue
        client = make_client(prof)
        if hasattr(client, "detect_context_length"):
            det = client.detect_context_length()
            if det:
                _ok("server reports a context window of %d tokens%s" % (
                    det, "" if client.context_tokens == det else " (Spy will use %d: context_cap)" % client.context_tokens))
            else:
                _note("server did not report a context window; Spy uses %d tokens (set \"context_tokens\" to the "
                      "real value if you know it - ask whoever runs the server for vLLM's --max-model-len)"
                      % client.context_tokens)
        if hasattr(client, "list_models"):
            try:
                ms = client.list_models()
                _ok("GET /models -> %d models: %s" % (len(ms), ", ".join(ms[:15])))
                if prof.get("model") not in ms:
                    _note("configured model id '%s' not in that list - check the id" % prof.get("model"))
            except Exception as e:
                _note("GET /models failed (not fatal): %s" % str(e)[:200])
        # 1. plain chat
        t0 = time.time()
        try:
            r = client.chat([{"role": "user", "content": "Reply with exactly: PONG"}], max_tokens=20)
            _ok("basic chat in %.1fs -> %r" % (time.time() - t0, r.content[:60]))
            if r.reasoning:
                _note("model returns separate reasoning text (%d chars)" % len(r.reasoning))
            if r.usage:
                _note("usage reported: %s" % json.dumps(r.usage))
        except LLMError as e:
            _bad("basic chat failed: %s" % str(e)[:500])
            print("     -> Check base_url (should usually end in /v1), api_key, ca_bundle/verify_ssl.")
            print("     -> If the API is not OpenAI-compatible, see spy/llm/custom_template.py")
            continue
        # 2. streaming
        if hasattr(client, "stream_enabled"):
            chunks = []
            client.stream_enabled = True
            try:
                client.chat([{"role": "user", "content": "Count from 1 to 5."}], stream_cb=lambda k, c: chunks.append(c),
                            max_tokens=40)
                if chunks and client.stream_enabled:
                    _ok("streaming works (%d chunks) -> keep \"stream\": true" % len(chunks))
                else:
                    _note("streaming not supported -> set \"stream\": false")
            except LLMError as e:
                _note("streaming failed -> set \"stream\": false (%s)" % str(e)[:200])
        # 3. native tools
        client.tool_mode = "native"
        native_ok = False
        try:
            r = client.chat([{"role": "user", "content": "What's the weather in Paris? Use the tool."}], tools=TEST_TOOL,
                            max_tokens=200)
            if r.tool_calls and r.tool_calls[0].name == "get_weather":
                native_ok = True
                _ok("native tool calling works -> %s(%s)" % (r.tool_calls[0].name, r.tool_calls[0].arguments))
                # round trip with a tool result
                msgs = [{"role": "user", "content": "What's the weather in Paris? Use the tool."},
                        {"role": "assistant", "content": "", "tool_calls": [r.tool_calls[0].to_message_dict()]},
                        {"role": "tool", "tool_call_id": r.tool_calls[0].id, "name": "get_weather",
                         "content": "Sunny, 21C"}]
                r2 = client.chat(msgs, tools=TEST_TOOL, max_tokens=100)
                (_ok if "21" in r2.content or "sunny" in r2.content.lower() else _note)(
                    "tool result round-trip -> %r" % r2.content[:100])
            else:
                _note("no native tool call returned (content: %r)" % r.content[:150])
                if r.content and ("<|" in r.content or "tool" in r.content.lower()):
                    _note("the model seems to emit tool calls as raw text: the server was probably started "
                          "without a tool parser (for Inkling on vLLM: --enable-auto-tool-choice "
                          "--tool-call-parser inkling --reasoning-parser inkling). Spy's prompted mode works meanwhile.")
        except LLMError as e:
            _note("native tools request rejected: %s" % str(e)[:200])
        # 4. prompted tools
        client.tool_mode = "prompted"
        try:
            r = client.chat([{"role": "user", "content": "What's the weather in Paris? Use the tool."}], tools=TEST_TOOL,
                            max_tokens=200)
            if r.tool_calls:
                _ok("prompted tool calling works -> %s(%s)" % (r.tool_calls[0].name, r.tool_calls[0].arguments))
            else:
                _note("prompted tool calling did not produce a call: %r" % r.content[:150])
        except LLMError as e:
            _bad("prompted mode failed: %s" % e)
        print("  => recommended \"tool_mode\": %s" % ('"auto" (native works)' if native_ok else '"prompted"'))

    print("\nSearch sources")
    from spy.research.sources import build_sources
    srcs = build_sources(config)
    if not srcs:
        _note("no sources enabled (config search.sources.*.enabled)")
    for s in srcs:
        try:
            t0 = time.time()
            res = s.search("test", 1, 5)
            _ok("%s (%s): %d results in %.1fs" % (s.name, s.cfg.get("type"), len(res), time.time() - t0))
            for r in res[:3]:
                print("         - %s | %s" % (r.title[:60], r.url[:80]))
            if not res:
                _note("0 results - check url template / result_selector_regex")
        except Exception as e:
            _bad("%s: %s" % (s.name, str(e)[:300]))
    if config.get("locations"):
        print("\nLocations")
        for k, v in config.get("locations").items():
            (_ok if os.path.isdir(v) else _bad)("%s -> %s" % (k, v))
    print("\nDone.")
