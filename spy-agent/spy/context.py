"""Persistent context files (SPY.md - Spy's equivalent of CLAUDE.md) and memory updates.

Load order (all that exist are concatenated into the system prompt):
  1. ~/.spy/SPY.md                      MASTER: applies to every project, always loaded
  2. SPY.md in every directory from the project root down to cwd   (project-specific)
  3. <project>/.spy/SPY.local.md        personal notes for this project, not committed
The file name is matched case-insensitively (SPY.md, spy.md, Spy.md all work).
A line "@relative/or/absolute/path.md" inside a context file imports that file.

The agent updates these files itself through the `memory` tool ("reflexive"
context), and you can add a note from the prompt by starting a line with '#'.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import List, Optional, Tuple

from spy.tools.base import Tool, ToolError

GLOBAL_TEMPLATE = """# Master SPY.md
<!-- Loaded into EVERY Spy session in every project. Spy may append to it via the memory tool. -->

## About me / preferences

## Environment facts
<!-- e.g. where network shares live, internal hostnames (placeholders ok), tool quirks -->

## Lessons learned
"""

PROJECT_TEMPLATE = """# Project SPY.md
<!-- Loaded when Spy runs in this project (on top of the master ~/.spy/SPY.md). Commit it so teammates benefit. -->

## Overview

## Commands
<!-- build / test / run commands -->

## Conventions

## Lessons learned
"""

MAX_IMPORT_DEPTH = 3


def _expand_imports(text: str, base: Path, depth: int = 0) -> str:
    if depth >= MAX_IMPORT_DEPTH:
        return text

    def repl(m):
        p = Path(m.group(1)).expanduser()
        if not p.is_absolute():
            p = base / p
        try:
            return "<!-- imported %s -->\n%s" % (p, _expand_imports(p.read_text(encoding="utf-8"), p.parent, depth + 1))
        except OSError:
            return m.group(0)
    return re.sub(r"(?m)^@(\S+\.(?:md|txt))\s*$", repl, text)


def _find_named(d: Path, names: List[str]) -> Optional[Path]:
    """Case-insensitive lookup of a context file in directory d."""
    try:
        entries = {e.lower(): e for e in __import__("os").listdir(d)}
    except OSError:
        return None
    for n in names:
        hit = entries.get(n.lower())
        if hit and (d / hit).is_file():
            return d / hit
    return None


def context_files(config, cwd: Path) -> List[Path]:
    names = config.get("agent", "context_file_names", default=["SPY.md"])
    files: List[Path] = []
    g = _find_named(config.home, names)
    if g:
        files.append(g)
    root = config.project_root
    try:
        rel_parts = cwd.resolve().relative_to(root.resolve()).parts
    except ValueError:
        rel_parts = ()
    dirs = [root] + [root.joinpath(*rel_parts[:i + 1]) for i in range(len(rel_parts))]
    for d in dirs:
        f = _find_named(d, names)
        if f and f not in files:
            files.append(f)
    local = root / ".spy" / "SPY.local.md"
    if local.is_file():
        files.append(local)
    return files


def load_context(config, cwd: Path) -> Tuple[str, List[Path]]:
    files = context_files(config, cwd)
    home = config.home.resolve()
    parts = []
    for f in files:
        try:
            body = _expand_imports(f.read_text(encoding="utf-8"), f.parent).strip()
        except OSError:
            continue
        if not body:
            continue
        if f.resolve().parent == home:
            scope = "master"
        elif f.name.lower().endswith(".local.md"):
            scope = "project-local"
        else:
            scope = "project"
        parts.append("<context scope=\"%s\" file=\"%s\">\n%s\n</context>" % (scope, f, body))
    if not parts:
        return "", files
    header = ("How to apply these files: the MASTER file holds the user's standing rules and preferences and "
              "applies everywhere. PROJECT files add instructions for this project (deeper folders are more "
              "specific). Follow all of them; if they conflict, the more specific file wins for matters of "
              "this project, but never break a master rule the user marked as always/never.")
    return header + "\n\n" + "\n\n".join(parts), files


def memory_path(config, scope: str) -> Path:
    names = config.get("agent", "context_file_names", default=["SPY.md"])
    if scope in ("global", "master"):
        return _find_named(config.home, names) or config.home / "SPY.md"
    if scope == "local":
        return config.project_root / ".spy" / "SPY.local.md"
    return _find_named(config.project_root, names) or config.project_root / "SPY.md"


def ensure_memory_file(config, scope: str) -> Path:
    p = memory_path(config, scope)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(GLOBAL_TEMPLATE if scope == "global" else PROJECT_TEMPLATE, encoding="utf-8")
    return p


def add_note(config, scope: str, note: str, section: Optional[str] = None) -> Path:
    """Append a bullet under `section` (created if missing)."""
    p = ensure_memory_file(config, scope)
    text = p.read_text(encoding="utf-8")
    note = note.strip()
    if not note.startswith("- "):
        note = "- " + note
    section = (section or "Lessons learned").strip().lstrip("#").strip()
    lines = text.rstrip("\n").split("\n")
    idx = None
    for i, l in enumerate(lines):
        if re.match(r"^#{1,6}\s+%s\s*$" % re.escape(section), l, re.I):
            idx = i
            break
    if idx is None:
        lines += ["", "## " + section, note]
    else:
        j = idx + 1
        while j < len(lines) and not re.match(r"^#{1,6}\s", lines[j]):
            j += 1
        while j > idx + 1 and not lines[j - 1].strip():
            j -= 1
        lines.insert(j, note)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class Memory(Tool):
    name = "memory"
    description = """Read or update your persistent memory files (SPY.md), which are loaded into
