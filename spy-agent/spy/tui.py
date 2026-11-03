"""Claude-Code-style terminal input for Spy.

With prompt_toolkit (shipped with Anaconda): an inline bordered input box that
grows with your text, a status line (mode, model, context use), a slash-command
menu with descriptions, @file completion, history, and key bindings:

  Enter            send                    Alt+Enter / Ctrl+J   new line
  Shift+Tab        cycle mode: default -> accept edits -> plan
  Alt+V / Ctrl+V   paste a screenshot from the clipboard (shows as [Image #1])
  Up / Down        history                 Tab                  complete
  Ctrl+C           clear input (twice on an empty box to exit)    Ctrl+D  exit

Without prompt_toolkit it falls back to a plain prompt with the same status line.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import has_completions
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.styles import DynamicStyle, Style
    from prompt_toolkit.widgets import Frame, TextArea
    HAVE_PTK = True
except Exception:  # pragma: no cover - depends on environment
    HAVE_PTK = False

MODE_CYCLE = ["default", "accept_edits", "plan"]
MODE_LABEL = {"default": "", "accept_edits": "⏵⏵ accept edits on", "plan": "⏸ plan mode on",
              "auto": "⏵⏵⏵ auto-approve on"}

if HAVE_PTK:
    class SpyCompleter(Completer):
        def __init__(self, commands: Dict[str, str], cwd_fn: Callable[[], Path]):
            self.commands = commands
            self.cwd_fn = cwd_fn

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/") and " " not in text:
                for name, desc in sorted(self.commands.items()):
                    if name.startswith(text[1:]):
                        yield Completion("/" + name, start_position=-len(text), display="/" + name,
                                         display_meta=desc)
                return
            word = text.split()[-1] if text.split() and not text.endswith(" ") else ""
            if word.startswith("@"):
                frag = word[1:]
                base = self.cwd_fn()
                d, _, prefix = frag.replace("\\", "/").rpartition("/")
                folder = (base / d) if d else base
                try:
                    names = sorted(os.listdir(folder))
                except OSError:
                    return
                for n in names:
                    if n.startswith(".") and not prefix.startswith("."):
                        continue
                    if n.lower().startswith(prefix.lower()):
                        full = (d + "/" if d else "") + n + ("/" if (folder / n).is_dir() else "")
                        yield Completion("@" + full, start_position=-len(word), display=n)


class TerminalInput:
    def __init__(self, history_path: Optional[Path], commands: Dict[str, str], cwd_fn: Callable[[], Path],
                 status_fn: Callable[[], str], get_mode: Callable[[], str], set_mode: Callable[[str], None],
                 color: bool = True, on_paste: Optional[Callable[[], tuple]] = None):
        self.commands = commands
        self.cwd_fn = cwd_fn
        self.status_fn = status_fn
        self.get_mode = get_mode
        self.set_mode = set_mode
        self.color = color
        self.history_path = history_path
        self.on_paste = on_paste      # () -> (list of labels like "[Image #1]", clipboard text or None)
        self.prefill = ""             # text to start the next input with (e.g. a voice transcript)
        self._last_ctrl_c = 0.0
        self._hint = ""
        self.fancy = HAVE_PTK and sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("SPY_PLAIN") is None
        self._history = None
        if self.fancy:
            try:
                self._history = FileHistory(str(history_path)) if history_path else InMemoryHistory()
            except Exception:
                self._history = InMemoryHistory()

    # ------------------------------------------------------------ status --
    def status_text(self) -> str:
        mode = self.get_mode()
        label = MODE_LABEL.get(mode, mode)
        left = (label + " (shift+tab to cycle)") if label else "? /help · shift+tab: modes · alt+v: paste image"
        return "  %s   %s" % (left, self.status_fn())

    # -------------------------------------------------------------- read --
    def read(self) -> str:
        prefill, self.prefill = self.prefill, ""
        if prefill and not self.fancy:
            sys.stdout.write("\n" + self._c("Transcript: ", "\033[1m") + prefill + "\n")
            sys.stdout.write(self._c("Enter = send it · type a replacement · n = discard: ", "\033[90m"))
            sys.stdout.flush()
            ans = sys.stdin.readline()
            if not ans:
                raise EOFError
            ans = ans.rstrip("\n")
            return prefill if ans.strip() == "" else ("" if ans.strip().lower() == "n" else ans)
        if self.fancy:
            self._prefill_now = prefill
            try:
                return self._read_fancy()
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as e:  # any terminal incompatibility -> plain mode for the session
                self.fancy = False
                sys.stdout.write("(fancy input disabled: %s)\n" % e)
        return self._read_plain()

    def _read_fancy(self) -> str:
        kb = KeyBindings()
        result: Dict[str, object] = {}
        ta = TextArea(multiline=True, prompt=FormattedText([("class:prompt", "> ")]), history=self._history,
                      completer=SpyCompleter(self.commands, self.cwd_fn), complete_while_typing=True,
                      wrap_lines=True, height=Dimension(min=1, max=15), focus_on_click=True)
        pre = getattr(self, "_prefill_now", "")
        if pre:
            ta.buffer.insert_text(pre)

        def paste(event):
            """Alt+V / Ctrl+V: attach a clipboard image (screenshot) or paste clipboard text."""
            if not self.on_paste:
                return
            labels, text = self.on_paste()
            buf = event.current_buffer
            if labels:
                lead = "" if (not buf.text or buf.text.endswith(" ")) else " "
                buf.insert_text(lead + " ".join(labels) + " ")
                self._hint = ""
            elif text:
                buf.insert_text(text.replace("\r\n", "\n"))
            else:
                self._hint = "Clipboard has no image (copy a screenshot first: Win+Shift+S)"
            event.app.invalidate()

        kb.add("escape", "v")(paste)
        kb.add("c-v")(paste)

        def submit(event):
            text = ta.text
            if text.endswith("\\"):  # backslash-enter = newline (like Claude Code)
                ta.buffer.delete_before_cursor(1)
                ta.buffer.insert_text("\n")
                return
            ta.buffer.append_to_history()
            event.app.exit(result=text)

        @kb.add("enter", filter=~has_completions)
        def _(event):
            submit(event)

        @kb.add("enter", filter=has_completions)
        def _(event):
            buf = event.current_buffer
            state = buf.complete_state
            before = buf.text
            comp = (state.current_completion or (state.completions[0] if state.completions else None)) if state else None
            if comp is not None:
                buf.apply_completion(comp)
            if comp is None or buf.text == before or (buf.text.startswith("/") and " " not in buf.text):
                buf.cancel_completion()
                submit(event)  # nothing new to complete, or a slash command was picked -> send

        @kb.add("escape", "enter")
        @kb.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("s-tab")
        def _(event):
            cur = self.get_mode()
            nxt = MODE_CYCLE[(MODE_CYCLE.index(cur) + 1) % len(MODE_CYCLE)] if cur in MODE_CYCLE else "default"
            self.set_mode(nxt)
            event.app.invalidate()

        @kb.add("up")
        def _(event):
            event.current_buffer.auto_up(count=event.arg)

        @kb.add("down")
        def _(event):
            event.current_buffer.auto_down(count=event.arg)

        @kb.add("c-c")
        def _(event):
            if ta.text:
                ta.text = ""
                self._hint = ""
                return
            now = time.time()
            if now - self._last_ctrl_c < 2.0:
                event.app.exit(exception=EOFError())
                return
            self._last_ctrl_c = now
            self._hint = "Press Ctrl-C again to exit"
            event.app.invalidate()

        @kb.add("c-d")
        def _(event):
            if not ta.text:
                event.app.exit(exception=EOFError())
            else:
                event.current_buffer.delete(1)

        def status():
            hint = ("  " + self._hint) if self._hint else ""
            return FormattedText([("class:status", self.status_text()), ("class:hint", hint)])

        frame = Frame(ta, style="class:frame")
        root = FloatContainer(
            content=HSplit([frame, Window(FormattedTextControl(status), height=1)]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=10, scroll_offset=1))])
        base = {
            "frame.border": "#888888", "prompt": "bold", "status": "#777777", "hint": "#d7af5f",
            "completion-menu.completion": "bg:#303030 #dddddd",
            "completion-menu.completion.current": "bg:#5f87af #ffffff",
            "completion-menu.meta.completion": "bg:#262626 #999999",
            "completion-menu.meta.completion.current": "bg:#5f87af #ffffff",
        }
        border = {"accept_edits": "#b48ead", "plan": "#5fafaf", "auto": "#d7875f"}
        styles = {m: Style.from_dict(dict(base, **({"frame.border": c} if c else {})))
                  for m, c in [("default", None)] + list(border.items())}
        style = DynamicStyle(lambda: styles.get(self.get_mode(), styles["default"]))
        app = Application(layout=Layout(root, focused_element=ta), key_bindings=kb, style=style,
                          full_screen=False, mouse_support=False, erase_when_done=True)
        text = app.run()
        self._hint = ""
        return text if isinstance(text, str) else ""

    def _read_plain(self) -> str:
        sys.stdout.write("\n" + self._c(self.status_text(), "\033[90m") + "\n")
        sys.stdout.write(self._c("> ", "\033[1m"))
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        line = line.rstrip("\n")
        if line.strip() == '"""':
            buf = []
            while True:
                l2 = sys.stdin.readline()
                if not l2 or l2.strip() == '"""':
                    break
                buf.append(l2.rstrip("\n"))
            return "\n".join(buf)
        while line.endswith("\\"):
            sys.stdout.write(self._c(". ", "\033[90m"))
            sys.stdout.flush()
            nxt = sys.stdin.readline()
            if not nxt:
                break
            line = line[:-1] + "\n" + nxt.rstrip("\n")
        return line

    def _c(self, s, code):
        return code + s + "\033[0m" if self.color and sys.stdout.isatty() else s


def welcome_box(lines: List[str], color: bool = True, width: Optional[int] = None) -> str:
    try:
        cols = os.get_terminal_size().columns
    except OSError:
        cols = 80
    width = width or max(40, min(cols - 2, 78))
    inner = width - 4
    out = ["╭" + "─" * (width - 2) + "╮"]
    for i, l in enumerate(lines):
        if len(l) > inner:
            l = "…" + l[-(inner - 1):]
        out.append("│ " + l + " " * (inner - len(l)) + " │")
    out.append("╰" + "─" * (width - 2) + "╯")
    s = "\n".join(out)
    if color and sys.stdout.isatty():
        s = s.replace(lines[0], "\033[1;38;5;173m" + lines[0] + "\033[0m", 1)
    return s
