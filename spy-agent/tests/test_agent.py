import json
import unittest

from spy.agent import Agent
from spy.llm import make_client
from spy.permissions import Permissions
from tests.fakes import FakeLLMServer
from tests.helpers import ScriptedUI, make_config, tmpdir


def script_responder(steps):
    """Return responses in order; each step may be a dict or callable(body)->dict."""
    steps = list(steps)

    def r(body):
        s = steps.pop(0) if steps else {"content": "done"}
        return s(body) if callable(s) else s
    return r


class AgentTest(unittest.TestCase):
    def make(self, steps, mode="default", answers=None, stream=True, **cfg_over):
        self.dir = tmpdir()
        self.srv = FakeLLMServer(script_responder(steps))
        self.addCleanup(self.srv.close)
        over = {"providers": {"work": {"base_url": self.srv.url, "api_key": "k", "max_retries": 0}},
                "models": {"inkling-small": {"model": "fake-model", "stream": stream, "context_tokens": 32000,
                                             "max_output_tokens": 500}},
                "permissions": {"mode": mode, "allow": [], "deny": ["shell(rm -rf*)"]}}
        over.update(cfg_over)
        self.cfg = make_config(self.dir, **over)
        self.ui = ScriptedUI(answers)
        llm = make_client(self.cfg.model_profile("main"))
        return Agent(self.cfg, llm, self.ui, Permissions(self.cfg, self.ui), cwd=self.dir)

    def test_write_then_answer(self):
        a = self.make([
            {"content": "I'll create it.", "tool_calls": [{"name": "write_file", "arguments": {"path": "hello.py", "content": "print('hi')\n"}}]},
            {"tool_calls": [{"name": "run_python", "arguments": {"path": "hello.py"}}]},
            {"content": "Created hello.py and it prints hi."},
        ], answers=[("yes", ""), ("yes", "")])
        out = a.run_turn("make hello.py")
        self.assertIn("prints hi", out)
        self.assertEqual((self.dir / "hello.py").read_text(), "print('hi')\n")
        self.assertEqual(len(self.ui.asked), 2)
        tool_msgs = [m for m in a.messages if m["role"] == "tool"]
        self.assertIn("hi", tool_msgs[1]["content"])
        # the follow-up request carried tool results with matching ids
        last = self.srv.requests[-1]["messages"]
        self.assertEqual(last[-1]["role"], "tool")
        self.assertEqual(last[-1]["tool_call_id"], last[-2]["tool_calls"][0]["id"])
        self.assertEqual(last[0]["role"], "system")

    def test_denied_with_feedback(self):
        a = self.make([
            {"tool_calls": [{"name": "shell", "arguments": {"command": "echo hi"}}]},
            lambda body: {"content": "OK: " + body["messages"][-1]["content"]},
        ], answers=[("no", "use python instead")])
        out = a.run_turn("say hi")
        self.assertIn("use python instead", out)

    def test_deny_rule_and_plan_mode(self):
        a = self.make([
            {"tool_calls": [{"name": "shell", "arguments": {"command": "rm -rf /tmp/x"}}]},
            {"content": "blocked"},
        ], mode="auto")
        a.run_turn("delete")
        self.assertIn("deny rule", [m for m in a.messages if m["role"] == "tool"][0]["content"])
        b = self.make([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "z", "content": "z"}}]},
            {"content": "Here is my plan"},
        ], mode="plan")
        b.run_turn("do it")
        self.assertIn("Plan mode", [m for m in b.messages if m["role"] == "tool"][0]["content"])
        self.assertFalse((self.dir / "z").exists())
        self.assertIn("PLAN MODE", self.srv.requests[0]["messages"][0]["content"])

    def test_robustness(self):
        a = self.make([
            {"tool_calls": [{"name": "Bash", "arguments": {"command": "echo alias-ok"}}]},       # alias
            {"tool_calls": [{"name": "frobnicate", "arguments": {}}]},                            # unknown
            {"tool_calls": [{"name": "read_file", "arguments": {}}]},                             # missing arg
            {"content": ""},                                                                      # empty -> nudge
            {"content": "finished"},
        ], mode="auto")
        out = a.run_turn("go")
        self.assertEqual(out, "finished")
        tools = [m["content"] for m in a.messages if m["role"] == "tool"]
        self.assertIn("alias-ok", tools[0])
        self.assertIn("Unknown tool", tools[1])
        self.assertIn("Missing required", tools[2])
        self.assertTrue(any("empty" in m["content"] for m in a.messages if m["role"] == "user"))

    def test_repeat_detection(self):
        call = {"tool_calls": [{"name": "list_dir", "arguments": {}}]}
        a = self.make([call, call, call, {"content": "ok"}], mode="auto")
        a.run_turn("loop")
        self.assertIn("exact call 3 times", [m for m in a.messages if m["role"] == "tool"][2]["content"])

    def test_subagent(self):
        (lambda: None)()
        a = self.make([
            {"tool_calls": [{"name": "task", "arguments": {"description": "find", "prompt": "find the secret file",
                                                           "agent_type": "explore"}}]},
            # sub-agent's steps
            lambda body: {"tool_calls": [{"name": "write_file", "arguments": {"path": "x", "content": "x"}}]}
            if any(t["function"]["name"] == "write_file" for t in body.get("tools", [])) else
            {"tool_calls": [{"name": "glob", "arguments": {"pattern": "*.secret"}}]},
            {"content": "SUBREPORT: found s.secret"},
            # main agent continues
            lambda body: {"content": "Main got: " + body["messages"][-1]["content"]},
        ], mode="auto")
        (self.dir / "s.secret").write_text("x")
        out = a.run_turn("delegate")
        self.assertIn("SUBREPORT", out)
        # explore sub-agent had no write tools
        sub_req = self.srv.requests[1]
        names = {t["function"]["name"] for t in sub_req["tools"]}
        self.assertNotIn("write_file", names)
        self.assertNotIn("task", names)
        self.assertIn("sub-agent", sub_req["messages"][0]["content"])

    def test_compaction(self):
        big = "x" * 9000
        a = self.make([{"content": "summary of earlier work: edited foo.py"}, {"content": "continuing"}], mode="auto")
        for i in range(12):
            a.messages += [{"role": "user", "content": "q%d %s" % (i, big)}, {"role": "assistant", "content": "a%d" % i}]
        a.run_turn("next")
        self.assertTrue(a.messages[0]["content"].startswith("[Summary of earlier conversation]"))
        self.assertIn("edited foo.py", a.messages[0]["content"])
        self.assertEqual(a.messages[-1]["content"], "continuing")

    def test_prompted_tool_mode_end_to_end(self):
        a = self.make([
            {"content": 'Reading.\n<tool_call>\n{"name": "list_dir", "arguments": {"path": "."}}\n</tool_call>'},
            lambda body: {"content": "saw: " + ("tool_result" in body["messages"][-1]["content"] and "yes" or "no")},
        ], mode="auto", stream=False)
        a.llm.tool_mode = "prompted"
        out = a.run_turn("list")
        self.assertEqual(out, "saw: yes")
        self.assertNotIn("tools", self.srv.requests[0])


if __name__ == "__main__":
    unittest.main()
