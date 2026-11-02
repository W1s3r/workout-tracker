"""Command-line entry point and interactive REPL.

  spy                       interactive session
  spy "fix the failing test"  start with a prompt
  spy -p "question"         one-shot, print answer, exit (scriptable)
  spy -c                    continue the most recent session
  spy doctor                test API + search configuration
  spy research "question"   run deep research directly
  spy index PATH|LOCATION   build/refresh the local document index
  spy setup                 write a starter config to ~/.spy/config.json
  spy webtest URL [--query Q]  find a way to reach internal web pages / search
  spy browser [URL]         open a browser Spy can drive; log in there once
  spy evalloop              measure how well the model drives the agent loop
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from spy import __version__
from spy.config import load_config, write_starter_config
from spy.context import add_note, context_files, ensure_memory_file, memory_path
from spy.llm import make_client
from spy.permissions import MODES, Permissions
from spy.session import Session
from spy.ui import UI

HELP = """Commands:
  /help                    this help
  /exit, /quit             leave (Ctrl+D also works)
  /clear                   start a fresh conversation
  /compact [focus]         summarise the conversation to free context
  /context                 show context usage and loaded memory files
  /memory [project|global|local]  show a memory file (and its path, to edit)
  /init                    analyse this project and write an SPY.md for it
  /reflect                 review this session and save lessons to memory
  /model [name]            show or switch model       /models  list models
  /mode [default|accept_edits|auto|plan]   permission mode   /plan  toggle plan mode
  /search <query>          quick intranet search
  /research <question>     deep research -> cited report in research/
  /index <path|location>   build/refresh the local document index
  /todos                   show the todo list          /tools  list tools
  /cd <dir>                change working directory    /jobs   background jobs
  /cost                    token usage this session
  /sessions, /resume [id]  list / resume saved sessions
  /doctor                  test API and search configuration
  /style [name|key value]  response style: /style bluf, /style language Spanish,
                           /style verbosity detailed, /style format plain, /style custom <text>,
                           /style save [global|project], /style reset
  /last                    how the last turn ran (steps, auto-continues, why it ended)
  /web <url>               test which internal-web access methods work for a URL
  /undo                    revert Spy's last file change (repeat to go further back)
  /changes                 list the files Spy changed this session
Shortcuts:
  # note                   save a note to the project SPY.md   (#g note -> master ~/.spy/SPY.md)
  @path                    attach a file (or folder listing) to your message - Tab completes paths
  Shift+Tab                cycle mode: default -> accept edits -> plan
  !command                 run a shell command yourself; output is shared with the agent
  \\ at end of line        continue input on next line;  \"\"\" on its own line starts/ends a block
  Ctrl+C                   interrupt the agent
