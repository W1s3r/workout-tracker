"""Intranet search, page fetching, local doc index search, and deep research tools."""
from __future__ import annotations

from hsa.research.fetch import Fetcher
from hsa.research.sources import build_sources
from hsa.research.textrank import best_passages
from hsa.tools.base import Tool, ToolError, truncate


class IntranetSearch(Tool):
    name = "intranet_search"
    description = """Search the internal network search engine(s). The engine is a simple keyword
matcher: use SHORT keyword queries (1-5 words), try acronyms AND their expansions,
and run several variants rather than one long question. Returns titles, URLs and
snippets; use fetch_url to read a result. For thorough multi-source questions use
deep_research instead."""
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "Short keyword query"},
        "source": {"type": "string", "description": "Only this configured source (optional)"},
        "page": {"type": "integer", "description": "Results page (default 1)"},
        "max_results": {"type": "integer", "description": "Default 10"},
    }, "required": ["query"]}

    def run(self, args, ctx):
        srcs = build_sources(ctx.config, [args["source"]] if args.get("source") else None)
        if not srcs:
            raise ToolError("No search sources enabled. Configure config['search']['sources'].")
        n = int(args.get("max_results") or 10)
        out, errs = [], []
        for s in srcs:
            try:
                res = s.search(args["query"], int(args.get("page") or 1), n)
            except Exception as e:
                errs.append("[%s error: %s]" % (s.name, str(e)[:300]))
                continue
            for i, r in enumerate(res[:n], 1):
                out.append("%d. [%s] %s" % (i, s.name, r.short()))
        if not out:
            return "No results for %r.%s Try shorter/different keywords or acronym variants." % (
                args["query"], (" " + " ".join(errs)) if errs else "")
        return "\n".join(out + errs)


class FetchUrl(Tool):
    name = "fetch_url"
    description = """Fetch an internal web page or document (HTML, PDF, Word, PowerPoint, Excel) by URL,
or a file:// / UNC path, and return its text. If `focus` is given, only the passages
most relevant to it are returned (good for long documents). Set show_links=true to
list links on the page (to navigate internal sites)."""
    parameters = {"type": "object", "properties": {
        "url": {"type": "string"},
        "focus": {"type": "string", "description": "What you are looking for (optional)"},
        "show_links": {"type": "boolean"},
        "max_chars": {"type": "integer", "description": "Default 12000"},
    }, "required": ["url"]}

    def run(self, args, ctx):
        page = Fetcher(ctx.config).fetch(args["url"])
        if page.error:
            raise ToolError("Fetch failed: %s" % page.error)
        maxc = int(args.get("max_chars") or 12000)
        text = page.text
        if args.get("focus") and len(text) > maxc:
            text = "[Most relevant passages for: %s]\n\n%s" % (
                args["focus"], "\n\n...\n\n".join(best_passages(text, args["focus"], k=max(2, maxc // 2000), size=2000)))
        out = "Title: %s\nURL: %s\n\n%s" % (page.title, page.url, truncate(text, maxc, "use focus= to target"))
        if args.get("show_links") and page.links:
            seen, lines = set(), []
            for u, a in page.links:
                if u not in seen:
                    seen.add(u)
                    lines.append("- %s -> %s" % (a[:80] or "(no text)", u))
            out += "\n\nLinks:\n" + "\n".join(lines[:150])
        return out


class DocSearch(Tool):
    name = "doc_search"
    description = """Full-text search of the LOCAL document index (files on network drives and
folders that were indexed with `python -m hsa index`). Fast, ranked (BM25), and
covers Word/PDF/PowerPoint/Excel/text files. Returns paths + best-matching snippet;
use read_file to open a hit."""
    parameters = {"type": "object", "properties": {
        "query": {"type": "string"},
        "path_contains": {"type": "string", "description": "Only paths containing this text"},
        "max_results": {"type": "integer"},
    }, "required": ["query"]}

    def run(self, args, ctx):
        from hsa.research.docindex import DocIndex
        idx = DocIndex(ctx.config)
        try:
            st = idx.stats()
            if not st["documents"]:
                return "The document index is empty. Ask the user to run: python -m hsa index <folders>"
            hits = idx.search(args["query"], int(args.get("max_results") or 10), args.get("path_contains"))
        finally:
            idx.close()
        if not hits:
            return "No indexed documents match %r (index has %d docs)." % (args["query"], st["documents"])
        return "\n\n".join("%d. %s  (score %.1f, modified %s)\n   %s" % (i, h["path"], h["score"], h["date"], h["snippet"][:400])
                           for i, h in enumerate(hits, 1))


class DeepResearchTool(Tool):
    name = "deep_research"
    description = """Run a multi-round deep research process over the internal network: plans
sub-questions, runs many keyword query variants across all search sources, triages
and reads the best documents, follows relevant links, finds gaps, iterates, and writes
a cited markdown report to the research/ folder. Takes several minutes. Use for
substantive questions that need many sources; use intranet_search for quick lookups."""
    parameters = {"type": "object", "properties": {
        "question": {"type": "string", "description": "The full research question"},
        "context": {"type": "string", "description": "Background / what is already known (optional)"},
        "rounds": {"type": "integer", "description": "Search rounds (default from config, usually 3)"},
    }, "required": ["question"]}

    def run(self, args, ctx):
        from hsa.research.engine import DeepResearch
        srcs = build_sources(ctx.config)
        if not srcs:
            raise ToolError("No search sources enabled. Configure config['search']['sources'].")
        eng = DeepResearch(ctx.config, ctx.llm_factory("research"), ctx.llm_factory("fast"), srcs,
                           log=lambda m: ctx.ui.info(m))
        res = eng.run(args["question"], args.get("context") or "", args.get("rounds"),
                      save_dir=ctx.cwd / ctx.config.get("research", "output_dir", default="research"))
        return "Report saved to %s (%d sources, %d facts, %d queries, %ds).\n\n%s" % (
            res["path"], res["sources"], res["facts"], res["queries"], res["seconds"],
            truncate(res["report"], 12000, "read the report file for the rest"))


TOOLS = [IntranetSearch, FetchUrl, DocSearch, DeepResearchTool]
