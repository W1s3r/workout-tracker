"""Project instances (SPY.md hierarchy), CLI input handling, and the installer."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from spy.config import find_project_root, project_data_dir
from spy.context import add_note, load_context, memory_path
from tests.helpers import make_config, tmpdir

ROOT = Path(__file__).resolve().parent.parent


class TestProjects(unittest.TestCase):
    def test_project_root_detection(self):
        d = tmpdir()
        (d / "repo" / "src" / "deep").mkdir(parents=True)
        (d / "repo" / ".git").mkdir()
        self.assertEqual(find_project_root(d / "repo" / "src" / "deep"), d / "repo")
        (d / "plain" / "sub").mkdir(parents=True)
        self.assertEqual(find_project_root(d / "plain" / "sub"), d / "plain" / "sub")   # no marker -> cwd
        (d / "plain" / "spy.md").write_text("x")                                        # lowercase works
        self.assertEqual(find_project_root(d / "plain" / "sub"), d / "plain")

    def test_master_and_project_hierarchy(self):
        d = tmpdir()
        proj = d / "proj"
        (proj / "pkg").mkdir(parents=True)
        cfg = make_config(proj)
        add_note(cfg, "global", "Never push to main", "About me / preferences")
        add_note(cfg, "project", "Tests: pytest -q")
        (proj / "pkg" / "Spy.md").write_text("pkg uses numpy only")
        text, files = load_context(cfg, proj / "pkg")
        self.assertEqual([f.name for f in files], ["SPY.md", "SPY.md", "Spy.md"])
        self.assertLess(text.index('scope="master"'), text.index('scope="project"'))
        self.assertIn("Never push to main", text)
        self.assertIn("pytest -q", text)
        self.assertIn("pkg uses numpy only", text)
        self.assertIn("MASTER file holds the user's standing rules", text)
        # from the project root, the sub-folder file is not loaded
        self.assertNotIn("numpy", load_context(cfg, proj)[0])
        self.assertEqual(memory_path(cfg, "master"), cfg.home / "SPY.md")

    def test_projects_are_isolated(self):
        d = tmpdir()
        a, b = make_config(d / "a"), make_config(d / "b")
        b.home = a.home  # same user, same master file
        add_note(a, "project", "only for A")
        self.assertNotIn("only for A", load_context(b, d / "b")[0])
        self.assertNotEqual(project_data_dir(a.home, d / "a"), project_data_dir(a.home, d / "b"))


class TestCliInput(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        os.environ["SPY_HOME"] = str(self.dir / ".spyhome")
        (self.dir / "notes.txt").write_text("alpha\nbeta\n")
        from spy import cli
        with mock.patch("pathlib.Path.cwd", return_value=self.dir):
            args = mock.Mock(config=None, model="mock", mode="auto")
            self.app = cli.App(args)
        self.app.agent.ctx.cwd = self.dir

    def test_mentions_attach_files(self):
        out = self.app.expand_mentions("summarize @notes.txt and @missing.txt please")
        self.assertIn('<file path="%s">' % (self.dir / "notes.txt"), out)
        self.assertIn("beta", out)
        self.assertNotIn("missing.txt\">", out)
        # attached file counts as read -> can be edited directly
        self.assertIn(str((self.dir / "notes.txt").resolve()), self.app.agent.ctx.read_files)
        self.assertEqual(self.app.expand_mentions("email me@example.com"), "email me@example.com")

    def test_slash_vs_absolute_path(self):
        with mock.patch.object(self.app, "slash", return_value=True) as sl, \
                mock.patch.object(self.app.agent, "run_turn", return_value="") as rt:
            self.app.handle("/changes")
            self.app.handle("/usr/local/data.csv has bad rows, fix it")
        sl.assert_called_once_with("/changes")
        rt.assert_called_once()

    def test_undo_command(self):
        from spy.tools.files import ReadFile, EditFile
        ReadFile().run({"path": "notes.txt"}, self.app.agent.ctx)
        EditFile().run({"path": "notes.txt", "old_string": "beta", "new_string": "gamma"}, self.app.agent.ctx)
        self.app.slash("/undo")
        self.assertEqual((self.dir / "notes.txt").read_text(), "alpha\nbeta\n")


class TestInstaller(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "posix launcher test")
    def test_install_creates_working_spy_command(self):
        home = tmpdir()
        env = dict(os.environ, HOME=str(home), SPY_HOME=str(home / ".spy"))
        r = subprocess.run([sys.executable, str(ROOT / "install.py")], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        launcher = home / ".local" / "bin" / "spy"
        self.assertTrue(launcher.exists())
        self.assertTrue((home / ".spy" / "SPY.md").exists())
        self.assertTrue((home / ".spy" / "commands" / "review.md").exists())
        proj = home / "work"
        proj.mkdir()
        r = subprocess.run([str(launcher), "--version"], env=env, capture_output=True, text=True, cwd=str(proj))
        self.assertIn("spy 0.2", r.stdout)
        r = subprocess.run([str(launcher), "--model", "mock"], input="hello\n/exit\n", env=env, capture_output=True,
                           text=True, cwd=str(proj), timeout=60)
        self.assertIn("Welcome to Spy", r.stdout)
        self.assertIn("You said: hello", r.stdout)
        r = subprocess.run([sys.executable, str(ROOT / "install.py"), "--uninstall"], env=env, capture_output=True,
                           text=True)
        self.assertFalse(launcher.exists())


if __name__ == "__main__":
    unittest.main()