Custom commands: put NAME.md in .spy/commands/ or ~/.spy/commands/, then run /NAME args
                 ($ARGUMENTS in the file is replaced by args)."""

COMMANDS = {
    "help": "show commands and shortcuts", "exit": "quit Spy", "clear": "start a fresh conversation",
    "compact": "summarise the conversation to free context", "context": "context usage and loaded SPY.md files",
    "memory": "show/edit SPY.md (project | master | local)", "init": "analyse this project and write its SPY.md",
    "reflect": "save lessons from this session to SPY.md", "model": "show or switch model", "models": "list models",
    "mode": "permission mode: default | accept_edits | auto | plan", "plan": "toggle plan (read-only) mode",
    "search": "quick intranet search", "research": "deep research -> cited report",
    "index": "index a folder/network share for doc_search", "todos": "show the todo list", "tools": "list tools",
    "cd": "change working directory", "jobs": "background shell jobs", "cost": "token usage",
    "sessions": "list this project's saved sessions", "resume": "resume a saved session",
    "doctor": "test API, search and drives", "style": "response style / language / format",
    "last": "how the last turn ran", "web": "test internal-web access for a URL",
    "undo": "revert Spy's last file change", "changes": "files Spy changed this session",
}

INIT_PROMPT = """Analyse this project and create (or improve) an SPY.md file in the project root that will be
loaded into every future session. Explore first: list the directory tree, read the README and key
config/build files, and find how to build, run, and test. Then write SPY.md with these sections:
## Overview (what this is, main components and where they live)
## Commands (exact build/test/run/lint commands)
## Conventions (code style, patterns, branch/commit conventions)
## Important paths (including any network locations used)
## Lessons learned (leave empty if none)
Be concise and specific - no generic advice. If SPY.md already exists, read it and improve it."""

REFLECT_PROMPT = """Review this conversation. Identify durable lessons worth remembering in future sessions:
user preferences and corrections, commands that worked, important paths/locations, project conventions,
and mistakes to avoid. View the relevant memory files, then use the memory tool to add only NEW,
non-duplicate, concise notes (project scope for project-specific facts, global scope for general ones).
Finally list what you saved. If nothing is worth saving, say so."""


class App:
    def __init__(self, args):
        self.args = args
        self.config = load_config(args.config)
        home = self.config.home
        home.mkdir(parents=True, exist_ok=True)
        from spy.config import project_data_dir
        self.pdata = project_data_dir(home, self.config.project_root)
        self.custom_cmds = self._load_custom_commands()
        cmds = ["/" + c for c in ("help exit quit clear compact context memory init reflect model models mode plan "
                                  "search research index todos tools cd jobs cost sessions resume doctor style last web").split()]
        cmds += ["/" + c for c in self.custom_cmds]
        self.ui = UI(self.config, color=self.config.get("ui", "color", default=True),
                     history_file=self.pdata / "history", commands=cmds)
        if args.mode:
            self.config.data["permissions"]["mode"] = args.mode
        self.permissions = Permissions(self.config, self.ui)
        self._clients: Dict[str, object] = {}
        main_name = args.model or self.config["roles"].get("main")
        if args.model:
            # --model overrides every role so everything runs on that model
            for r in list(self.config["roles"]):
                self.config.data["roles"][r] = args.model
        self.llm = self.client(main_name)
        from spy.agent import Agent
        self.agent = Agent(self.config, self.llm, self.ui, self.permissions, cwd=Path.cwd(),
                           llm_factory=self.client)
        self.session = Session(self.pdata)
        self.agent.on_change = lambda: self._save()
        from spy.tui import TerminalInput
        cmds_desc = dict(COMMANDS)
        cmds_desc.update({c: "custom command (%s)" % p.name for c, p in self.custom_cmds.items()})
        self.input = TerminalInput(self.pdata / "input_history", cmds_desc, lambda: self.agent.ctx.cwd,
                                   self._status, lambda: self.permissions.mode, self._set_mode,
                                   color=self.ui.color)

    # ------------------------------------------------------------------
    def client(self, name_or_role: str):
        prof = self.config.model_profile(name_or_role)
        if prof["name"] not in self._clients:
            self._clients[prof["name"]] = make_client(prof)
        return self._clients[prof["name"]]

    def _save(self):
        try:
            if self.agent.messages:
                self.session.save(self.agent)
        except Exception as e:
            self.ui.warn("could not save session: %s" % e)

    def _load_custom_commands(self) -> Dict[str, Path]:
        out = {}
        for d in (self.config.home / "commands", self.config.project_root / ".spy" / "commands"):
            if d.is_dir():
                for f in d.glob("*.md"):
                    out[f.stem] = f
        return out

    # ------------------------------------------------------------------
    def _status(self) -> str:
        try:
            pct = 100.0 * self.agent.context_usage() / self.agent.llm.context_tokens
        except Exception:
            pct = 0
        return "%s · ctx %d%%" % (self.agent.llm.name, pct)

    def _set_mode(self, mode: str):
        self.permissions.mode = mode

    def banner(self):
        from spy.tui import welcome_box
        files = context_files(self.config, self.agent.ctx.cwd)
        home = self.config.home.resolve()
        master = [f for f in files if f.resolve().parent == home]
        project = [f for f in files if f.resolve().parent != home]
        lines = ["✻ Welcome to Spy %s" % __version__, "",
                 "  /help for commands · @file to attach · shift+tab for modes",
                 "",
                 "  cwd:     %s" % self.agent.ctx.cwd,
                 "  project: %s" % self.config.project_root,
                 "  model:   %s" % self.llm.name,
                 "  master SPY.md:  %s" % (master[0] if master else "none - create with: #g <note> or /memory master"),
                 "  project SPY.md: %s" % (", ".join(str(f) for f in project) if project else "none - run /init to create one")]
        print(welcome_box(lines, self.ui.color))

    def repl(self, initial: Optional[str] = None):
        self.banner()
        if initial:
            self.handle(initial)
        while True:
            try:
                line = self.input.read()
            except EOFError:
                break
            except KeyboardInterrupt:
                continue
            if not line.strip():
                continue
            if self.input.fancy:
                self.echo_user(line)        # the input box erased itself - show the message
            elif not sys.stdin.isatty():
                print(line)                 # piped input isn't echoed by the terminal
            try:
                if self.handle(line) is False:
                    break
            except KeyboardInterrupt:
                self.ui.warn("Interrupted.")
        self._save()
        self.ui.info("Session saved: %s  (resume with: spy --resume %s)" % (self.session.id, self.session.id))

    def echo_user(self, line: str):
        """Show the submitted message in the transcript (the input box erases itself)."""
        first, *rest = line.split("\n")
        out = self.ui.c("> " + first, "\033[1m", "\033[90m") if self.ui.color else "> " + first
        for r in rest:
            out += "\n" + (self.ui.c("  " + r, "\033[90m") if self.ui.color else "  " + r)
        print(out)

    def expand_mentions(self, line: str) -> str:
        """@path mentions: attach file contents (or a folder listing) to the message."""
        import re as _re
        attached = []
        for m in _re.finditer(r"(?<![\w@])@(\"[^\"]+\"|[^\s]+)", line):
            raw = m.group(1).strip('"')
            p = self.agent.ctx.resolve(raw)
            if p.is_file():
                tool = self.agent.tools["read_file"]
                try:
                    body = tool.run({"path": str(p), "limit": 2000}, self.agent.ctx)
                except Exception as e:
                    body = "[could not read: %s]" % e
                attached.append("<file path=\"%s\">\n%s\n</file>" % (p, body[:60000]))
                self.ui.info("  ⎿ attached %s" % p)
            elif p.is_dir():
                body = self.agent.tools["list_dir"].run({"path": str(p), "depth": 2}, self.agent.ctx)
                attached.append("<folder path=\"%s\">\n%s\n</folder>" % (p, body[:20000]))
                self.ui.info("  ⎿ attached listing of %s" % p)
        if attached:
            line += "\n\n" + "\n\n".join(attached)
        return line

    def handle(self, line: str):
        s = line.strip()
        if s.startswith("#") and not s.startswith("#!"):
            scope = "global" if s.startswith("#g ") else "project"
            note = s[3:] if scope == "global" else s[1:]
            if note.strip():
                p = add_note(self.config, scope, note.strip(),
                             "About me / preferences" if scope == "global" else "Lessons learned")
                self.ui.info("Saved to %s SPY.md: %s" % ("master" if scope == "global" else "project", p))
            return True
        if s.startswith("!"):
            return self.bang(s[1:])
        # "/cmd args" is a command; "/usr/local/file.txt is broken" (an absolute path) is a message
        if s.startswith("/") and "/" not in s.split()[0][1:]:
            return self.slash(s)
        self.agent.run_turn(self.expand_mentions(line))
        return True

    def bang(self, cmd: str):
        tool = self.agent.tools.get("shell")
        out = tool.run({"command": cmd}, self.agent.ctx)
        print(out)
        self.agent.messages.append({"role": "user", "content": "I ran this shell command myself:\n$ %s\n%s" % (cmd, out[:8000])})
        self.agent.messages.append({"role": "assistant", "content": "Noted."})
        return True

    def slash(self, s: str):
        parts = s[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        a = self.agent
        if cmd in ("exit", "quit", "q"):
            return False
        if cmd == "help":
            print(HELP)
            if self.custom_cmds:
                print("Custom: " + ", ".join("/" + c for c in sorted(self.custom_cmds)))
        elif cmd == "clear":
            self._save()
            a.messages.clear()
            a.ctx.todos.clear()
            self.session = Session(self.pdata)
            self.ui.info("Started a new conversation.")
        elif cmd == "compact":
            a.compact(arg, force=True)
            self._save()
        elif cmd == "context":
            used = a.context_usage()
            total = a.llm.context_tokens
            self.ui.info("Context: ~%d / %d tokens (%.0f%%), %d messages" % (used, total, 100.0 * used / total, len(a.messages)))
            for f in context_files(self.config, a.ctx.cwd):
                self.ui.info("  memory file: %s" % f)
        elif cmd == "memory":
            scope = {"master": "global"}.get(arg, arg) or "project"
            p = ensure_memory_file(self.config, scope) if scope in ("project", "global", "local") else None
            if p:
                print(p.read_text(encoding="utf-8"))
                self.ui.info("(%s memory file: %s - edit it with any editor)" % (scope, p))
                editor = os.environ.get("EDITOR")
                if editor:
                    subprocess.call([editor, str(p)])
        elif cmd == "init":
            a.run_turn(INIT_PROMPT)
        elif cmd == "reflect":
            a.run_turn(REFLECT_PROMPT)
        elif cmd == "model":
            if not arg:
                self.ui.info("Current model: %s. Configured: %s" % (a.llm.name, ", ".join(self.config["models"])))
            else:
                try:
                    a.llm = self.llm = self.client(arg)
                    self.ui.info("Switched to %s" % arg)
                except KeyError as e:
                    self.ui.error(str(e))
        elif cmd == "models":
            for n, m in self.config["models"].items():
                print("  %s%s -> %s (%s ctx, tools: %s)" % ("*" if n == a.llm.name else " ", n, m.get("model"),
                                                            m.get("context_tokens"), m.get("tool_mode")))
            if hasattr(a.llm, "list_models"):
                try:
                    print("Server reports: " + ", ".join(a.llm.list_models()))
                except Exception as e:
                    self.ui.info("(could not list server models: %s)" % str(e)[:100])
        elif cmd == "mode":
            if arg in MODES:
                self.permissions.mode = arg
            self.ui.info("Permission mode: %s  (options: %s)" % (self.permissions.mode, ", ".join(MODES)))
        elif cmd == "plan":
            self.permissions.mode = "default" if self.permissions.mode == "plan" else "plan"
            self.ui.info("Permission mode: %s" % self.permissions.mode)
        elif cmd == "search":
            if not arg:
                self.ui.error("usage: /search <keywords>")
            else:
                from spy.tools.research_tools import IntranetSearch
                try:
                    print(IntranetSearch().run({"query": arg}, a.ctx))
                except Exception as e:
                    self.ui.error(str(e))
        elif cmd == "research":
            if not arg:
                self.ui.error("usage: /research <question>")
            else:
                self.research(arg)
        elif cmd == "index":
            self.index(arg.split() if arg else [])
        elif cmd == "todos":
            self.ui.show_todos(a.ctx.todos) if a.ctx.todos else self.ui.info("No todos.")
        elif cmd == "tools":
            for t in a.tools.values():
                print("  %-16s %s" % (t.name, t.description.strip().splitlines()[0][:90]))
        elif cmd == "cd":
            p = a.ctx.resolve(arg or str(Path.home()))
            if p.is_dir():
                a.ctx.cwd = p.resolve()
                self.ui.info("cwd: %s" % a.ctx.cwd)
            else:
                self.ui.error("Not a directory: %s" % p)
        elif cmd == "jobs":
            print(a.tools["shell_output"].run({}, a.ctx))
        elif cmd == "cost":
            for name, c in self._clients.items():
                u = c.total_usage
                self.ui.info("%s: %d calls, %d prompt tokens, %d completion tokens" % (
                    name, u["calls"], u["prompt_tokens"], u["completion_tokens"]))
        elif cmd == "sessions":
            for s_ in Session.list_recent(self.pdata):
                print("  %s  %s  (%d msgs)  %s" % (s_["id"], s_["saved"], s_["messages"], s_["title"]))
        elif cmd == "resume":
            self.resume(arg or None)
        elif cmd == "doctor":
            from spy.doctor import run_doctor
            run_doctor(self.config, a.llm.name)
        elif cmd == "style":
            self.style_cmd(arg)
        elif cmd == "undo":
            self.ui.info(a.ctx.history.undo())
        elif cmd == "changes":
            print(a.ctx.history.summary())
        elif cmd == "last":
            st = a.last_turn
            if not st:
                self.ui.info("No turn yet.")
            else:
                self.ui.info("route=%s steps=%d ended_by=%s auto-continues=%s tools=%s" % (
                    st.route, st.steps, st.ended_by, ",".join(st.nudges) or "none",
                    ", ".join(st.tools_used) or "none"))
        elif cmd == "web":
            from spy import webaccess
            if not arg:
                self.ui.info("usage: /web <url>   (use {query} in the URL to also test result extraction)")
            else:
                webaccess.webtest(arg.split()[0], " ".join(arg.split()[1:]) or None)
        elif cmd in self.custom_cmds:
            body = self.custom_cmds[cmd].read_text(encoding="utf-8")
            body = body.replace("$ARGUMENTS", arg)
            a.run_turn(body)
        else:
            self.ui.error("Unknown command /%s - try /help" % cmd)
        return True

    def style_cmd(self, arg: str):
        from spy.style import BUILTIN_STYLES, DEFAULT_STYLE, available_styles, current_style, save_style
        cfg = self.config
        if not hasattr(cfg, "style_overrides"):
            cfg.style_overrides = {}
        words = arg.split(None, 1)
        if not words:
            st = current_style(cfg)
            self.ui.info("Current style: " + ", ".join("%s=%s" % (k, v) for k, v in st.items() if v))
            self.ui.info("Named styles: " + ", ".join(sorted(available_styles(cfg))))
            self.ui.info("Your own: put NAME.md in %s" % (cfg.home / "styles"))
            return
        key = words[0].lower()
        val = words[1].strip() if len(words) > 1 else ""
        if key == "reset":
            cfg.style_overrides = {}
            self.ui.info("Style overrides cleared (saved style files are unchanged).")
        elif key == "save":
            p = save_style(cfg, cfg.style_overrides, "project" if val == "project" else "global")
            self.ui.info("Saved %s to %s" % (cfg.style_overrides, p))
        elif key in ("language", "verbosity", "format", "tone", "custom", "name"):
            cfg.style_overrides[key] = val
            self.ui.info("style %s = %r (this session; /style save to keep)" % (key, val))
        elif key in available_styles(cfg):
            cfg.style_overrides["name"] = key
            self.ui.info("Using style '%s' (this session; /style save to keep)" % key)
        else:
            self.ui.error("Unknown style or setting '%s'. Try /style to list options." % key)

    def resume(self, sid: Optional[str]):
        recent = Session.list_recent(self.pdata, 30)
        if not recent:
            self.ui.info("No saved sessions.")
            return
        if sid is None:
            for i, s_ in enumerate(recent[:15], 1):
                print("  %2d. %s  %s  %s" % (i, s_["id"], s_["saved"], s_["title"]))
            ans = input("Resume which? (number, Enter to cancel): ").strip()
            if not ans.isdigit() or not (1 <= int(ans) <= len(recent)):
                return
            sid = recent[int(ans) - 1]["id"]
        match = [s_ for s_ in recent if s_["id"].startswith(sid)]
        if not match:
            self.ui.error("No session %s" % sid)
            return
        self.session = Session(self.pdata, match[0]["id"])
        data = self.session.load_into(self.agent)
        if data.get("cwd") and Path(data["cwd"]).is_dir():
            self.agent.ctx.cwd = Path(data["cwd"])
        self.ui.info("Resumed %s (%d messages). Last exchange:" % (self.session.id, len(self.agent.messages)))
        for m in [m for m in self.agent.messages if m["role"] in ("user", "assistant") and m.get("content")][-2:]:
            print("  %s: %s" % (m["role"], m["content"][:300]))

    def research(self, question: str):
        from spy.research.engine import DeepResearch
        from spy.research.sources import build_sources
        srcs = build_sources(self.config)
        if not srcs:
            self.ui.error("No search sources enabled - configure search.sources (see README).")
            return
        eng = DeepResearch(self.config, self.client("research"), self.client("fast"), srcs, log=self.ui.info)
        res = eng.run(question, save_dir=self.agent.ctx.cwd / self.config.get("research", "output_dir", default="research"))
        self.ui.assistant_message(res["report"], False)
        self.ui.info("Saved: %s  (%d sources, %d facts, %d queries, %ds)" % (
            res["path"], res["sources"], res["facts"], res["queries"], res["seconds"]))
        # let the conversation continue from the report
        self.agent.messages.append({"role": "user", "content": "/research " + question})
        self.agent.messages.append({"role": "assistant", "content": "I researched this and saved the report to %s:\n\n%s"
                                    % (res["path"], res["report"][:6000])})

    def index(self, targets: List[str]):
        from spy.research.docindex import DocIndex
        locs = self.config.get("locations") or {}
        roots = [locs.get(t, t) for t in targets] or list(locs.values())
        if not roots:
            self.ui.error("usage: /index <folder or location name> ...  (or configure 'locations')")
            return
        idx = DocIndex(self.config)
        try:
            for r in roots:
                if not os.path.isdir(r):
                    self.ui.error("not a directory: %s" % r)
            res = idx.index_paths([r for r in roots if os.path.isdir(r)], progress=self.ui.info)
            self.ui.info("Index updated: %s. Totals: %s" % (res, idx.stats()))
        finally:
            idx.close()


def main(argv: Optional[List[str]] = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < (3, 7):
        sys.exit("Spy needs Python 3.7+")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    sub = argv[0] if argv else ""
    ap = argparse.ArgumentParser(prog="spy", description="Spy")
    ap.add_argument("--config", help="extra config file")
    ap.add_argument("--model", help="model name from config (overrides all roles)")
    ap.add_argument("--mode", choices=MODES, help="permission mode")
    if sub == "doctor":
        ap.add_argument("cmd")
        args = ap.parse_args(argv)
        from spy.doctor import run_doctor
        run_doctor(load_config(args.config), args.model)
        return
    if sub == "webtest":
        ap.add_argument("cmd")
        ap.add_argument("url")
        ap.add_argument("--query", help="search words to substitute for {query} in the URL")
        ap.add_argument("--methods", help="comma list: python,curl,powershell,headless,cdp")
        args = ap.parse_args(argv)
        load_config(args.config)
        from spy import webaccess
        webaccess.webtest(args.url, args.query, args.methods.split(",") if args.methods else None)
        return
    if sub == "browser":
        ap.add_argument("cmd")
        ap.add_argument("url", nargs="?", default="about:blank")
        args = ap.parse_args(argv)
        load_config(args.config)
        from spy import webaccess
        cdp = webaccess.CDP()
        if cdp.available():
            print("A browser is already listening on DevTools port %d - Spy will use it." % cdp.port)
            return
        cdp.launch(headless=False, url=args.url)
        print("Opened a browser window that Spy can drive (DevTools port %d, profile %s)." % (
            cdp.port, webaccess.profile_dir("cdp")))
        print("1. In that window, open your internal sites and log in (smart card / SSO) as usual.")
        print("2. Keep the window open (minimize it). Spy loads pages in background tabs.")
        print("3. Test with:  spy webtest <url> --methods cdp")
        print("Logins usually persist in this profile, so next time a hidden copy can start automatically.")
        return
    if sub == "evalloop":
        ap.add_argument("cmd")
        ap.add_argument("--only", help="comma list of scenario names")
        ap.add_argument("--completion", choices=["text", "finish_tool"])
        ap.add_argument("--verbose", action="store_true")
        args = ap.parse_args(argv)
        app = App(args)
        if args.completion:
            app.config.data["agent"]["completion"] = args.completion
        from spy.evals import run_evals
        sys.exit(run_evals(app, args.only.split(",") if args.only else None, args.verbose))
    if sub == "setup":
        from spy.config import HOME_DIR
        p = HOME_DIR / "config.json"
        if p.exists():
            print("%s already exists - not overwriting." % p)
        else:
            write_starter_config(p)
            print("Wrote starter config to %s. Fill in the <PLACEHOLDERS>, then run: spy doctor" % p)
        ensure_memory_file(load_config(), "global")
        print("Global memory file: %s" % (HOME_DIR / "SPY.md"))
        return
    if sub == "index":
        ap.add_argument("cmd")
        ap.add_argument("paths", nargs="*")
        args = ap.parse_args(argv)
        args.continue_ = False
        args.resume = None
        args.print = None
        args.prompt = []
        app = App(args)
        app.index(args.paths)
        return
    if sub == "research":
        ap.add_argument("cmd")
        ap.add_argument("question", nargs="+")
        args = ap.parse_args(argv)
        args.mode = None
        app = App(args)
        app.research(" ".join(args.question))
        return
    ap.add_argument("-p", "--print", metavar="PROMPT", help="run one prompt non-interactively and print the answer")
    ap.add_argument("-c", "--continue", dest="continue_", action="store_true", help="continue the last session")
    ap.add_argument("--resume", metavar="ID", nargs="?", const="", help="resume a session (list if no id)")
    ap.add_argument("--version", action="version", version="spy " + __version__)
    ap.add_argument("prompt", nargs="*", help="initial prompt")
    args = ap.parse_args(argv)
    app = App(args)
    if args.continue_:
        recent = Session.list_recent(app.pdata, 1)
        if recent:
            app.resume(recent[0]["id"])
    elif args.resume is not None:
        app.resume(args.resume or None)
    if args.print:
        text = args.print if args.print != "-" else sys.stdin.read()
        piped = not sys.stdout.isatty()
        final = app.agent.run_turn(text, quiet=piped)
        if piped:
            print(final)
        app._save()
        return
    app.repl(" ".join(args.prompt) if args.prompt else None)


if __name__ == "__main__":
    main()
