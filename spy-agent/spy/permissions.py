"""Permission checks for tool calls.

Modes:
  default       ask before anything that changes state (edits, shell, writes)
  accept_edits  file edits inside the project are auto-approved; shell still asks
  auto          everything approved except explicit deny rules  (use with care)
  plan          read-only: the agent may investigate and propose a plan only

Rules are glob patterns over a call's permission key, e.g. "shell(git status*)",
"write_file(*.md)", "gitlab(create_mr)". deny beats allow.
"""
from __future__ import annotations

import fnmatch
from typing import List, Tuple

MODES = ["default", "accept_edits", "auto", "plan"]


class Permissions:
    def __init__(self, config, ui):
        p = config.get("permissions") or {}
        self.mode = p.get("mode", "default")
        if self.mode not in MODES:
            self.mode = "default"
        self.allow: List[str] = list(p.get("allow") or [])
        self.deny: List[str] = list(p.get("deny") or [])
        self.session_allow: List[str] = []
        self.ui = ui
        self.project_root = str(config.project_root)

    @staticmethod
    def _match(key: str, rules: List[str]) -> bool:
        k = key.lower()
        return any(fnmatch.fnmatchcase(k, r.lower()) for r in rules)

    def check(self, tool, args, ctx) -> Tuple[bool, str]:
        """Returns (allowed, message_for_model_if_denied)."""
        key = tool.permission_key(args)
        if self._match(key, self.deny):
            return False, "Blocked by a deny rule (%s). Do not retry this; choose another approach or ask the user." % key
        if not tool.needs_permission(args):
            return True, ""
        if self.mode == "plan":
            return False, ("Plan mode is active: you may only read and investigate. Present your plan "
                           "to the user instead of making changes.")
        danger = tool.force_confirm(args, ctx)
        if danger:  # large deletions are always confirmed, whatever the mode or allow rules
            self.ui.warn("⚠ " + danger)
            choice, feedback = self.ui.confirm_tool(tool, args)
            if choice in ("yes", "always"):
                return True, ""
            return False, ("The user rejected this change (%s)%s Make a precise edit with edit_file instead."
                           % (danger, (" Feedback: " + feedback + ".") if feedback else "."))
        if self.mode == "auto":
            return True, ""
        if self._match(key, self.allow + self.session_allow):
            return True, ""
        if self.mode == "accept_edits" and tool.is_edit:
            path = str(ctx.resolve(args.get("path", ""))) if args.get("path") else ""
            if not path or path.startswith(self.project_root):
                return True, ""
        choice, feedback = self.ui.confirm_tool(tool, args)
        if choice == "always":
            self.session_allow.append(self._always_rule(tool, args))
            return True, ""
        if choice == "yes":
            return True, ""
        msg = "The user denied this tool call."
        if feedback:
            msg += " User feedback: " + feedback
        return False, msg + " Do not retry the same call; adjust based on the feedback or ask the user."

    @staticmethod
    def _always_rule(tool, args) -> str:
        if tool.name == "shell":
            words = (args.get("command") or "").split()
            prefix = " ".join(words[:2]) if words[:1] in (["git"], ["python"], ["npm"], ["pip"], ["conda"]) else " ".join(words[:1])
            return "shell(%s*)" % prefix
        return "%s(*)" % tool.name
