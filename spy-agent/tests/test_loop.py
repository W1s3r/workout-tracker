"""Tests for the turn loop: chat vs task, auto-continue nudges, finish tool,
step limit, stuck detection, style prompt and response cleanup."""
import unittest

from spy.mdrender import render
from spy.style import clean_response, style_prompt
from spy.turn_policy import (AUTO, CHAT, TASK, announces_without_acting, asks_permission_to_proceed, asks_user,
                             route)
from tests import test_agent


class TestRouting(unittest.TestCase):
    def test_route(self):
        for t in ["hi", "Thanks!", "thank you so much", "good morning", "cool"]:
            self.assertEqual(route(t), CHAT, t)
        # short answers depend on what came before
        self.assertEqual(route("yes", "I found 3 old logs. Shall I delete them?"), TASK)
        self.assertEqual(route("the second one", "Which config do you mean?"), TASK)
        self.assertEqual(route("ok", "Done - tests pass.", prev_engaged=True), TASK)
        self.assertNotEqual(route("ok", "Hello! How can I help?"), CHAT)
        self.assertEqual(route("ok", "Hello."), AUTO)
        self.assertEqual(route("continue"), TASK)
        self.assertEqual(route("go ahead and do it"), TASK)
        for t in ["fix the failing test", "Can you create a script that renames files?", "please run the build",
                  "what's in config.json?", r"look at \\server\share\docs", "search the intranet for falcon"]:
            self.assertEqual(route(t), TASK, t)
        for t in ["what is the difference between a list and a tuple?", "why is the sky blue",
                  "hi, can you tell me how our deploy works"]:
            self.assertEqual(route(t), AUTO, t)

    def test_text_analysis(self):
        self.assertTrue(announces_without_acting("Found the bug. I'll now update the config file."))
        self.assertTrue(announces_without_acting("Next, I will run the tests:"))
        self.assertTrue(announces_without_acting("Let me check the logs."))
        self.assertFalse(announces_without_acting("I updated the config file and the tests pass."))
        self.assertFalse(announces_without_acting("I'll need the server name. Which server should I use?"))
        self.assertTrue(asks_permission_to_proceed("Here's the plan. Shall I proceed?"))
        self.assertTrue(asks_permission_to_proceed("Would you like me to make these changes?"))
        self.assertFalse(asks_permission_to_proceed("Which of the two files is the right one?"))
        self.assertTrue(asks_user("Which of the two files is the right one?"))
        self.assertTrue(asks_user("Please provide the path to the share."))


