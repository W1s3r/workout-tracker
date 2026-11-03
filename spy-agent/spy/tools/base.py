"""Tool base class, shared context, and argument validation."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class ToolError(Exception):
    """Raised by a tool to report a user/model-facing error (not a crash)."""


@dataclass
class ToolContext:
    config: Any                      # spy.config.Config
    ui: Any                          # spy.ui.UI
    cwd: Path
    llm_factory: Callable[[str], Any] = None  # role/model name -> LLMClient
    read_files: Dict[str, float] = field(default_factory=dict)  # path -> mtime when read
    todos: List[Dict[str, str]] = field(default_factory=list)
    bg_jobs: Dict[str, Any] = field(default_factory=dict)
    agent: Any = None                # the owning Agent (for sub-agents)
    history: Any = None              # edit_safety.FileHistory - backups for /undo
    pending_images: List[str] = field(default_factory=list)  # images a tool wants the model to see
    depth: int = 0                   # sub-agent nesting depth

    def __post_init__(self):
        if self.history is None:
            from spy.tools.edit_safety import FileHistory
            self.history = FileHistory()

    def resolve(self, p: str) -> Path:
        p = os.path.expandvars(os.path.expanduser(str(p).strip().strip('"').strip("'")))
        path = Path(p)
        if not path.is_absolute():
            path = self.cwd / path
        return path


class Tool:
    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    read_only: bool = True           # read-only tools never need permission
    is_edit: bool = False            # auto-approved in accept_edits mode

    def schema(self) -> Dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description.strip(),
                             "parameters": self.parameters}}

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        raise NotImplementedError

    def needs_permission(self, args: Dict[str, Any]) -> bool:
        return not self.read_only

    def force_confirm(self, args: Dict[str, Any], ctx: "ToolContext") -> str:
        """Return a reason to ALWAYS ask the user (even in auto/accept_edits mode), or ''."""
        return ""

    def permission_key(self, args: Dict[str, Any]) -> str:
        """String matched against allow/deny rules, e.g. 'bash(git status)'."""
        return "%s(%s)" % (self.name, args.get("path") or args.get("command") or "")

    def describe_call(self, args: Dict[str, Any]) -> str:
        """One-line summary shown in the UI."""
        main = args.get("path") or args.get("command") or args.get("pattern") or \
            args.get("query") or args.get("url") or ""
        return "%s(%s)" % (self.name, str(main)[:120])


def validate_args(tool: Tool, args: Dict[str, Any]) -> Optional[str]:
    """Light JSON-schema check; returns an error string or None. Also coerces
    common small-model mistakes (numbers as strings, 'true' as string)."""
    props = tool.parameters.get("properties", {})
    missing = [r for r in tool.parameters.get("required", []) if r not in args or args[r] is None]
    if missing:
        return "Missing required parameter(s): %s. Expected parameters: %s" % (
            ", ".join(missing), ", ".join("%s%s" % (k, "*" if k in tool.parameters.get("required", []) else "")
                                          for k in props))
    for k in list(args):
        if k not in props:
            # tolerate unknown keys silently - small models add extras
            continue
        typ = props[k].get("type")
        v = args[k]
        try:
            if typ == "integer" and not isinstance(v, int):
                args[k] = int(float(v))
            elif typ == "number" and not isinstance(v, (int, float)):
                args[k] = float(v)
            elif typ == "boolean" and not isinstance(v, bool):
                args[k] = str(v).lower() in ("1", "true", "yes", "y")
            elif typ == "string" and not isinstance(v, str):
                args[k] = v if v is None else (str(v) if not isinstance(v, (list, dict)) else __import__("json").dumps(v))
            elif typ == "array" and isinstance(v, str):
                import json
                try:
                    args[k] = json.loads(v)
                except Exception:
                    args[k] = [s.strip() for s in v.split(",") if s.strip()]
        except (TypeError, ValueError):
            return "Parameter '%s' should be of type %s, got %r" % (k, typ, v)
        if "enum" in props[k] and args[k] not in props[k]["enum"]:
            return "Parameter '%s' must be one of %s, got %r" % (k, props[k]["enum"], args[k])
    return None


def truncate(text: str, limit: int, note: str = "") -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    return "%s\n\n... [%d characters truncated%s] ...\n\n%s" % (
        head, len(text) - len(head) - len(tail), ("; " + note) if note else "", tail)


SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             ".mypy_cache", ".pytest_cache", ".idea", ".tox", "dist", "build", ".ipynb_checkpoints"}
