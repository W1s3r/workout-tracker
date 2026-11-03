import io
import os
import unittest
import zipfile
from unittest import mock

from spy.context import Memory, load_context
from spy.extract import extract_any
from spy.tools.base import ToolContext, ToolError, validate_args
from spy.tools.files import EditFile, FindFiles, Glob, Grep, ListDir, ReadFile, WriteFile
from spy.tools.shell import RunPython, Shell
from tests.helpers import ScriptedUI, make_config, tmpdir


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.cfg = make_config(self.dir, locations={"share": str(self.dir)})
        self.ctx = ToolContext(config=self.cfg, ui=ScriptedUI(), cwd=self.dir)


class TestFileTools(Base):
    def test_write_read_edit(self):
        WriteFile().run({"path": "a/b.py", "content": "def f():\n    return 1\n"}, self.ctx)
        out = ReadFile().run({"path": "a/b.py"}, self.ctx)
        self.assertIn("     2\t    return 1", out)
        res = EditFile().run({"path": "a/b.py", "old_string": "return 1", "new_string": "return 2"}, self.ctx)
        self.assertIn("Edited", res)
        self.assertIn("return 2", (self.dir / "a/b.py").read_text())

    def test_edit_requires_read(self):
        (self.dir / "x.txt").write_text("hello")
        with self.assertRaises(ToolError):
            EditFile().run({"path": "x.txt", "old_string": "hello", "new_string": "bye"}, self.ctx)

    def test_write_existing_requires_read(self):
        (self.dir / "x.txt").write_text("hello")
        with self.assertRaises(ToolError):
            WriteFile().run({"path": "x.txt", "content": "new"}, self.ctx)

    def test_edit_forgiving(self):
        p = self.dir / "c.py"
        p.write_text("class A:\n    def m(self):\n        pass\n")
        ReadFile().run({"path": "c.py"}, self.ctx)
        # model copied line-number prefixes
        EditFile().run({"path": "c.py", "old_string": "     3\t        pass", "new_string": "        return 5"}, self.ctx)
        self.assertIn("return 5", p.read_text())
        # wrong indentation
        EditFile().run({"path": "c.py", "old_string": "def m(self):\n  return 5", "new_string": "    def m(self):\n        return 6"}, self.ctx)
        self.assertIn("return 6", p.read_text())
        with self.assertRaises(ToolError) as e:
            EditFile().run({"path": "c.py", "old_string": "def mm(selff):", "new_string": "x"}, self.ctx)
        self.assertIn("closest match", str(e.exception))

    def test_edit_ambiguous_and_crlf(self):
        p = self.dir / "w.txt"
        p.write_bytes(b"x = 1\r\nx = 1\r\n")
        ReadFile().run({"path": "w.txt"}, self.ctx)
        with self.assertRaises(ToolError):
            EditFile().run({"path": "w.txt", "old_string": "x = 1", "new_string": "x = 2"}, self.ctx)
        EditFile().run({"path": "w.txt", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}, self.ctx)
        self.assertEqual(p.read_bytes(), b"x = 2\r\nx = 2\r\n")

    def test_read_suggests(self):
        (self.dir / "report_final.txt").write_text("x")
        with self.assertRaises(ToolError) as e:
            ReadFile().run({"path": "report_finl.txt"}, self.ctx)
        self.assertIn("Did you mean", str(e.exception))

    def test_list_glob_grep_find(self):
        (self.dir / "src").mkdir()
        (self.dir / "src" / "main.py").write_text("import os\nTODO = 1\n")
        (self.dir / "src" / "app.js").write_text("// TODO later\n")
        (self.dir / "node_modules").mkdir()
        (self.dir / "node_modules" / "junk.js").write_text("TODO")
        self.assertIn("src/", ListDir().run({"depth": 2}, self.ctx))
        g = Glob().run({"pattern": "**/*.{py,js}"}, self.ctx)
        self.assertIn("main.py", g)
        self.assertIn("app.js", g)
        self.assertNotIn("junk.js", g)
        with mock.patch("shutil.which", return_value=None):  # force pure-python grep
            out = Grep().run({"pattern": "TODO", "output_mode": "content"}, self.ctx)
            self.assertIn("main.py:2:TODO = 1", out)
            self.assertNotIn("junk", out)
            out = Grep().run({"pattern": "todo", "ignore_case": True, "glob": "*.js"}, self.ctx)
            self.assertIn("app.js", out)
            self.assertNotIn("main.py", out)
        self.assertIn("main.py", FindFiles().run({"query": "MAIN"}, self.ctx))
        self.assertIn("share:", FindFiles().run({}, self.ctx))

    def test_docx_extraction(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                       '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Title</w:t></w:r></w:p>'
                       '<w:p><w:r><w:t>Body text</w:t></w:r></w:p></w:body></w:document>')
        (self.dir / "d.docx").write_bytes(buf.getvalue())
        out = ReadFile().run({"path": "d.docx"}, self.ctx)
        self.assertIn("# Title", out)
        self.assertIn("Body text", out)

    def test_html_extraction(self):
        t, text, links = extract_any(b"<html><title>T</title><script>x()</script><nav>menu</nav><h2>Head</h2>"
                                     b"<p>Para <a href='/x'>link</a></p></html>", "p.html", base_url="http://h/a/")
        self.assertEqual(t, "T")
        self.assertIn("## Head", text)
        self.assertNotIn("x()", text)
        self.assertNotIn("menu", text)
        self.assertEqual(links, [("http://h/x", "link")])

    def test_validate_args_coercion(self):
        args = {"path": "a", "offset": "5", "limit": 10.0}
        self.assertIsNone(validate_args(ReadFile(), args))
        self.assertEqual(args["offset"], 5)
        self.assertIn("Missing", validate_args(ReadFile(), {}))


