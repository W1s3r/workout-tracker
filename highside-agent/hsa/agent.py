"""The agent loop: model <-> tools, with permissions, hooks, robustness
against weak tool-callers, and automatic context compaction."""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hsa.context import load_context
from hsa.llm.base import LLMError, estimate_tokens
from hsa.tools import build_tools
from hsa.tools.base import ToolContext, ToolError, truncate, validate_args
from hsa.tools.shell import detect_shell, shell_kind
from hsa.ui import QuietUI

CORE_PROMPT = """You are HSA, an autonomous assistant running in the user's terminal on an internal (air-gapped) network.
You help with software engineering, scripting, file management across local and network drives,
git/GitLab work, and research using the internal search engine. You act by calling tools.

# How to work
- Use tools to look things up; never guess file contents, paths, or facts. Read a file before editing it.
- For tasks with 3+ steps, call todo_write first, keep it updated, and work through it item by item.
- Prefer the dedicated tools: read_file/list_dir/glob/grep/find_files for files, edit_file for changes,
  shell for git/builds/tests, run_python for computation and data work.
- Finding things: find_files (by name, across network locations), grep (contents), doc_search (indexed
  documents on shares), intranet_search (internal web search - use SHORT keyword queries and try
  acronym variants), fetch_url (read a page/document), deep_research (big multi-source questions).
- Use the task tool to delegate a self-contained investigation to a sub-agent when it would otherwise
  fill your context with lots of file contents or search results.
- After writing code, run it or its tests to verify. If something fails, read the error and fix it.
- When a tool returns an error, read it carefully and correct your call; don't repeat the same call.
- Git: do not commit, push, force-push, or delete branches unless the user asked. Never discard user work.
- Be careful with destructive commands (delete, overwrite, move) - especially on network shares.

# Memory
Your memory files (AGENT.md) are shown below under <context>. Use the memory tool PROACTIVELY to save
durable knowledge for future sessions: user corrections and preferences, build/test commands, important
paths and network locations, project conventions, and lessons from mistakes. Keep notes short.

# Communication
Be concise and direct. Use markdown. When you finish a task, briefly state what you did and the result.
Cite file paths as path:line and sources as URLs."""

PLAN_MODE = """
# PLAN MODE IS ACTIVE
You may only use read-only tools. Investigate, then present a clear step-by-step plan and wait for the
user to approve it (they will switch modes). Do not attempt edits or commands that change anything."""


