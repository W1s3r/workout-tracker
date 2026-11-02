"""`python -m hsa evalloop` - measure how well YOUR model drives the agent loop.

Runs a set of small scenarios in a throw-away folder (auto-approve mode) and
checks behaviour: does it answer chat without tools, finish multi-step tasks,
verify code, stop when blocked, avoid loops? Use it to compare models and
settings, e.g.:
    python -m hsa evalloop
    python -m hsa evalloop --model gpt-oss
    python -m hsa evalloop --completion finish_tool      # compare ending styles
    python -m hsa evalloop --only fix_bug --verbose
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Tuple

Check = Callable[[Path, str, object], Tuple[bool, str]]


def _file_has(name, *needles):
    def check(d, out, st):
        p = d / name
        if not p.exists():
            return False, "%s was not created" % name
        txt = p.read_text(errors="replace")
        missing = [n for n in needles if n not in txt]
        return (not missing), ("missing %s in %s" % (missing, name) if missing else "ok")
    return check


def _answer_has(*needles):
    def check(d, out, st):
        low = out.lower()
        missing = [n for n in needles if n.lower() not in low]
        return (not missing), ("answer missing %s" % missing if missing else "ok")
    return check


def _no_tools(d, out, st):
    return (not st.tools_used), ("used tools %s" % st.tools_used if st.tools_used else "ok")


def _used_any(*names):
    def check(d, out, st):
        ok = any(n in st.tools_used for n in names)
        return ok, ("ok" if ok else "never used any of %s" % (names,))
    return check


def _max_steps(n):
    def check(d, out, st):
        return st.steps <= n, ("%d steps" % st.steps)
    return check


def _ran_after_edit(d, out, st):
    tools = st.tools_used
    last_edit = max([i for i, t in enumerate(tools) if t in ("write_file", "edit_file")], default=-1)
    if last_edit < 0:
        return False, "never edited any file"
    ran = any(t in ("shell", "run_python") for t in tools[last_edit + 1:])
    return ran, "ok" if ran else "edited code but never ran it afterwards"


def _test_passes(d, out, st):
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "test_calc.py"], cwd=str(d), capture_output=True, timeout=60)
    return r.returncode == 0, ("tests pass" if r.returncode == 0 else r.stdout.decode()[-300:] + r.stderr.decode()[-300:])


def _setup_calc(d: Path):
    (d / "calc.py").write_text("def average(nums):\n    return sum(nums) / (len(nums) + 1)\n")
    (d / "test_calc.py").write_text("from calc import average\nassert average([2, 4, 6]) == 4, average([2, 4, 6])\n"
                                    "print('ok')\n")


def _setup_zebra(d: Path):
    (d / "notes").mkdir()
    (d / "notes" / "a.txt").write_text("apples and pears\n")
    (d / "notes" / "b.txt").write_text("the zebra crossed the road\n")
    (d / "notes" / "c.txt").write_text("nothing here\n")


SCENARIOS = [
    {"name": "greeting", "prompt": "hi there!", "checks": [_no_tools, _max_steps(1)]},
    {"name": "knowledge", "prompt": "In one or two sentences: what is the difference between a Python list and a tuple?",
     "checks": [_no_tools, _answer_has("mutable")]},
    {"name": "create_and_run", "prompt": "Create a file named hello.py that prints Hello HSA, then run it to confirm it works.",
     "checks": [_file_has("hello.py", "Hello HSA"), _ran_after_edit, _answer_has("hello")]},
    {"name": "find_content", "prompt": "Which file under the notes folder mentions a zebra? Just tell me the file name.",
     "setup": _setup_zebra, "checks": [_used_any("grep", "read_file", "shell", "glob", "find_files"), _answer_has("b.txt")]},
    {"name": "multi_step", "prompt": ("Create fruits.txt with three lines: apple, banana, cherry. Then append a fourth "
                                      "line: date. Finally tell me how many lines the file has."),
     "checks": [_file_has("fruits.txt", "apple", "banana", "cherry", "date"), _answer_has("4")]},
    {"name": "fix_bug", "prompt": "test_calc.py fails. Fix the bug in calc.py so the test passes, and run the test to confirm.",
     "setup": _setup_calc, "checks": [_test_passes, _ran_after_edit]},
    {"name": "blocked", "prompt": "Summarize the file quarterly_report_9931.txt in this folder.",
     "checks": [_max_steps(8), _answer_has("not")]},
]


def run_evals(app, only: List[str] = None, verbose: bool = False) -> int:
    from hsa.agent import Agent
    from hsa.permissions import Permissions
    from hsa.ui import QuietUI
    app.permissions.mode = "auto"
    results = []
    print("Loop eval: model=%s completion=%s" % (app.llm.name, app.config.get("agent", "completion")))
    print("-" * 72)
    for sc in SCENARIOS:
        if only and sc["name"] not in only:
            continue
        d = Path(tempfile.mkdtemp(prefix="hsa_eval_"))
        try:
            if sc.get("setup"):
                sc["setup"](d)
            ui = app.ui if verbose else QuietUI(app.ui)
            agent = Agent(app.config, app.llm, ui, Permissions(app.config, ui), cwd=d, llm_factory=app.client)
            agent.permissions.mode = "auto"
            t0 = time.time()
            err = ""
            try:
                out = agent.run_turn(sc["prompt"], quiet=not verbose)
            except Exception as e:  # report, keep going
                out, err = "", "%s: %s" % (type(e).__name__, e)
            st = agent.last_turn
            fails = []
            for chk in sc["checks"]:
                ok, why = chk(d, out or "", st)
                if not ok:
                    fails.append(why)
            if err:
                fails.append(err)
            passed = not fails
            results.append(passed)
            print("%-15s %s  steps=%-2d nudges=%-24s ended=%-10s %4.0fs" % (
                sc["name"], "PASS" if passed else "FAIL", st.steps, ",".join(st.nudges) or "-", st.ended_by,
                time.time() - t0))
            for f in fails:
                print("    - " + f)
            if fails and not verbose:
                print("    answer: %s" % (out or "").strip().replace("\n", " ")[:200])
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print("-" * 72)
    print("%d/%d passed" % (sum(results), len(results)))
    print("Tips: compare --completion text vs finish_tool; try tool_mode prompted vs auto; lower temperature;\n"
          "      use --verbose --only NAME to watch a failing scenario.")
    return 0 if all(results) else 1
