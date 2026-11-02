"""The 'model replaced my whole file with the snippet it meant to change' failure,
and other ways an edit can silently destroy content."""
import io
import re
import unittest
import zipfile

from spy.extract import docx_to_text
from spy.permissions import Permissions
from spy.tools.base import ToolContext, ToolError
from spy.tools.files import EditFile, ReadFile, WriteFile
from tests import test_agent
from tests.helpers import ScriptedUI, make_config, tmpdir

BIG = "".join("def func_%d(x):\n    return x + %d\n\n" % (i, i) for i in range(40))  # 120 lines


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.cfg = make_config(self.dir)
        self.ui = ScriptedUI()
        self.ctx = ToolContext(config=self.cfg, ui=self.ui, cwd=self.dir)
        self.f = self.dir / "mod.py"
        self.f.write_text(BIG)
        ReadFile().run({"path": "mod.py"}, self.ctx)


class TestWriteGuards(Base):
    def test_snippet_overwrite_refused(self):
        snippet = "def func_7(x):\n    return x * 700\n"
        with self.assertRaises(ToolError) as e:
            WriteFile().run({"path": "mod.py", "content": snippet}, self.ctx)
        self.assertIn("edit_file", str(e.exception))
        self.assertEqual(self.f.read_text(), BIG)  # untouched

    def test_placeholder_refused(self):
        for ph in ["# ... rest of the code unchanged ...", "// ... existing code ...", "# (rest of file remains the same)",
                   "    ...", "<!-- remaining content unchanged -->"]:
            content = BIG.replace("def func_39", ph + "\ndef func_39")
            with self.assertRaises(ToolError, msg=ph):
                WriteFile().run({"path": "mod.py", "content": content}, self.ctx)
        self.assertEqual(self.f.read_text(), BIG)

    def test_line_number_prefixes_refused(self):
        numbered = ReadFile().run({"path": "mod.py"}, self.ctx)
        with self.assertRaises(ToolError) as e:
            WriteFile().run({"path": "mod.py", "content": numbered}, self.ctx)
        self.assertIn("line-number", str(e.exception))

    def test_full_rewrite_allowed_but_confirmed_even_in_auto(self):
        tool = WriteFile()
        args = {"path": "mod.py", "content": "print('new program')\n", "full_rewrite": True}
        self.cfg.data["permissions"]["mode"] = "auto"
        perms = Permissions(self.cfg, self.ui)
        self.ui.answers = [("no", "keep the functions")]
        ok, msg = perms.check(tool, args, self.ctx)
        self.assertFalse(ok)
        self.assertIn("keep the functions", msg)
        self.assertEqual(len(self.ui.asked), 1)
        self.ui.answers = [("yes", "")]
        ok, _ = perms.check(tool, args, self.ctx)
        self.assertTrue(ok)
        tool.run(args, self.ctx)
        self.assertEqual(self.f.read_text(), "print('new program')\n")

    def test_full_file_rewrite_with_complete_content_ok(self):
        content = BIG.replace("return x + 5\n", "return x + 500\n")
        out = WriteFile().run({"path": "mod.py", "content": content}, self.ctx)
        self.assertIn("1 removed", out)
        self.assertEqual(self.f.read_text(), content)

    def test_office_files_refused(self):
        with self.assertRaises(ToolError):
            WriteFile().run({"path": "report.docx", "content": "hello"}, self.ctx)


class TestPreciseEdits(Base):
    def test_edit_changes_only_target(self):
        out = EditFile().run({"path": "mod.py", "old_string": "def func_7(x):\n    return x + 7",
                              "new_string": "def func_7(x):\n    return x * 700"}, self.ctx)
        self.assertIn("rest of the file is unchanged", out)
        self.assertEqual(self.f.read_text(), BIG.replace("return x + 7\n", "return x * 700\n"))

    def test_new_string_placeholder_refused(self):
        with self.assertRaises(ToolError):
            EditFile().run({"path": "mod.py", "old_string": "def func_7(x):\n    return x + 7",
                            "new_string": "def func_7(x):\n    # ... rest of code unchanged\n    return x * 7"}, self.ctx)
        self.assertEqual(self.f.read_text(), BIG)

    def test_wrong_indent_is_reindented(self):
        f = self.dir / "cls.py"
        f.write_text("class A:\n    def m(self):\n        a = 1\n        return a\n\n    def n(self):\n        return 2\n")
        ReadFile().run({"path": "cls.py"}, self.ctx)
        EditFile().run({"path": "cls.py", "old_string": "def m(self):\n    a = 1\n    return a",
                        "new_string": "def m(self):\n    a = 10\n    return a * 2"}, self.ctx)
        self.assertEqual(f.read_text(), "class A:\n    def m(self):\n        a = 10\n        return a * 2\n\n"
                                        "    def n(self):\n        return 2\n")

    def test_encoding_crlf_and_trailing_newline_preserved(self):
        f = self.dir / "w.txt"
        f.write_bytes("première ligne\r\nzweite Zeile\r\nlast\r\n".encode("utf-8"))
        ReadFile().run({"path": "w.txt"}, self.ctx)
        EditFile().run({"path": "w.txt", "old_string": "zweite Zeile", "new_string": "second line"}, self.ctx)
        self.assertEqual(f.read_bytes(), "première ligne\r\nsecond line\r\nlast\r\n".encode("utf-8"))

    def test_large_deletion_by_edit_asks_user(self):
        body = BIG[: BIG.index("def func_35")]
        args = {"path": "mod.py", "old_string": body, "new_string": ""}
        self.cfg.data["permissions"]["mode"] = "accept_edits"
        self.ui.answers = [("no", "")]
        ok, _ = Permissions(self.cfg, self.ui).check(EditFile(), args, self.ctx)
        self.assertFalse(ok)
        self.assertEqual(len(self.ui.asked), 1)
        # a normal small edit is not asked about in accept_edits
        ok, _ = Permissions(self.cfg, self.ui).check(EditFile(), {"path": "mod.py", "old_string": "return x + 7",
                                                                  "new_string": "return 7"}, self.ctx)
        self.assertTrue(ok)
        self.assertEqual(len(self.ui.asked), 1)

    def test_undo(self):
        EditFile().run({"path": "mod.py", "old_string": "return x + 7", "new_string": "return 7"}, self.ctx)
        WriteFile().run({"path": "new.py", "content": "x = 1\n"}, self.ctx)
        self.assertIn("removed newly created", self.ctx.history.undo())
        self.assertFalse((self.dir / "new.py").exists())
        self.assertIn("restored", self.ctx.history.undo())
        self.assertEqual(self.f.read_text(), BIG)
        self.assertEqual(self.ctx.history.undo(), "Nothing to undo.")


