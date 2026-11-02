"""Sub-agents: delegate a self-contained task to a fresh agent with its own
context window, and get back only its final report. Keeps the main
conversation small - especially valuable with small-context local models."""
from __future__ import annotations

from hsa.tools.base import Tool, ToolError

READ_ONLY_TOOLS = {"read_file", "list_dir", "glob", "grep", "find_files", "intranet_search",
                   "fetch_url", "doc_search", "todo_write"}

AGENT_TYPES = {
    "explore": ("Read-only investigator: find files, read code/docs, search the intranet, and report "
                "findings with exact paths/URLs. Never modifies anything.", READ_ONLY_TOOLS),
    "general": ("General-purpose agent that can read, write, run commands, and search.", None),
}


class Task(Tool):
    name = "task"
    description = """Launch a sub-agent with a FRESH context to handle a self-contained task, and receive
its final report. Use it to keep your own context small: e.g. "find where X is configured
across the network share", "search the intranet for Y and summarise", "investigate why
test Z fails". The sub-agent cannot see this conversation, so put ALL needed details in
`prompt` and say exactly what to return.
agent_type: "explore" (read-only, safe) or "general" (can edit files and run commands)."""
    parameters = {"type": "object", "properties": {
        "description": {"type": "string", "description": "3-6 word label"},
        "prompt": {"type": "string", "description": "Complete, self-contained instructions"},
        "agent_type": {"type": "string", "enum": list(AGENT_TYPES)},
    }, "required": ["prompt"]}

    def describe_call(self, args):
        return "task[%s]: %s" % (args.get("agent_type") or "explore", args.get("description") or args["prompt"][:80])

    def run(self, args, ctx):
        from hsa.agent import Agent
        if ctx.depth >= 2:
            raise ToolError("Sub-agents cannot spawn further sub-agents at this depth. Do the work directly.")
        kind = args.get("agent_type") or "explore"
        role, allowed = AGENT_TYPES.get(kind, AGENT_TYPES["explore"])
        parent = ctx.agent
        child = Agent(parent.config, parent.llm, parent.ui, parent.permissions, cwd=ctx.cwd,
                      llm_factory=ctx.llm_factory, depth=ctx.depth + 1,
                      tool_filter=(lambda n: n in allowed) if allowed else (lambda n: n != "task"),
                      extra_system="You are a sub-agent. %s When finished, reply with a concise but complete "
                                   "final report of what you found/did, including exact file paths, URLs and "
                                   "commands. Your final message is all the caller will see." % role)
        ctx.ui.info("  ↳ sub-agent (%s) started: %s" % (kind, args.get("description") or ""))
        result = child.run_turn(args["prompt"], quiet=True)
        ctx.ui.info("  ↳ sub-agent finished (%d steps)" % child.steps_last_turn)
        return result or "(sub-agent returned no text)"


TOOLS = [Task]
