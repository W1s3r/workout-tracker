"""Web access methods. Browser tests use a real Edge/Chrome/Chromium when one is
installed (skipped otherwise)."""
import socket
import unittest

from spy import net, webaccess
from spy.research.fetch import Fetcher
from spy.research.sources import HtmlSource
from tests.fakes import FakeIntranet
from tests.helpers import make_config, tmpdir


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestClassify(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(webaccess.classify(b"<html><body>" + b"real words " * 50 + b"</body></html>"), "")
        self.assertIn("login", webaccess.classify(b"<html><title>Sign In</title><input type=password></html>"))
        self.assertIn("JavaScript", webaccess.classify(b"<html><script>app()</script><div id=root></div></html>"))
        self.assertIn("404", webaccess.classify(b"x", 404))
        self.assertIn("login", webaccess.classify(b"<html>hi</html>", 200, "https://h/sso/login?next=/"))
        self.assertEqual(webaccess.classify(b"%PDF-1.4 ..."), "")


class WebBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net = FakeIntranet()
        cls.dir = tmpdir()

    @classmethod
    def tearDownClass(cls):
        cls.net.close()

    def setUp(self):
        self.cfg = make_config(self.dir, web={"via": "auto", "profile_dir": str(self.dir / "prof"),
                                              "cdp_port": getattr(self, "port", 9222), "render_wait": 2,
                                              "auto_launch": False})


class TestSimpleMethods(WebBase):
    def test_python(self):
        st, hd, body = webaccess.fetch_python(self.net.base + "/doc/1", {})
        self.assertEqual(st, 200)
        self.assertIn(b"Jane Smith", body)
        st, _, _ = webaccess.fetch_python(self.net.base + "/nope", {})
        self.assertEqual(st, 404)

    def test_curl(self):
        if "curl" not in webaccess.available_methods():
            self.skipTest("no curl")
        st, hd, body = webaccess.fetch_curl(self.net.base + "/doc/3", {})
        self.assertEqual(st, 200)
        self.assertIn(b"March 2027", body)
        self.assertIn("text/html", hd["Content-Type"])

    def test_auto_stops_on_404(self):
        st, _, _ = webaccess.web_get(self.net.base + "/missing", {}, 10, None, "auto")
        self.assertEqual(st, 404)

    def test_auto_returns_python_for_plain_page(self):
        st, _, body = webaccess.web_get(self.net.base + "/doc/1", {}, 10, None, "auto")
        self.assertIn(b"Jane Smith", body)
        self.assertEqual(webaccess._remembered().get(webaccess._host(self.net.base)), "python")


@unittest.skipUnless(webaccess.find_browser(), "no Edge/Chrome/Chromium installed")
class TestBrowserMethods(WebBase):
    port = free_port()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cfg = make_config(cls.dir, web={"cdp_port": cls.port, "profile_dir": str(cls.dir / "prof"), "render_wait": 2})
        cls.proc = webaccess.CDP(cls.port).launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        super().tearDownClass()

    def test_headless_renders_js(self):
        st, hd, body = webaccess.fetch_headless(self.net.base + "/jsearch?q=falcon", {}, 60)
        self.assertIn(b"Falcon schedule memo", body)

    def test_cdp_renders_js_and_downloads(self):
        cdp = webaccess.CDP(self.port)
        self.assertTrue(cdp.available())
        st, hd, body = cdp.get(self.net.base + "/jsearch?q=falcon", 30)
        self.assertEqual(st, 200)
        self.assertIn(b'<div id="r"><a', body)
        st, hd, body = cdp.get(self.net.base + "/file.pdf", 30)
        self.assertEqual(body, b"%PDF-1.4 fake pdf bytes \x00\x01\x02")
        self.assertIn("pdf", hd["Content-Type"])
        st, _, _ = cdp.get(self.net.base + "/nope", 30)
        self.assertEqual(st, 404)

    def test_auto_escalates_to_browser_for_js_page(self):
        webaccess.forget_methods()
        url = self.net.base + "/jsearch?q=falcon"
        st, hd, body = webaccess.web_get(url, {}, 30, None, "auto")
        self.assertIn(b'<div id="r"><a', body)  # rendered DOM, not the raw script
        self.assertEqual(webaccess._remembered()[webaccess._host(url)], "cdp")

    def test_search_source_and_fetcher_through_browser(self):
        src = HtmlSource("js", {"url": self.net.base + "/jsearch?q={query}", "via": "cdp"})
        titles = [r.title for r in src.search("falcon")]
        self.assertIn("Falcon schedule memo", titles)
        page = Fetcher(self.cfg).fetch(self.net.base + "/jsearch?q=x", use_cache=False, via="cdp")
        self.assertIn("Project Falcon Overview", page.text)

    def test_login_wall_detected(self):
        with self.assertRaises(net.HTTPError) as e:
            webaccess.web_get(self.net.base + "/login-wall", {}, 20, None, "auto")
        self.assertIn("login page", str(e.exception))
        self.assertIn("spy browser", str(e.exception))


if __name__ == "__main__":
    unittest.main()
