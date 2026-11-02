"""Terminal UI. Pure stdlib (ANSI colors); uses prompt_toolkit (input history,
multi-line editing, completion) and rich (markdown) automatically if Anaconda
has them, otherwise falls back to input()/plain text."""
from __future__ import annotations

import difflib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:  # Windows: enable ANSI escape processing
    if os.name == "nt":
        try:
            import colorama
            colorama.just_fix_windows_console()
        except Exception:
            os.system("")
except Exception:
    pass

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    HAVE_PTK = True
except Exception:
    HAVE_PTK = False

try:
    from rich.console import Console
    from rich.markdown import Markdown
    HAVE_RICH = True
except Exception:
    HAVE_RICH = False


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"


class UI:
    def __init__(self, config=None, color: bool = True, history_file: Optional[Path] = None,
                 commands: Optional[List[str]] = None):
        self.color = color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.md = HAVE_RICH and (config.get("ui", "markdown", default=True) if config else True)
        self.console = Console() if HAVE_RICH else None
        self.show_reasoning = bool(config.get("agent", "show_reasoning", default=False)) if config else False
        self._streamed = False
        self._thinking_shown = False
        self._spin_stop: Optional[threading.Event] = None
        self.session = None
        self.indent = ""
        if HAVE_PTK and sys.stdin.isatty():
            kb = KeyBindings()

            @kb.add("escape", "enter")
            def _(event):
                event.current_buffer.insert_text("\n")

            try:
                hist = FileHistory(str(history_file)) if history_file else None
                self.session = PromptSession(history=hist, key_bindings=kb,
                                             completer=WordCompleter(commands or [], sentence=True))
            except Exception:
                self.session = None

    # -- primitives -----------------------------------------------------
    def c(self, s: str, *codes: str) -> str:
        return ("".join(codes) + s + C.RESET) if self.color else s

    def _print(self, s: str = "", end: str = "\n"):
        self.stop_thinking()
        sys.stdout.write(s + end)
        sys.stdout.flush()

    def info(self, msg: str):
        self._print(self.indent + self.c(msg, C.GREY))

    def warn(self, msg: str):
        self._print(self.indent + self.c(msg, C.YELLOW))

    def error(self, msg: str):
        self._print(self.indent + self.c(msg, C.RED))

    def banner(self, lines: List[str]):
        self._print(self.c(lines[0], C.BOLD, C.CYAN))
        for l in lines[1:]:
            self._print(self.c(l, C.GREY))

    # -- thinking indicator ---------------------------------------------
    def start_thinking(self, label: str = "thinking"):
        if not sys.stdout.isatty():
            return
        self.stop_thinking()
        ev = threading.Event()
        self._spin_stop = ev

        def spin():
            frames = "|/-\\"
            i = 0
            t0 = time.time()
            while not ev.is_set():
                sys.stdout.write("\r" + self.indent + self.c("%s %s %ds" % (frames[i % 4], label, time.time() - t0), C.GREY))
                sys.stdout.flush()
                i += 1
                ev.wait(0.2)
        threading.Thread(target=spin, daemon=True).start()
        self._thinking_shown = True

    def stop_thinking(self):
        if self._spin_stop is not None:
            self._spin_stop.set()
            self._spin_stop = None
            time.sleep(0.05)
        if self._thinking_shown:
            sys.stdout.write("\r\033[K" if self.color else "\r" + " " * 40 + "\r")
            sys.stdout.flush()
            self._thinking_shown = False

    # -- model output ---------------------------------------------------
    def stream(self, kind: str, chunk: str):
        if kind == "reasoning":
            if self.show_reasoning:
                self.stop_thinking()
                sys.stdout.write(self.c(chunk, C.DIM))
                sys.stdout.flush()
            return
        self.stop_thinking()
        if not self._streamed:
            sys.stdout.write("\n")
            self._streamed = True
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def assistant_message(self, text: str, was_streamed: bool):
        self.stop_thinking()
        if was_streamed:
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif text.strip():
            self._print()
            if self.md and self.console:
                self.console.print(Markdown(text))
            else:
                self._print(text)
        self._streamed = False

    def reset_stream(self):
        self._streamed = False

    def reasoning(self, text: str):
        if self.show_reasoning and text:
            self._print(self.c(text.strip()[:3000], C.DIM))

    def tool_start(self, desc: str):
        self._print(self.indent + self.c("● ", C.GREEN) + self.c(desc, C.BOLD))

    def tool_result(self, text: str, ok: bool = True, max_lines: int = 6):
        lines = (text or "").strip().splitlines()
        shown = lines[:max_lines]
        color = C.GREY if ok else C.RED
        for i, l in enumerate(shown):
            self._print(self.indent + self.c(("  ⎿ " if i == 0 else "    ") + l[:200], color))
        if len(lines) > max_lines:
            self._print(self.indent + self.c("    … +%d lines" % (len(lines) - max_lines), C.GREY))

    def show_todos(self, todos):
        mark = {"completed": "☒", "in_progress": "◐", "pending": "☐"}
        for t in todos:
            col = C.GREEN if t["status"] == "completed" else (C.YELLOW if t["status"] == "in_progress" else C.GREY)
            self._print(self.indent + "    " + self.c("%s %s" % (mark.get(t["status"], "☐"), t["content"]), col))

    # -- interaction ----------------------------------------------------
    def prompt(self, mode: str = "default") -> str:
        self.stop_thinking()
        tag = "" if mode == "default" else "[%s] " % mode
        if self.session is not None:
            return self.session.prompt("\n%s> " % tag, multiline=False)
        sys.stdout.write("\n" + self.c(tag + "> ", C.BOLD, C.CYAN))
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        line = line.rstrip("\n")
        # multi-line: end a line with '\' to continue, or wrap in triple quotes
        if line.strip() == '"""':
            buf = []
            while True:
                l2 = sys.stdin.readline()
                if not l2 or l2.strip() == '"""':
                    break
                buf.append(l2.rstrip("\n"))
            return "\n".join(buf)
        while line.endswith("\\"):
            sys.stdout.write(self.c(". ", C.GREY))
            sys.stdout.flush()
            nxt = sys.stdin.readline()
            if not nxt:
                break
            line = line[:-1] + "\n" + nxt.rstrip("\n")
        return line

    def _input(self, label: str) -> str:
        self.stop_thinking()
        sys.stdout.write(label)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return ""
        return line.strip()

    def confirm_tool(self, tool, args) -> Tuple[str, str]:
        self.stop_thinking()
        self._print(self.c("\n┌ Permission needed: ", C.YELLOW, C.BOLD) + tool.describe_call(args))
        preview = self._preview(tool, args)
        if preview:
            for l in preview.splitlines()[:60]:
                col = C.GREEN if l.startswith("+") else C.RED if l.startswith("-") else C.GREY
                self._print(self.c("│ " + l, col))
        ans = self._input(self.c("└ Allow? [y]es / [a]lways this session / [n]o (+feedback): ", C.YELLOW)).strip()
        low = ans.lower()
        if low in ("y", "yes", ""):
            return "yes", ""
        if low in ("a", "always"):
            return "always", ""
        fb = ans[1:].strip() if low.startswith("n") else ans
        if low in ("n", "no"):
            fb = self._input(self.c("  Tell the agent what to do instead (Enter to skip): ", C.GREY))
        return "no", fb

    def _preview(self, tool, args) -> str:
        try:
            if tool.name == "edit_file":
                return "".join(difflib.unified_diff(args.get("old_string", "").splitlines(True),
                                                    args.get("new_string", "").splitlines(True), n=1))[:4000]
            if tool.name == "write_file":
                body = args.get("content", "")
                head = "\n".join("+" + l for l in body.splitlines()[:40])
                return head + ("\n+ … (%d lines total)" % body.count("\n") if body.count("\n") > 40 else "")
            if tool.name == "run_python" and args.get("code"):
                return "\n".join("  " + l for l in args["code"].splitlines()[:40])
            if tool.name in ("shell",):
                return "$ " + args.get("command", "")
            if tool.name in ("gitlab", "memory"):
                return json.dumps(args, indent=1)[:1500]
        except Exception:
            pass
        return ""

    def ask(self, question: str, options: List[str]) -> str:
        self._print(self.c("\n? " + question, C.MAGENTA, C.BOLD))
        for i, o in enumerate(options, 1):
            self._print("  %d. %s" % (i, o))
        ans = self._input(self.c("> ", C.MAGENTA))
        if options and ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        return ans or "(no answer)"


class QuietUI(UI):
    """UI for sub-agents: indented, no streaming text, shares confirm/ask."""

    def __init__(self, parent: UI):
        self.__dict__.update(parent.__dict__)
        self.indent = parent.indent + "    "
        self._parent = parent

    def stream(self, kind, chunk):
        pass

    def assistant_message(self, text, was_streamed):
        pass

    def start_thinking(self, label="thinking"):
        pass