every future session. Use it PROACTIVELY when you learn something durable:
the user corrects you or states a preference, you discover a build/test command,
a file location, a network share path, a gotcha, or a project convention.
Keep notes short, factual and specific. Don't store secrets or one-off details.
scope: "project" (this repo/folder; shared), "global" (all projects), "local" (this project, private).
action: "view" | "add" (append a bullet under a section) | "replace" (exact text) | "remove" (exact text)."""
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["view", "add", "replace", "remove"]},
        "scope": {"type": "string", "enum": ["project", "global", "local"]},
        "note": {"type": "string", "description": "For add: the note text"},
        "section": {"type": "string", "description": "For add: section heading, e.g. 'Commands', 'Lessons learned'"},
        "old_text": {"type": "string", "description": "For replace/remove: exact existing text"},
        "new_text": {"type": "string", "description": "For replace: new text"},
    }, "required": ["action"]}
    read_only = False
    is_edit = True

    def permission_key(self, args):
        return "memory(%s)" % args.get("scope", "project")

    def describe_call(self, args):
        return "memory %s [%s] %s" % (args.get("action"), args.get("scope", "project"),
                                      (args.get("note") or args.get("old_text") or "")[:100])

    def run(self, args, ctx):
        scope = args.get("scope") or "project"
        action = args["action"]
        if action == "view":
            p = memory_path(ctx.config, scope)
            return p.read_text(encoding="utf-8") if p.exists() else "(%s memory file %s does not exist yet)" % (scope, p)
        if action == "add":
            if not args.get("note"):
                raise ToolError("note is required for add")
            p = add_note(ctx.config, scope, args["note"], args.get("section"))
            return "Saved to %s memory (%s)." % (scope, p)
        p = memory_path(ctx.config, scope)
        if not p.exists():
            raise ToolError("Memory file %s does not exist." % p)
        text = p.read_text(encoding="utf-8")
        old = args.get("old_text") or ""
        if not old or old not in text:
            raise ToolError("old_text not found in %s. View it first." % p)
        new = args.get("new_text", "") if action == "replace" else ""
        text = text.replace(old, new, 1)
        if action == "remove":
            text = re.sub(r"\n{3,}", "\n\n", text)
        p.write_text(text, encoding="utf-8")
        return "Updated %s." % p


class TodoWrite(Tool):
    name = "todo_write"
    description = """Create/update your task list for multi-step work. Send the FULL list each time.
Each item: {"content": "...", "status": "pending"|"in_progress"|"completed"}.
Use it for any task with 3+ steps; keep exactly one item in_progress; mark items
completed immediately when done."""
    parameters = {"type": "object", "properties": {
        "todos": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
            "required": ["content", "status"]}}}, "required": ["todos"]}

    def describe_call(self, args):
        return "todo_write (%d items)" % len(args.get("todos") or [])

    def run(self, args, ctx):
        todos = []
        for t in args.get("todos") or []:
            if isinstance(t, str):
                t = {"content": t, "status": "pending"}
            todos.append({"content": str(t.get("content", "")),
                          "status": t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"})
        ctx.todos[:] = todos
        ctx.ui.show_todos(todos)
        return "Todo list updated (%d items). Continue with the in_progress item." % len(todos)


class AskUser(Tool):
    name = "ask_user"
    description = """Ask the user a clarifying question and wait for the answer. Only use when you
are genuinely blocked on a decision the user must make. Optionally provide options."""
    parameters = {"type": "object", "properties": {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}}}, "required": ["question"]}

    def run(self, args, ctx):
        return "User answered: " + ctx.ui.ask(args["question"], args.get("options") or [])


def stamp() -> str:
    return _dt.date.today().isoformat()


TOOLS = [Memory, TodoWrite, AskUser]
