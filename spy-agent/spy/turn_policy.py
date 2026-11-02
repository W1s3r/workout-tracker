"""Turn policy: decides whether a user message is chat or a task, and - when
the model stops calling tools - whether the turn is really finished or the
model should be told to keep going.

Why this exists: the naive loop "run tools until the model replies with plain
text" fails in predictable ways with small models:
  * "I'll now update the config file."  <- announces work, then stops
  * stops with todo items still pending
  * edits code and declares victory without running it
  * asks "Shall I proceed?" after the user already said to do it
  * calls tools for "hi" / "thanks"
  * flails on the same error forever
Each check below targets one of those, with a hard cap on automatic
continuations so the loop can never spin forever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

CHAT = "chat"
TASK = "task"
AUTO = "auto"

_GREETING = re.compile(
    r"^\s*(hi|hello|hey|yo|hiya|howdy|thanks|thank you|thx|ty|cheers|good (morning|afternoon|evening|night)|"
    r"bye|goodbye|see ya|lol|haha|nice|awesome|great|cool|perfect)"
    r"(\s+(there|again|so much|a lot|man|buddy|spy))?[\s!.?,:)]*$", re.I)
# Short replies that are usually ANSWERS to something the agent asked
_ACK = re.compile(r"^\s*(yes|yeah|yep|yup|y|no|nope|n|ok|okay|k|sure|sounds good|got it|correct|right|"
                  r"the (first|second|third|last) one|both|neither|either|option \w+|\d+)[\s!.?,]*$", re.I)
_CONTINUE = re.compile(r"^\s*(continue|keep going|go ahead|go on|proceed|do it|carry on|resume|try again|retry|"
                       r"go|next|finish (it|up)|yes,? (please|go ahead|do it|proceed|continue))\b", re.I)

_ACTION = re.compile(
    r"^\s*(please\s+|can you\s+|could you\s+|would you\s+|i need you to\s+|i want you to\s+|go ahead and\s+|now\s+)?"
    r"(create|write|fix|edit|update|change|modify|add|remove|delete|rename|move|copy|run|execute|build|test|install|"
    r"deploy|commit|push|pull|refactor|implement|find|search|look\s*up|locate|read|open|list|check|analy[sz]e|"
    r"review|summari[sz]e|generate|make|convert|research|investigate|debug|clone|merge|download|fetch|compare|"
    r"organi[sz]e|clean\s*up|set\s*up|configure|scan|index|draft|extract|parse|count|grep|plot|calculate|compute)\b",
    re.I)
_PATHISH = re.compile(r"(`[^`]+`|\b[\w.-]+\.(py|ps1|bat|sh|js|ts|json|md|txt|csv|xlsx?|docx?|pptx?|pdf|ya?ml|ini|cfg|sql|log|html?)\b"
                      r"|[A-Za-z]:\\|\\\\[\w.-]+\\|(^|\s)\.{0,2}/[\w.-]+/)", re.I)


def route(text: str, prev_assistant: str = "", prev_engaged: bool = False) -> str:
    """CHAT: a purely social message (answered without tools).
    TASK: clearly asks the agent to do something, or continues/answers an ongoing task.
    AUTO: let the model decide whether tools are needed.
    prev_assistant / prev_engaged: the agent's last message and whether the last turn used tools -
    "yes" after "Shall I delete them?" is an instruction, not small talk."""
    t = (text or "").strip()
    if not t:
        return CHAT
    if _CONTINUE.match(t):
        return TASK
    if len(t) < 40 and _ACK.match(t):
        return TASK if (prev_engaged or asks_user(prev_assistant)) else AUTO
    if len(t) < 60 and _GREETING.match(t):
        return CHAT
    if _ACTION.match(t) or _PATHISH.search(t):
        return TASK
    return AUTO


# --- analysing a text-only reply ---------------------------------------------

_INTENT = re.compile(
    r"(\b(i'll|i will|i am going to|i'm going to|let me|let's|i need to|i should|i can now|i'll now|i will now|"
    r"next,? i|now,? i|first,? i|then,? i|proceeding to|now proceeding|moving on to|starting with)\b[^.?!\n]*"
    r"[.:!]?\s*$)|(:\s*$)", re.I)
_ASK_PERMISSION = re.compile(
    r"(shall i|should i|do you want me to|would you like me to|want me to|may i|can i|ok to|okay to)\s+"
    r"(proceed|continue|go ahead|start|begin|do (that|this|it)|make (the|these|those) changes|apply|implement|"
    r"run|create|write|fix|update)\b[^?]*\?\s*$", re.I)
_QUESTION_TO_USER = re.compile(
    r"(\?\s*$)|(\b(let me know|please (provide|confirm|clarify|specify|tell me|share|send)|could you (provide|confirm|clarify|tell)|"
    r"which (one|option|file|version) (do|would|should)|what (would|do) you (like|want|prefer))\b)", re.I)
_BLOCKED = re.compile(
    r"\b(i (can't|cannot|am unable to|was unable to|wasn't able to|couldn't|could not)|unable to (access|find|read|reach|connect)|"
    r"(don't|do not) have (access|permission)|permission denied|access (is )?denied|blocked|not (found|accessible|reachable))\b",
    re.I)


def _tail(text: str, n: int = 400) -> str:
    t = (text or "").strip()
    # ignore trailing code blocks when judging how a message ends
    t = re.sub(r"```.*?```\s*$", "", t, flags=re.S).strip() or t
    return t[-n:]


def announces_without_acting(text: str) -> bool:
    """True if the message ends by announcing an action it didn't take."""
    tail = _tail(text, 250)
    if not tail:
        return False
    if _QUESTION_TO_USER.search(tail):
        return False
    last = re.split(r"(?<=[.!?])\s+|\n", tail)
    last = [s for s in last if s.strip()]
    return bool(last) and bool(_INTENT.search(last[-1]))