class TestStyle(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(clean_response("Assistant: Hello"), "Hello")
        self.assertEqual(clean_response("```markdown\n# Title\nbody\n```"), "# Title\nbody")
        self.assertEqual(clean_response("Done.\n\nLet me know if you need anything else!"), "Done.")
        self.assertEqual(clean_response("Answer <|end|>"), "Answer")
        keep = "```python\nprint(1)\n```"
        self.assertEqual(clean_response(keep), keep)

    def test_render(self):
        out = render("# Title\n- **bold** item\n```py\nx = 1\n```", color=False)
        self.assertIn("TITLE", out)
        self.assertIn("• bold item", out)
        self.assertIn("  x = 1", out)


ECHO_LAST = lambda body: {"content": "seen: " + body["messages"][-1]["content"][:120]}  # noqa: E731


class TestLoop(unittest.TestCase):
    make = test_agent.AgentTest.make

    def test_chat_gets_no_tools(self):
        a = self.make([{"content": "Hello! How can I help?"}])
        out = a.run_turn("hi")
        self.assertEqual(out, "Hello! How can I help?")
        self.assertNotIn("tools", self.srv.requests[0])
        self.assertEqual(a.last_turn.steps, 1)

    def test_yes_after_question_gets_tools(self):
        a = self.make([
            {"tool_calls": [{"name": "list_dir", "arguments": {}}]},
            {"content": "I found two files. Shall I delete them?"},
            {"tool_calls": [{"name": "list_dir", "arguments": {"depth": 2}}]},
            {"content": "Deleted."},
        ], mode="auto")
        a.run_turn("clean up the folder")
        out = a.run_turn("yes")
        self.assertEqual(a.last_turn.route, TASK)
        self.assertIn("tools", self.srv.requests[-1])

    def test_question_answered_directly(self):
        a = self.make([{"content": "A list is mutable; a tuple is not."}])
        out = a.run_turn("what is the difference between a list and a tuple?")
        self.assertIn("mutable", out)
        self.assertIn("tools", self.srv.requests[0])
        self.assertEqual(a.last_turn.nudges, [])

    def test_announce_without_acting_is_nudged(self):
        a = self.make([
            {"content": "Sure. I'll start by listing the files."},
            {"tool_calls": [{"name": "list_dir", "arguments": {}}]},
            {"content": "The folder is empty."},
        ], mode="auto")
        out = a.run_turn("look around this folder and tell me what's here")
        self.assertEqual(out, "The folder is empty.")
        self.assertEqual(a.last_turn.nudges, ["announced"])

    def test_permission_question_is_nudged_in_task(self):
        a = self.make([
            {"tool_calls": [{"name": "list_dir", "arguments": {}}]},
            {"content": "I found the folder. Shall I proceed with creating the file?"},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}]},
            {"content": "Created a.txt."},
        ], mode="auto")
        out = a.run_turn("create a.txt containing x")
        self.assertEqual(out, "Created a.txt.")
        self.assertIn("permission", a.last_turn.nudges)

    def test_genuine_question_ends_turn(self):
        a = self.make([
            {"tool_calls": [{"name": "list_dir", "arguments": {}}]},
            {"content": "There are two config files. Which one do you mean, dev or prod?"},
        ], mode="auto")
        out = a.run_turn("update the config")
        self.assertIn("Which one", out)
        self.assertEqual(a.last_turn.nudges, [])

    def test_unfinished_todos_nudged_then_cap(self):
        todo = {"tool_calls": [{"name": "todo_write", "arguments": {"todos": [
            {"content": "step one", "status": "completed"}, {"content": "step two", "status": "pending"}]}}]}
        a = self.make([todo, {"content": "Step one is done."}, {"content": "Done with step one."},
                       {"content": "Still just step one."}], mode="auto")
        out = a.run_turn("do the two steps")
        self.assertEqual(a.last_turn.nudges, ["todos", "todos"])  # capped at 2
        self.assertEqual(out, "Still just step one.")
        self.assertIn("step two", [m for m in a.messages if m.get("_auto")][0]["content"])

    def test_verify_nudge_after_code_edit(self):
        a = self.make([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "s.py", "content": "print(1)\n"}}]},
            {"content": "Wrote s.py."},
            {"tool_calls": [{"name": "run_python", "arguments": {"path": "s.py"}}]},
            {"content": "Wrote s.py and ran it: prints 1."},
        ], mode="auto")
        out = a.run_turn("write s.py that prints 1")
        self.assertIn("ran it", out)
        self.assertEqual(a.last_turn.nudges, ["verify"])

    def test_no_verify_nudge_for_docs(self):
        a = self.make([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "n.md", "content": "# hi\n"}}]},
            {"content": "Wrote n.md."},
        ], mode="auto")
        a.run_turn("write n.md")
        self.assertEqual(a.last_turn.nudges, [])

    def test_finish_tool_mode(self):
        a = self.make([
            {"tool_calls": [{"name": "list_dir", "arguments": {}}]},
            {"content": "Listed it."},
            {"tool_calls": [{"name": "finish", "arguments": {"status": "done", "summary": "All **done**."}}]},
        ], mode="auto", agent={"completion": "finish_tool"})
        out = a.run_turn("list the folder")
        self.assertEqual(out, "All **done**.")
        self.assertEqual(a.last_turn.ended_by, "finish")
        self.assertEqual(a.last_turn.nudges, ["finish"])
        self.assertIn("finish", {t["function"]["name"] for t in self.srv.requests[0]["tools"]})
        # history is valid: finish call answered, then assistant summary
        self.assertEqual(a.messages[-2]["role"], "tool")
        self.assertEqual(a.messages[-1]["content"], "All **done**.")

    def test_finish_not_offered_in_text_mode(self):
        a = self.make([{"content": "ok"}])
        a.run_turn("what is 2+2")
        self.assertNotIn("finish", {t["function"]["name"] for t in self.srv.requests[0]["tools"]})

    def test_step_limit_summary(self):
        call = lambda body: {"tool_calls": [{"name": "list_dir", "arguments": {"depth": len(body["messages"])}}]}  # noqa
        a = self.make([call] * 3 + [{"content": "Done: listed. Remaining: nothing else."}], mode="auto",
                      agent={"max_steps_per_turn": 3})
        out = a.run_turn("keep listing")
        self.assertEqual(a.last_turn.ended_by, "step_limit")
        self.assertIn("Remaining", out)
        self.assertNotIn("tools", self.srv.requests[-1])

    def test_stuck_note(self):
        bad = {"tool_calls": [{"name": "read_file", "arguments": {"path": "nope%d.txt"}}]}
        steps = [{"tool_calls": [{"name": "read_file", "arguments": {"path": "nope%d.txt" % i}}]} for i in range(4)]
        a = self.make(steps + [{"content": "The files don't exist; I am unable to find them."}], mode="auto")
        a.run_turn("read the nope files")
        tool_msgs = [m["content"] for m in a.messages if m["role"] == "tool"]
        self.assertIn("Stop and think", tool_msgs[3])
        self.assertNotIn("Stop and think", tool_msgs[2])

    def test_style_in_system_prompt_and_cleanup(self):
        a = self.make([{"content": "Assistant: Bonjour!\n\nLet me know if you need anything else!"}],
                      style={"language": "French", "name": "bluf"})
        out = a.run_turn("explain recursion")
        self.assertEqual(out, "Bonjour!")
        sysmsg = self.srv.requests[0]["messages"][0]["content"]
        self.assertIn("respond in French", sysmsg)
        self.assertIn("BLUF", sysmsg)

    def test_reasoning_exhausted_budget_is_retried_bigger(self):
        a = self.make([{"content": "", "reasoning": "thinking " * 50, "finish_reason": "length"},
                       {"content": "The answer is 42."}], stream=False)
        out = a.run_turn("what is six times seven")
        self.assertEqual(out, "The answer is 42.")
        self.assertEqual(self.srv.requests[1]["max_tokens"], self.srv.requests[0]["max_tokens"] * 2)
        self.assertEqual(a.last_turn.nudges, [])

    def test_truncated_reply_continues(self):
        a = self.make([{"content": "part one "}, {"content": "part two"}])
        # simulate finish_reason=length via the policy directly
        from spy.turn_policy import TurnState, decide_after_text
        st = TurnState(route=AUTO)
        self.assertIsNotNone(decide_after_text(st, "part one", "length", [], {}))


if __name__ == "__main__":
    unittest.main()