class TestShell(Base):
    def test_shell_cwd_persists(self):
        if os.name == "nt":
            self.skipTest("posix test")
        sh = Shell()
        (self.dir / "sub").mkdir()
        out = sh.run({"command": "cd sub && echo hi"}, self.ctx)
        self.assertIn("hi", out)
        self.assertEqual(self.ctx.cwd.name, "sub")
        out = sh.run({"command": "exit 3"}, self.ctx)
        self.assertIn("exit code 3", out)
        out = sh.run({"command": "sleep 5", "timeout": 1}, self.ctx)
        self.assertIn("TIMED OUT", out)

    def test_run_python(self):
        out = RunPython().run({"code": "import sys\nprint(6*7)\nprint('e', file=sys.stderr)"}, self.ctx)
        self.assertIn("42", out)
        self.assertIn("[stderr]", out)


class TestMemory(Base):
    def test_add_and_load(self):
        m = Memory()
        m.run({"action": "add", "scope": "project", "note": "Run tests with pytest -q", "section": "Commands"}, self.ctx)
        m.run({"action": "add", "scope": "project", "note": "Share is at \\\\srv\\x"}, self.ctx)
        text = (self.dir / "SPY.md").read_text()
        cmd_idx = text.index("## Commands")
        self.assertLess(cmd_idx, text.index("- Run tests with pytest -q"))
        self.assertLess(text.index("- Run tests with pytest -q"), text.index("## Conventions"))
        self.assertIn("- Share is at", text.split("## Lessons learned")[1])
        m.run({"action": "replace", "scope": "project", "old_text": "pytest -q", "new_text": "pytest -x"}, self.ctx)
        ctx_text, files = load_context(self.cfg, self.dir)
        self.assertIn("pytest -x", ctx_text)
        # imports
        (self.dir / "extra.md").write_text("IMPORTED FACT")
        with open(self.dir / "SPY.md", "a") as f:
            f.write("\n@extra.md\n")
        self.assertIn("IMPORTED FACT", load_context(self.cfg, self.dir)[0])


if __name__ == "__main__":
    unittest.main()