class Agent:
    def __init__(self, config, llm, ui, permissions, cwd: Optional[Path] = None,
                 llm_factory: Optional[Callable[[str], Any]] = None, depth: int = 0,
                 tool_filter: Optional[Callable[[str], bool]] = None, extra_system: str = ""):
        self.config = config
        self.llm = llm
        self.ui = ui if depth == 0 else QuietUI(ui)
        self.permissions = permissions
        self.depth = depth
        self.extra_system = extra_system
        all_tools = build_tools(config)
        self.tools = {n: t for n, t in all_tools.items() if tool_filter is None or tool_filter(n)}
        self.ctx = ToolContext(config=config, ui=self.ui, cwd=Path(cwd or Path.cwd()).resolve(),
                               llm_factory=llm_factory or (lambda role: llm), agent=self, depth=depth)
        self.messages: List[Dict[str, Any]] = []
        self.steps_last_turn = 0
        self.on_change: Optional[Callable[[], None]] = None
        acfg = config.get("agent") or {}
        self.max_steps = int(acfg.get("max_steps_per_turn", 40))
        self.compact_frac = float(acfg.get("compact_at_fraction", 0.75))
        self.keep_recent = int(acfg.get("keep_recent_messages", 8))
        self.max_tool_chars = int(acfg.get("max_tool_output_chars", 16000))
        self._git_info_cache: Optional[str] = None

    # ------------------------------------------------------- system prompt --
    def _git_info(self) -> str:
        if self._git_info_cache is None:
            try:
                r = subprocess.run(["git", "status", "--short", "--branch"], cwd=str(self.ctx.cwd),
                                   capture_output=True, timeout=10)
                out = r.stdout.decode("utf-8", "replace").strip().splitlines()
                self._git_info_cache = ("Git: %s (%d changed files)" % (out[0].lstrip("# "), len(out) - 1)) if r.returncode == 0 and out else "Git: not a repository"
            except Exception:
                self._git_info_cache = "Git: unavailable"
        return self._git_info_cache

    def system_prompt(self) -> str:
        argv = detect_shell((self.config.get("shell") or {}).get("program"))
        locs = self.config.get("locations") or {}
        srcs = [n for n, s in (self.config.get("search", "sources") or {}).items() if s.get("enabled", True)]
        env = ["# Environment",
               "Date: %s" % dt.date.today().isoformat(),
               "OS: %s %s" % (platform.system(), platform.release()),
               "Shell: %s (%s syntax)" % (os.path.basename(argv[0]), shell_kind(argv)),
               "Python: %s (%s)" % (sys.executable, platform.python_version()),
               "Working directory: %s" % self.ctx.cwd,
               "Project root: %s" % self.config.project_root,
               self._git_info(),
               "Model: %s" % self.llm.name]
        if locs:
            env.append("Network/file locations: " + ", ".join("%s=%s" % kv for kv in locs.items()))
        env.append("Intranet search sources: " + (", ".join(srcs) if srcs else "none configured"))
        parts = [CORE_PROMPT, "\n".join(env)]
        if self.permissions.mode == "plan":
            parts.append(PLAN_MODE)
        ctx_text, _ = load_context(self.config, self.ctx.cwd)
        if ctx_text:
            parts.append("# Context files (memory)\n" + ctx_text)
        if self.ctx.todos:
            parts.append("# Current todo list\n" + "\n".join("[%s] %s" % (t["status"], t["content"]) for t in self.ctx.todos))
        if self.extra_system:
            parts.append(self.extra_system)
        return "\n\n".join(parts)

    def tool_schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    # ----------------------------------------------------------- the loop --
    def run_turn(self, user_text: str, quiet: bool = False) -> str:
        self.messages.append({"role": "user", "content": user_text})
        self._git_info_cache = None
        final = ""
        seen_calls: Dict[str, int] = {}
        nudged_empty = False
        self.steps_last_turn = 0
        stream_cb = None if (quiet or self.depth) else self.ui.stream
        try:
            for step in range(self.max_steps):
                self.steps_last_turn = step + 1
                self.maybe_compact()
                msgs = [{"role": "system", "content": self.system_prompt()}] + self.messages
                self.ui.reset_stream()
                self.ui.start_thinking()
                streamed = [False]

                def cb(kind, chunk, _s=streamed):
                    if kind == "text":
                        _s[0] = True
                    stream_cb(kind, chunk)
                try:
                    resp = self.llm.chat(msgs, tools=self.tool_schemas(), stream_cb=cb if stream_cb else None)
                except LLMError as e:
                    self.ui.stop_thinking()
                    self.ui.error("Model error: %s" % e)
                    if "context" in str(e).lower() and "length" in str(e).lower() or "too long" in str(e).lower():
                        self.ui.warn("Context too long - compacting and retrying.")
                        self.compact(force=True)
                        continue
                    break
                self.ui.stop_thinking()
                if resp.reasoning:
                    self.ui.reasoning(resp.reasoning) if not streamed[0] else None
                asst: Dict[str, Any] = {"role": "assistant", "content": resp.content or ""}
                if resp.tool_calls:
                    asst["tool_calls"] = [tc.to_message_dict() for tc in resp.tool_calls]
                self.messages.append(asst)
                if resp.content.strip() and not quiet:
                    self.ui.assistant_message(resp.content, streamed[0])
                elif streamed[0]:
                    self.ui.assistant_message("", True)
                if not resp.tool_calls:
                    if not resp.content.strip() and not nudged_empty:
                        nudged_empty = True
                        self.messages.append({"role": "user", "content": "(Your last reply was empty. Continue the task: "
                                              "call a tool, or give your final answer.)"})
                        continue
                    if resp.finish_reason == "length" and step < self.max_steps - 1:
                        self.messages.append({"role": "user", "content": "(Your reply was cut off. Continue exactly where you stopped.)"})
                        final += resp.content
                        continue
                    final += resp.content
                    break
                for tc in resp.tool_calls:
                    sig = tc.name + json.dumps(tc.arguments, sort_keys=True)
                    seen_calls[sig] = seen_calls.get(sig, 0) + 1
                    result = self.execute_tool(tc)
                    if seen_calls[sig] >= 3:
                        result += ("\n\n[NOTE: you have made this exact call %d times this turn. Stop repeating it; "
                                   "try a different approach or explain what is blocking you.]" % seen_calls[sig])
                    self.messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": result})
            else:
                self.ui.warn("Stopped after %d steps (max_steps_per_turn). Say 'continue' to keep going." % self.max_steps)
        except KeyboardInterrupt:
            self.ui.stop_thinking()
            self.ui.warn("\nInterrupted.")
            self._repair_after_interrupt()
            self.messages.append({"role": "user", "content": "[The user interrupted the previous action.]"})
            self.messages.append({"role": "assistant", "content": "Stopped. What would you like me to do?"})
        if self.on_change:
            self.on_change()
        return final

    def _repair_after_interrupt(self):
        """Ensure every tool_call has a tool result (APIs reject dangling calls)."""
        if not self.messages:
            return
        last_asst = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]["role"] == "assistant":
                last_asst = i
                break
        if last_asst is None:
            return
        answered = {m.get("tool_call_id") for m in self.messages[last_asst + 1:] if m["role"] == "tool"}
        for tc in self.messages[last_asst].get("tool_calls") or []:
            if tc["id"] not in answered:
                self.messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"],
                                      "content": "[interrupted by user before completion]"})

    def execute_tool(self, tc) -> str:
        tool = self.tools.get(tc.name)
        if tool is None:
            # tolerate a few common aliases from models trained on other agents
            alias = {"bash": "shell", "Bash": "shell", "Read": "read_file", "Write": "write_file",
                     "Edit": "edit_file", "ls": "list_dir", "LS": "list_dir", "Glob": "glob", "Grep": "grep",
                     "python": "run_python", "TodoWrite": "todo_write", "web_search": "intranet_search",
                     "search": "intranet_search", "WebFetch": "fetch_url", "Task": "task"}.get(tc.name)
            tool = self.tools.get(alias) if alias else None
            if tool is None:
                msg = "Unknown tool '%s'. Available tools: %s" % (tc.name, ", ".join(self.tools))
                self.ui.tool_start(tc.name)
                self.ui.tool_result(msg, ok=False)
                return "Error: " + msg
        if tc.parse_error:
            self.ui.tool_start(tool.name)
            self.ui.tool_result(tc.parse_error, ok=False)
            return "Error: %s. Re-issue the call with valid JSON arguments." % tc.parse_error
        args = dict(tc.arguments)
        err = validate_args(tool, args)
        self.ui.tool_start(tool.describe_call(args))
        if err:
            self.ui.tool_result(err, ok=False)
            return "Error: " + err
        allowed, msg = self.permissions.check(tool, args, self.ctx)
        if not allowed:
            self.ui.tool_result(msg, ok=False)
            return msg
        hook_msg = self._run_hooks("pre_tool", tool.name, args)
        if hook_msg is not None:
            self.ui.tool_result(hook_msg, ok=False)
            return "Blocked by pre_tool hook: " + hook_msg
        t0 = time.time()
        try:
            out = tool.run(args, self.ctx)
            ok = True
        except ToolError as e:
            out, ok = "Error: %s" % e, False
        except KeyboardInterrupt:
            raise
        except Exception as e:  # never let a tool crash the session
            out, ok = "Error: %s: %s" % (type(e).__name__, e), False
        out = out if isinstance(out, str) else json.dumps(out)
        limit = min(self.max_tool_chars, int(self.llm.context_tokens * 3.5 * 0.3))
        out = truncate(out, limit, "narrow your query or use offset/limit")
        self.ui.tool_result(out + ("" if time.time() - t0 < 5 else "\n(%.0fs)" % (time.time() - t0)), ok)
        self._run_hooks("post_tool", tool.name, args)
        return out

    def _run_hooks(self, kind: str, name: str, args: Dict[str, Any]) -> Optional[str]:
        for h in (self.config.get("hooks", kind) or []):
            if not re.fullmatch(h.get("match", ".*"), name):
                continue
            env = dict(os.environ, HSA_TOOL=name, HSA_TOOL_INPUT=json.dumps(args)[:30000])
            try:
                r = subprocess.run(h["command"], shell=True, cwd=str(self.ctx.cwd), env=env,
                                   capture_output=True, timeout=int(h.get("timeout", 60)))
            except Exception as e:
                self.ui.warn("hook failed: %s" % e)
                continue
            if kind == "pre_tool" and r.returncode != 0:
                return (r.stdout + r.stderr).decode("utf-8", "replace").strip() or "hook exited %d" % r.returncode
        return None

    # --------------------------------------------------------- compaction --
    def context_usage(self) -> int:
        return estimate_tokens(self.system_prompt()) + estimate_tokens(self.messages) + \
            estimate_tokens(json.dumps(self.tool_schemas()))

    def maybe_compact(self):
        limit = self.llm.context_tokens - self.llm.max_output_tokens
        if self.context_usage() < limit * self.compact_frac:
            return
        # stage 1: shrink old tool outputs (cheap, no model call)
        cut = max(0, len(self.messages) - self.keep_recent)
        shrunk = False
        for m in self.messages[:cut]:
            if m["role"] == "tool" and len(m.get("content") or "") > 600:
                m["content"] = m["content"][:400] + "\n...[old tool output trimmed to save context]"
                shrunk = True
        if shrunk and self.context_usage() < limit * self.compact_frac:
            self.ui.info("(trimmed old tool outputs to save context)")
            return
        self.compact()

    def compact(self, instructions: str = "", force: bool = False):
        if len(self.messages) <= 2:
            return
        # find a cut point at a user message so tool call/result pairs stay together
        keep_from = max(1, len(self.messages) - self.keep_recent)
        while keep_from < len(self.messages) and self.messages[keep_from]["role"] != "user":
            keep_from += 1
        if keep_from >= len(self.messages):
            keep_from = len(self.messages) - 1
            while keep_from > 0 and self.messages[keep_from]["role"] != "user":
                keep_from -= 1
        old, recent = self.messages[:keep_from], self.messages[keep_from:]
        if not old:
            if not force:
                return
            old, recent = self.messages[:-1], self.messages[-1:]
        self.ui.info("Compacting conversation (%d messages)..." % len(old))
        transcript = []
        for m in old:
            c = m.get("content") or ""
            if m["role"] == "tool":
                c = c[:1500]
            line = "%s: %s" % (m["role"].upper(), c)
            for tc in m.get("tool_calls") or []:
                line += "\n  -> called %s(%s)" % (tc["function"]["name"], str(tc["function"]["arguments"])[:300])
            transcript.append(line)
        text = "\n\n".join(transcript)
        budget = int(self.llm.context_tokens * 3.5 * 0.6)
        if len(text) > budget:
            text = text[: budget // 3] + "\n\n...[middle omitted]...\n\n" + text[-(2 * budget // 3):]
        prompt = """Summarize this conversation between a user and a coding/research agent so the agent can continue
seamlessly. Include:
1. The user's goals and explicit requests (verbatim when important)
2. Key decisions, constraints and user preferences
3. Files read/created/modified (exact paths) and what changed
4. Commands run and important results/errors
5. Research findings and source URLs
6. Current state: what is done, what remains (next steps)
%s
CONVERSATION:
%s""" % (("Additional instructions: " + instructions) if instructions else "", text)
        try:
            summary = self.llm.complete(prompt, max_tokens=min(2000, self.llm.max_output_tokens))
        except Exception as e:
            self.ui.error("Compaction failed (%s); dropping oldest messages instead." % e)
            summary = "(Earlier conversation was dropped due to context limits.)"
        self.messages = [{"role": "user", "content": "[Summary of earlier conversation]\n" + summary},
                         {"role": "assistant", "content": "Understood. I have the context from the summary and will continue."}] + recent
        self.ui.info("Compacted to ~%d tokens." % self.context_usage())
