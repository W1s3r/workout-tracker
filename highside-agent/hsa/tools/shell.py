"""Shell + Python execution tools (cross-platform: bash/sh, PowerShell, cmd)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from hsa.tools.base import Tool, ToolError, truncate

CWD_MARK = "__HSA_CWD__"


def detect_shell(configured: Optional[str] = None) -> List[str]:
    """Return argv prefix used to run a command string."""
    if configured:
        low = configured.lower()
        if "powershell" in low or low.endswith("pwsh") or low.endswith("pwsh.exe"):
            return [configured, "-NoProfile", "-NonInteractive", "-Command"]
        if low.endswith("cmd") or low.endswith("cmd.exe"):
            return [configured, "/d", "/s", "/c"]
        return [configured, "-c"]
    if os.name == "nt":
        for ps in ("pwsh", "powershell"):
            exe = shutil.which(ps)
            if exe:
                return [exe, "-NoProfile", "-NonInteractive", "-Command"]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]
    return [shutil.which("bash") or "/bin/sh", "-c"]


def shell_kind(argv: List[str]) -> str:
    exe = os.path.basename(argv[0]).lower()
    if "powershell" in exe or "pwsh" in exe:
        return "powershell"
    if exe.startswith("cmd"):
        return "cmd"
    return "posix"


def _wrap_with_cwd(cmd: str, kind: str) -> str:
    if kind == "powershell":
        return "%s\n$__ec = $LASTEXITCODE; Write-Output ''; Write-Output '%s'; (Get-Location).Path; exit $__ec" % (cmd, CWD_MARK)
    if kind == "cmd":
        return "%s & echo. & echo %s & cd" % (cmd, CWD_MARK)
    return "%s\n__ec=$?; echo; echo %s; pwd; exit $__ec" % (cmd, CWD_MARK)


class Shell(Tool):
    name = "shell"
    read_only = False
    parameters = {"type": "object", "properties": {
        "command": {"type": "string", "description": "The command to run"},
        "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 120, max 1800)"},
        "background": {"type": "boolean", "description": "Run in background; check later with shell_output"},
    }, "required": ["command"]}

    def __init__(self, configured: Optional[str] = None, default_timeout: int = 120):
        self.argv = detect_shell(configured)
        self.kind = shell_kind(self.argv)
        self.default_timeout = default_timeout
        self.description = """Run a command in a %s shell (%s). The working directory persists between
calls (cd works). Use for git, running scripts/tests, builds, and system commands.
Prefer read_file/grep/glob/list_dir over cat/grep/find/ls. Quote paths with spaces.
Long-running servers should use background=true. Output is truncated if huge.""" % (
            self.kind, os.path.basename(self.argv[0]))

    def permission_key(self, args):
        return "shell(%s)" % args.get("command", "")

    def describe_call(self, args):
        return "shell: %s" % args.get("command", "")[:200]

    def run(self, args, ctx):
        cmd = args["command"]
        timeout = min(1800, int(args.get("timeout") or self.default_timeout))
        env = dict(os.environ, HSA="1", GIT_TERMINAL_PROMPT="0", PYTHONIOENCODING="utf-8")
        if args.get("background"):
            return self._background(cmd, ctx, env)
        wrapped = _wrap_with_cwd(cmd, self.kind)
        try:
            proc = subprocess.Popen(self.argv + [wrapped], cwd=str(ctx.cwd), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
        except OSError as e:
            raise ToolError("Could not start shell %s: %s" % (self.argv[0], e))
        try:
            out, _ = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, _ = proc.communicate()
            timed_out = True
        text = out.decode("utf-8", "replace") if out else ""
        if os.name == "nt" and "�" in text and out:
            text = out.decode("cp1252", "replace")
        text = text.replace("\r\n", "\n")
        # recover cwd
        if CWD_MARK in text:
            body, _, tail = text.rpartition(CWD_MARK)
            new_cwd = tail.strip().splitlines()[0].strip() if tail.strip() else ""
            if new_cwd and os.path.isdir(new_cwd):
                ctx.cwd = Path(new_cwd)
            text = body.rstrip()
        limit = int(ctx.config.get("agent", "max_tool_output_chars", default=16000))
        text = truncate(text, limit, "full output not shown")
        status = "TIMED OUT after %ds" % timeout if timed_out else "exit code %s" % proc.returncode
        return "%s\n[%s; cwd: %s]" % (text or "(no output)", status, ctx.cwd)

    def _background(self, cmd, ctx, env):
        job = "job_" + uuid.uuid4().hex[:6]
        logf = Path(tempfile.gettempdir()) / ("hsa_%s.log" % job)
        fh = open(logf, "wb")
        proc = subprocess.Popen(self.argv + [cmd], cwd=str(ctx.cwd), env=env, stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        ctx.bg_jobs[job] = {"proc": proc, "log": logf, "cmd": cmd, "fh": fh, "pos": 0, "start": time.time()}
        return "Started background job %s (pid %d). Use shell_output with job_id=%s to read output." % (job, proc.pid, job)


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass


class ShellOutput(Tool):
    name = "shell_output"
    description = """Read new output from a background shell job, or list jobs (no job_id).
