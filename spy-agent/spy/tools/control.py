"""Turn-control tool: an explicit, unambiguous way for the model to end a task."""
from __future__ import annotations

from spy.tools.base import Tool


class Finish(Tool):
    name = "finish"
    description = """End your turn and report to the user. Call this when the task is complete, or when you are
blocked and need something from the user. `summary` is shown to the user as your final message: say what you
did, the results (paths, outputs), and anything left undone. Do not call it before the work is actually done."""
    parameters = {"type": "object", "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "needs_input"]},
        "summary": {"type": "string", "description": "Final message to the user (markdown)"},
    }, "required": ["summary"]}

    def describe_call(self, args):
        return "finish (%s)" % (args.get("status") or "done")

    def run(self, args, ctx):
        return "Turn ended."


TOOLS = [Finish]
