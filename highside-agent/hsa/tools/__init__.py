"""Tool registry."""
from __future__ import annotations

from typing import Dict, List

from hsa.tools.base import Tool, ToolContext, ToolError  # noqa: F401


def build_tools(config) -> Dict[str, Tool]:
    from hsa import context
    from hsa.tools import control, files, gitlab, research_tools, shell, subagent
    tools: List[Tool] = []
    for cls in files.TOOLS + context.TOOLS + research_tools.TOOLS + gitlab.TOOLS + subagent.TOOLS + control.TOOLS:
        tools.append(cls())
    tools += shell.make_tools(config)
    disabled = set(config.get("agent", "disabled_tools", default=[]) or [])
    return {t.name: t for t in tools if t.name not in disabled}
