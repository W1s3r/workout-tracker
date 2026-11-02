import json
import re
import unittest

from spy.llm import make_client
from spy.research.docindex import DocIndex
from spy.research.engine import DeepResearch
from spy.research.sources import HtmlSource, JsonSource, build_sources
from spy.research.textrank import BM25, chunk_text, tokenize
from tests.fakes import FakeIntranet, FakeLLMServer
from tests.helpers import make_config, tmpdir


def research_llm(body):
    p = body["messages"][-1]["content"]
    if '"sub_questions"' in p:
        return {"content": json.dumps({"sub_questions": ["Who leads Project Falcon?", "When is Falcon IOC?"],
                                       "queries": ["Falcon", "Project Falcon schedule", "Falcon IOC"]})}
    if "Rate how likely" in p:
        scores = {}
        for line in p.splitlines():
            m = re.match(r"\[(\d+)\] (.*)", line)
            if m:
                scores[m.group(1)] = 0 if "Cafeteria" in m.group(2) else 3
        return {"content": json.dumps({"scores": scores})}
    if "Extract ONLY" in p:
        facts = []
        if "Jane Smith" in p:
            facts.append({"fact": "Jane Smith is the Falcon program manager", "quote": "program manager is Jane Smith"})
        if "March 2027" in p:
            facts.append({"fact": "Falcon IOC is March 2027", "quote": "March 2027"})
        links = re.findall(r"(L\d+): Falcon schedule memo", p)
        return {"content": "```json\n" + json.dumps({"relevant": bool(facts), "facts": facts,
                                                     "new_search_terms": ["LOGTRAK"], "follow_links": links}) + "\n```"}
    if "Assess coverage" in p:
        return {"content": json.dumps({"done": "March 2027" in p, "gaps": [], "new_queries": ["Falcon timeline"]})}
    if "Write a research report" in p:
        return {"content": "## Summary\nFalcon is led by Jane Smith [1]; IOC March 2027 [2]."}
    return {"content": "{}"}


class TestTextrank(unittest.TestCase):
    def test_tokenize_compound(self):
        t = tokenize("The AN/PRC-117G radio")
        self.assertIn("an/prc-117g", t)
        self.assertIn("prc", t)
        self.assertNotIn("the", t)

    def test_bm25_and_chunks(self):
        bm = BM25(["cats and dogs", "falcon program schedule", "falcon cookies"])
        self.assertEqual(bm.top("falcon schedule", 1)[0][0], 1)
        chunks = chunk_text("para one\n\n" * 400, size=500)
        self.assertTrue(all(len(c) <= 600 for c in chunks))


class TestSources(unittest.TestCase):
    def setUp(self):
        self.net = FakeIntranet()
        self.addCleanup(self.net.close)

    def test_html_source_heuristics(self):
        src = HtmlSource("i", {"url": self.net.base + "/search?q={query}"})
        res = src.search("falcon")
        titles = [r.title for r in res]
        self.assertIn("Project Falcon Overview", titles)
        self.assertNotIn("Home", titles)   # nav filtered
        self.assertNotIn("Next", titles)   # pagination filtered
        self.assertTrue(res[0].snippet)

    def test_html_source_regex(self):
        src = HtmlSource("i", {"url": self.net.base + "/search?q={query}",
                               "result_selector_regex": r"<a href='(?P<url>/doc/\d+)'>(?P<title>.*?)</a><p>(?P<snippet>.*?)</p>"})
        res = src.search("logtrak")
        self.assertEqual({r.title for r in res}, {"Project Falcon Overview", "LOGTRAK retirement notice"})

    def test_json_source_parse(self):
        src = JsonSource("j", {"results_path": "data.items", "fields": {"title": "name", "url": "meta.link",
                                                                        "snippet": ["summary", "body"]}})
        res = src.parse({"data": {"items": [{"name": "<b>A</b>", "meta": {"link": "/a"}, "body": "text"}]}}, "http://h/s")
        self.assertEqual((res[0].title, res[0].url, res[0].snippet), ("A", "http://h/a", "text"))


class TestDeepResearch(unittest.TestCase):
    def test_end_to_end(self):
        net = FakeIntranet()
        llm_srv = FakeLLMServer(research_llm)
        self.addCleanup(net.close)
        self.addCleanup(llm_srv.close)
        d = tmpdir()
        cfg = make_config(d,
                          providers={"work": {"base_url": llm_srv.url, "api_key": "k", "max_retries": 0}},
                          models={"inkling-small": {"model": "m", "stream": False}},
                          search={"sources": {"intranet": {"enabled": True, "type": "html",
                                                           "url": net.base + "/search?q={query}"}}},
                          research={"max_rounds": 3, "glossary": {"IOC": "initial operating capability"}})
        llm = make_client(cfg.model_profile("research"))
        logs = []
        eng = DeepResearch(cfg, llm, llm, build_sources(cfg), log=logs.append)
        res = eng.run("Who leads Project Falcon and when is IOC?", save_dir=d / "research")
        self.assertIn("Jane Smith", res["report"])
        self.assertIn("## Sources", res["report"])
        self.assertGreaterEqual(res["facts"], 2)
        urls = [s["url"] for s in eng.sources_read]
        self.assertFalse(any("/doc/2" in u for u in urls), "irrelevant cafeteria page should be triaged out")
        self.assertTrue(any("/doc/3" in u for u in urls))
        self.assertIn("initial operating capability", [q.lower() for q in eng.ran_queries])
        self.assertTrue((d / "research").is_dir())
        notes = list((d / "research").glob("*.notes.json"))
        self.assertEqual(len(notes), 1)


class TestDocIndex(unittest.TestCase):
    def test_index_and_search(self):
        d = tmpdir()
        share = d / "share"
        (share / "sub").mkdir(parents=True)
        (share / "falcon_plan.md").write_text("# Falcon\nThe Falcon program IOC date is March 2027.\n")
        (share / "sub" / "menu.txt").write_text("Taco Tuesday at the cafeteria.")
        (share / "ignore.bin").write_bytes(b"\x00\x01")
        cfg = make_config(d)
        idx = DocIndex(cfg)
        r = idx.index_paths([str(share)])
        self.assertEqual(r["added"], 2)
        hits = idx.search("falcon IOC")
        self.assertTrue(hits[0]["path"].endswith("falcon_plan.md"))
        self.assertEqual(idx.index_paths([str(share)])["unchanged"], 2)
        (share / "sub" / "menu.txt").unlink()
        self.assertEqual(idx.index_paths([str(share)])["removed"], 1)
        self.assertEqual(idx.stats()["documents"], 1)
        idx.close()


if __name__ == "__main__":
    unittest.main()


class TestHttpCommand(unittest.TestCase):
    def test_command_get(self):
        import sys
        from spy import net
        d = tmpdir()
        script = d / "getter.py"
        script.write_text("import sys\nopen(sys.argv[2], 'w').write('<html><a href=\"/doc/9\">Fetched ' + sys.argv[1] + '</a></html>')\n")
        cfg = {"http_command": '"%s" "%s" {url} {out}' % (sys.executable, script)}
        status, headers, body = net.request("http://intranet/x?q=1", cfg)
        self.assertIn(b"Fetched http://intranet/x?q=1", body)
        src = HtmlSource("i", {"url": "http://intranet/search?q={query}", "http_command": cfg["http_command"]})
        self.assertEqual(src.search("abc")[0].url, "http://intranet/doc/9")
