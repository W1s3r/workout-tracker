import unittest

from hsa.llm.base import extract_tool_calls_from_text, parse_json_loose, to_prompted_messages
from hsa.llm.openai_compat import OpenAICompatClient
from tests.fakes import FakeLLMServer


class TestJsonRepair(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_json_loose('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_loose('```json\n{"a": 1,}\n```'), {"a": 1})
        self.assertEqual(parse_json_loose("Sure! Here: {'a': True, 'b': None} hope that helps"), {"a": True, "b": None})
        self.assertEqual(parse_json_loose('{"a": true, "b": null, "c": [1,2,],}'), {"a": True, "b": None, "c": [1, 2]})
        self.assertEqual(parse_json_loose('{"code": "line1\nline2"}'), {"code": "line1\nline2"})
        with self.assertRaises(ValueError):
            parse_json_loose("no json here at all")


class TestTextToolCalls(unittest.TestCase):
    def test_tagged(self):
        text = 'Let me look.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'
        rest, calls = extract_tool_calls_from_text(text)
        self.assertEqual(rest, "Let me look.")
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "a.py"})

    def test_multiple_and_unclosed(self):
        text = '<tool_call>{"name":"a","arguments":{}}</tool_call><tool_call>{"name":"b","arguments":{"x":1}}'
        _, calls = extract_tool_calls_from_text(text)
        self.assertEqual([c.name for c in calls], ["a", "b"])

    def test_bare_json_known_tool(self):
        _, calls = extract_tool_calls_from_text('{"name": "glob", "arguments": {"pattern": "*.py"}}', known={"glob"})
        self.assertEqual(calls[0].arguments["pattern"], "*.py")
        _, calls = extract_tool_calls_from_text('{"name": "nope", "arguments": {}}', known={"glob"})
        self.assertEqual(calls, [])

    def test_bad_args(self):
        _, calls = extract_tool_calls_from_text('<tool_call>{"name": "x", "arguments": {broken</tool_call>')
        self.assertEqual(calls[0].name, "x")
        self.assertTrue(calls[0].parse_error)

    def test_prompted_conversion(self):
        tools = [{"type": "function", "function": {"name": "t", "description": "d",
                                                   "parameters": {"type": "object", "properties": {"a": {"type": "string"}}}}}]
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
                                                                     "function": {"name": "t", "arguments": '{"a":"b"}'}}]},
                {"role": "tool", "tool_call_id": "1", "name": "t", "content": "res1"},
                {"role": "tool", "tool_call_id": "2", "name": "t", "content": "res2"}]
        out = to_prompted_messages(msgs, tools)
        self.assertIn("<tool_call>", out[0]["content"])
        self.assertIn("* t: d", out[0]["content"])
        self.assertIn('"name": "t"', out[2]["content"])
        self.assertEqual(out[3]["role"], "user")
        self.assertIn("res1", out[3]["content"])
        self.assertIn("res2", out[3]["content"])
        self.assertEqual(len(out), 4)
        self.assertTrue(all(set(m) <= {"role", "content"} for m in out))


def profile(url, **kw):
    p = {"name": "fake", "model": "fake-model", "context_tokens": 8000, "max_output_tokens": 500,
         "tool_mode": "auto", "stream": True,
         "provider_config": {"type": "openai", "base_url": url, "api_key": "k", "max_retries": 0}}
    p.update(kw)
    return p


TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "w",
                                           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]


class TestOpenAIClient(unittest.TestCase):
    def setUp(self):
        self.srv = FakeLLMServer(lambda body: {"content": "Hello <think>secret</think>world",
                                               "tool_calls": [{"name": "get_weather", "arguments": {"city": "Paris"}}]
                                               if body.get("tools") else []})

    def tearDown(self):
        self.srv.close()

    def test_non_stream(self):
        c = OpenAICompatClient(profile(self.srv.url))
        r = c.chat([{"role": "user", "content": "hi"}], tools=TOOLS)
        self.assertEqual(r.tool_calls[0].arguments, {"city": "Paris"})
        self.assertEqual(r.content, "Hello world")
        self.assertEqual(r.reasoning, "secret")
        self.assertEqual(self.srv.requests[-1]["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(c.list_models(), ["fake-model"])

    def test_stream(self):
        c = OpenAICompatClient(profile(self.srv.url))
        chunks = []
        r = c.chat([{"role": "user", "content": "hi"}], tools=TOOLS, stream_cb=lambda k, t: chunks.append(t) if k == "text" else None)
        self.assertTrue(self.srv.requests[-1]["stream"])
        self.assertEqual(r.tool_calls[0].name, "get_weather")
        self.assertEqual(r.tool_calls[0].arguments, {"city": "Paris"})
        self.assertEqual(r.content, "Hello world")
        self.assertNotIn("secret", "".join(chunks))

    def test_prompted_mode(self):
        srv = FakeLLMServer(lambda body: {"content": 'ok <tool_call>{"name": "get_weather", "arguments": {"city": "Rome"}}</tool_call>'})
        try:
            c = OpenAICompatClient(profile(srv.url, tool_mode="prompted", stream=False))
            r = c.chat([{"role": "user", "content": "hi"}], tools=TOOLS)
            self.assertNotIn("tools", srv.requests[-1])
            self.assertIn("get_weather", srv.requests[-1]["messages"][0]["content"])
            self.assertEqual(r.tool_calls[0].arguments, {"city": "Rome"})
            self.assertEqual(r.content, "ok")
        finally:
            srv.close()

    def test_auto_mode_parses_text_calls(self):
        srv = FakeLLMServer(lambda body: {"content": '<tool_call>{"name": "get_weather", "arguments": {"city": "Oslo"}}</tool_call>'})
        try:
            c = OpenAICompatClient(profile(srv.url, stream=False))
            r = c.chat([{"role": "user", "content": "hi"}], tools=TOOLS)
            self.assertEqual(r.tool_calls[0].arguments, {"city": "Oslo"})
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