Set kill=true to stop the job."""
    parameters = {"type": "object", "properties": {
        "job_id": {"type": "string"}, "kill": {"type": "boolean"}}, "required": []}

    def run(self, args, ctx):
        if not args.get("job_id"):
            if not ctx.bg_jobs:
                return "No background jobs."
            return "\n".join("%s  %s  %s" % (j, "running" if v["proc"].poll() is None else
                                             "exited %s" % v["proc"].returncode, v["cmd"][:80])
                             for j, v in ctx.bg_jobs.items())
        j = ctx.bg_jobs.get(args["job_id"])
        if not j:
            raise ToolError("No such job: %s" % args["job_id"])
        if args.get("kill") and j["proc"].poll() is None:
            _kill_tree(j["proc"])
        with open(j["log"], "rb") as f:
            f.seek(j["pos"])
            new = f.read()
            j["pos"] = f.tell()
        state = "running" if j["proc"].poll() is None else "exited with code %s" % j["proc"].returncode
        return "%s\n[job %s %s]" % (truncate(new.decode("utf-8", "replace"), 16000) or "(no new output)",
                                    args["job_id"], state)


class RunPython(Tool):
    name = "run_python"
    read_only = False
    description = """Run Python code with the same interpreter as this agent (Anaconda: numpy,
pandas, openpyxl etc. are usually available). Give either `code` (saved to a scratch
file and run) or `path` to an existing .py script, plus optional args. Use print()
to show results. Good for data analysis, calculations, file conversion."""
    parameters = {"type": "object", "properties": {
        "code": {"type": "string", "description": "Python source to run"},
        "path": {"type": "string", "description": "Existing script to run instead of code"},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Command-line args"},
        "timeout": {"type": "integer", "description": "Seconds (default 300)"},
    }, "required": []}

    def describe_call(self, args):
        if args.get("path"):
            return "run_python(%s)" % args["path"]
        first = (args.get("code") or "").strip().splitlines()[:1]
        return "run_python: %s..." % (first[0][:80] if first else "")

    def permission_key(self, args):
        return "run_python(%s)" % (args.get("path") or "code")

    def run(self, args, ctx):
        if not args.get("code") and not args.get("path"):
            raise ToolError("Provide code or path.")
        if args.get("path"):
            script = ctx.resolve(args["path"])
            if not script.is_file():
                raise ToolError("Script not found: %s" % script)
        else:
            scratch = ctx.config.home / "scratch"
            scratch.mkdir(parents=True, exist_ok=True)
            script = scratch / ("snippet_%s.py" % time.strftime("%Y%m%d_%H%M%S"))
            script.write_text(args["code"], encoding="utf-8")
        cmd = [sys.executable, str(script)] + [str(a) for a in (args.get("args") or [])]
        timeout = int(args.get("timeout") or 300)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
        try:
            r = subprocess.run(cmd, cwd=str(ctx.cwd), capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return "[TIMED OUT after %ds] script: %s" % (timeout, script)
        out = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
        limit = int(ctx.config.get("agent", "max_tool_output_chars", default=16000))
        text = out + (("\n[stderr]\n" + err) if err.strip() else "")
        return "%s\n[exit code %d; script: %s]" % (truncate(text.strip(), limit) or "(no output)", r.returncode, script)


def make_tools(config):
    sh = config.get("shell") or {}
    return [Shell(sh.get("program"), int(sh.get("default_timeout") or 120)), ShellOutput(), RunPython()]