def asks_permission_to_proceed(text: str) -> bool:
    return bool(_ASK_PERMISSION.search(_tail(text, 300)))


def asks_user(text: str) -> bool:
    return bool(_QUESTION_TO_USER.search(_tail(text, 300)))


def reports_blocked(text: str) -> bool:
    return bool(_BLOCKED.search(_tail(text, 600)))


CODE_EXTS = (".py", ".ps1", ".psm1", ".sh", ".bat", ".cmd", ".js", ".ts", ".java", ".c", ".cpp", ".cs", ".go",
             ".rb", ".r", ".sql", ".pl", ".lua", ".vbs")
RUN_TOOLS = {"shell", "run_python"}
EDIT_TOOLS = {"write_file", "edit_file"}


@dataclass
class TurnState:
    route: str = AUTO
    engaged: bool = False                    # the model has started using tools this turn
    steps: int = 0
    nudges: List[str] = field(default_factory=list)
    nudge_counts: Dict[str, int] = field(default_factory=dict)
    tools_used: List[str] = field(default_factory=list)
    code_edited_since_run: List[str] = field(default_factory=list)
    consecutive_errors: int = 0
    ended_by: str = ""
    finish_status: Optional[str] = None

    def record_tool(self, name: str, args: dict, ok: bool):
        self.engaged = True
        self.tools_used.append(name)
        self.consecutive_errors = 0 if ok else self.consecutive_errors + 1
        if ok and name in EDIT_TOOLS:
            p = str(args.get("path", ""))
            if p.lower().endswith(CODE_EXTS) and p not in self.code_edited_since_run:
                self.code_edited_since_run.append(p)
        if ok and name in RUN_TOOLS:
            self.code_edited_since_run = []

    def can_nudge(self, kind: str, per_kind: int, total: int) -> bool:
        return self.nudge_counts.get(kind, 0) < per_kind and len(self.nudges) < total

    def add_nudge(self, kind: str):
        self.nudges.append(kind)
        self.nudge_counts[kind] = self.nudge_counts.get(kind, 0) + 1


NUDGES = {
    "empty": "(Your last reply was empty. Continue the task: call a tool, or give your final answer.)",
    "announced": ("(You said what you will do next but did not do it. Do it now by calling the appropriate tool. "
                  "Do not describe the action again - perform it.)"),
    "permission": ("(The user already asked you to do this. Proceed without asking for confirmation - the "
                   "permission system will ask the user if an action needs approval.)"),
    "todos": ("(Your todo list still has unfinished items:\n{items}\nContinue working on them. If you are blocked "
              "and cannot continue, explain exactly what is blocking you and what you need from the user.)"),
    "verify": ("(You changed {files} but have not run or tested it since. If it can be run or tested safely, do that "
               "now and fix any problems. If it cannot be tested here, give your final answer and say it is untested.)"),
    "finish": ("(If the task is complete, call the finish tool with a summary. If you are blocked, call finish with "
               "status \"blocked\" and explain what you need. Otherwise continue working with tools.)"),
    "truncated": "(Your reply was cut off. Continue exactly where you stopped.)",
}

STUCK_NOTE = ("\n\n[NOTE: the last {n} tool calls failed. Stop and think: re-read the error messages, check your "
              "assumptions (paths, names, syntax), and try a DIFFERENT approach. If you cannot make progress, stop "
              "and explain to the user what is blocking you.]")


def decide_after_text(state: TurnState, text: str, finish_reason: str, todos: List[Dict[str, str]],
                      cfg: Dict) -> Optional[str]:
    """Model replied without tool calls. Return a nudge message to auto-continue,
    or None if the turn should end here."""
    per = int(cfg.get("max_nudges_per_reason", 2))
    total = int(cfg.get("max_auto_continue", 5))
    completion = cfg.get("completion", "text")

    def nudge(kind, **fmt):
        if state.can_nudge(kind, 1 if kind == "verify" else per, total):
            state.add_nudge(kind)
            return NUDGES[kind].format(**fmt)
        return None

    if finish_reason == "length":
        return nudge("truncated")
    if not (text or "").strip():
        return nudge("empty")
    if state.route == CHAT:
        return None
    task_like = state.engaged or state.route == TASK
    if task_like and asks_permission_to_proceed(text):
        return nudge("permission")
    if announces_without_acting(text):
        return nudge("announced")
    if not state.engaged:
        return None  # a direct answer to a question - done
    if asks_user(text) or reports_blocked(text):
        return None  # waiting for the user / genuinely blocked
    open_items = [t for t in todos if t.get("status") != "completed"]
    if open_items:
        r = nudge("todos", items="\n".join("- [%s] %s" % (t["status"], t["content"]) for t in open_items))
        if r:
            return r
    if cfg.get("verify_nudge", True) and state.code_edited_since_run:
        r = nudge("verify", files=", ".join(state.code_edited_since_run[:5]))
        if r:
            return r
    if completion == "finish_tool":
        return nudge("finish")
    return None