def make_docx() -> bytes:
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ' \
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" mc:Ignorable="w14"'
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document %s><w:body>'
           '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Quarterly Report</w:t></w:r></w:p>'
           '<w:p w14:paraId="1A"><w:r><w:t xml:space="preserve">The budget is </w:t></w:r>'
           '<w:r><w:rPr><w:b/></w:rPr><w:t>$4.</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>2M</w:t></w:r>'
           '<w:r><w:t xml:space="preserve"> for FY26 &amp; beyond.</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>Status: on track.</w:t></w:r></w:p>'
           '</w:body></w:document>') % W
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", "<w:styles %s>STYLES</w:styles>" % W)
        z.writestr("word/media/image1.png", b"\x89PNG fake image bytes")
    return buf.getvalue()


class TestDocxEdits(Base):
    def setUp(self):
        super().setUp()
        self.d = self.dir / "report.docx"
        self.d.write_bytes(make_docx())
        self.orig = zipfile.ZipFile(self.d)
        ReadFile().run({"path": "report.docx"}, self.ctx)

    def test_edit_across_runs_keeps_everything_else(self):
        out = EditFile().run({"path": "report.docx", "old_string": "$4.2M for FY26", "new_string": "$5.0M for FY27"},
                             self.ctx)
        self.assertIn("formatting and the rest of the document are unchanged", out)
        new = zipfile.ZipFile(self.d)
        self.assertEqual(new.namelist(), self.orig.namelist())
        for name in ("word/styles.xml", "word/media/image1.png", "[Content_Types].xml"):
            self.assertEqual(new.read(name), self.orig.read(name), name)
        text = docx_to_text(self.d.read_bytes())
        self.assertIn("The budget is $5.0M for FY27 & beyond.", text)
        self.assertIn("# Quarterly Report", text)
        self.assertIn("Status: on track.", text)
        xml = new.read("word/document.xml").decode()
        self.assertIn('mc:Ignorable="w14"', xml)          # namespaces untouched
        self.assertIn('w14:paraId="1A"', xml)
        self.assertIn("<w:b/></w:rPr><w:t>$5.0M for FY27</w:t>", xml)  # replacement kept the bold run
        self.assertIn("&amp; beyond.", xml)

    def test_heading_marker_from_read_file_tolerated(self):
        EditFile().run({"path": "report.docx", "old_string": "# Quarterly Report", "new_string": "# Annual Report"},
                       self.ctx)
        self.assertIn("# Annual Report", docx_to_text(self.d.read_bytes()))

    def test_not_found_and_multi_paragraph(self):
        with self.assertRaises(ToolError):
            EditFile().run({"path": "report.docx", "old_string": "nonexistent words", "new_string": "x"}, self.ctx)
        with self.assertRaises(ToolError):
            EditFile().run({"path": "report.docx", "old_string": "beyond.\nStatus", "new_string": "x"}, self.ctx)
        self.assertEqual(self.d.read_bytes(), make_docx())


class TestAgentRecoversFromBadWrite(unittest.TestCase):
    make = test_agent.AgentTest.make

    def test_model_snippet_write_is_refused_then_edit_succeeds(self):
        a = self.make([
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "mod.py"}}]},
            # the classic mistake: write just the changed function
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "mod.py",
                                                                  "content": "def func_7(x):\n    return x * 700\n"}}]},
            # the error message steers it to edit_file
            lambda body: {"tool_calls": [{"name": "edit_file", "arguments": {
                "path": "mod.py", "old_string": "def func_7(x):\n    return x + 7",
                "new_string": "def func_7(x):\n    return x * 700"}}]}
            if "edit_file" in body["messages"][-1]["content"] else {"content": "gave up"},
            {"content": "Changed func_7."},
            {"content": "Changed func_7 (not run: there are no tests)."},   # after the verify reminder
        ], mode="auto")
        (self.dir / "mod.py").write_text(BIG)
        out = a.run_turn("make func_7 in mod.py multiply by 700 instead")
        self.assertIn("not run", out)
        tool_results = [m["content"] for m in a.messages if m["role"] == "tool"]
        self.assertIn("edit_file", tool_results[1])            # the snippet write was refused...
        self.assertTrue(tool_results[2].startswith("Edited"))  # ...and the precise edit succeeded
        self.assertEqual((self.dir / "mod.py").read_text(), BIG.replace("return x + 7\n", "return x * 700\n"))


if __name__ == "__main__":
    unittest.main()
