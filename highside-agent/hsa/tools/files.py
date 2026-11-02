"""File-system tools: read, write, edit, list, glob, grep, find (incl. network drives)."""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from hsa.extract import TEXT_EXTS, decode_text, extract_any, looks_binary
from hsa.tools.base import SKIP_DIRS, Tool, ToolContext, ToolError, truncate

DOC_EXTS = {".docx", ".pptx", ".xlsx", ".xlsm", ".pdf", ".ipynb"}


class ReadFile(Tool):
    name = "read_file"
    description = """Read a file and return its contents with line numbers (cat -n style).
Also extracts text from .docx, .pptx, .xlsx, .pdf, .ipynb and .html files. Works on
local paths, mapped drives (H:\\...) and UNC network paths (\\\\server\\share\\...).
For large files, use offset/limit to read a section. You must read a file before editing it."""
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path (absolute or relative to cwd)"},
        "offset": {"type": "integer", "description": "1-based line to start at (optional)"},
        "limit": {"type": "integer", "description": "Max number of lines to return (default 2000)"},
    }, "required": ["path"]}

    def run(self, args, ctx):
        path = ctx.resolve(args["path"])
        if not path.exists():
            raise ToolError("File not found: %s%s" % (path, _suggest(path)))
        if path.is_dir():
            raise ToolError("%s is a directory. Use list_dir instead." % path)
        size = path.stat().st_size
        if size > 50 * 1024 * 1024:
            raise ToolError("File is %.1f MB - too large. Use grep or read with a script." % (size / 1e6))
        data = path.read_bytes()
        ext = path.suffix.lower()
        if ext in DOC_EXTS or ext in (".html", ".htm"):
            _, text, _ = extract_any(data, path.name)
        elif looks_binary(data):
            return "[Binary file, %d bytes. Not displayed.]" % size
        else:
            text = decode_text(data)
        ctx.read_files[str(path.resolve())] = path.stat().st_mtime
        lines = text.splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = int(args.get("limit") or 2000)
        sel = lines[offset - 1: offset - 1 + limit]
        if not lines:
            return "[File is empty]"
        out = "\n".join("%6d\t%s" % (i, (ln if len(ln) < 2000 else ln[:2000] + "...[line truncated]"))
                        for i, ln in enumerate(sel, start=offset))
        if offset - 1 + limit < len(lines):
            out += "\n[... %d more lines. Use offset=%d to continue.]" % (
                len(lines) - (offset - 1 + limit), offset + limit)
        return out


class WriteFile(Tool):
    name = "write_file"
    description = """Create or overwrite a file with the given content. Parent directories are
created automatically. Prefer edit_file for changing existing files. If the file
already exists you must read_file it first."""
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path"},
        "content": {"type": "string", "description": "Full file content"},
    }, "required": ["path", "content"]}
    read_only = False
    is_edit = True

    def run(self, args, ctx):
        path = ctx.resolve(args["path"])
        if path.exists() and str(path.resolve()) not in ctx.read_files:
            raise ToolError("File %s already exists. read_file it first, then write (or use edit_file)." % path)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = args["content"]
        existed = path.exists()
        old = path.read_text(encoding="utf-8", errors="replace") if existed else ""
        newline = "\r\n" if "\r\n" in old else None
        with open(path, "w", encoding="utf-8", newline=newline) as f:
            f.write(content)
        ctx.read_files[str(path.resolve())] = path.stat().st_mtime
        n = content.count("\n") + (0 if content.endswith("\n") else 1)
        return "%s %s (%d lines)." % ("Overwrote" if existed else "Created", path, n)


class EditFile(Tool):
    name = "edit_file"
    description = """Replace an exact string in a file. old_string must match the file exactly
(including indentation/whitespace) and be unique unless replace_all is true. Include
enough surrounding lines to make it unique. Do NOT include the line-number prefixes
from read_file output. To create a new file use write_file."""
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path"},
        "old_string": {"type": "string", "description": "Exact text to replace"},
        "new_string": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
    }, "required": ["path", "old_string", "new_string"]}
    read_only = False
    is_edit = True

    def run(self, args, ctx):
        path = ctx.resolve(args["path"])
        if not path.is_file():
            raise ToolError("File not found: %s" % path)
        key = str(path.resolve())
        if key not in ctx.read_files:
            raise ToolError("You must read_file %s before editing it." % path)
        if path.stat().st_mtime > ctx.read_files[key] + 0.001:
            raise ToolError("%s changed on disk since you read it. read_file it again." % path)
        raw = path.read_bytes()
        crlf = b"\r\n" in raw
        text = decode_text(raw).replace("\r\n", "\n")
        old = args["old_string"].replace("\r\n", "\n")
        new = args["new_string"].replace("\r\n", "\n")
        if old == new:
            raise ToolError("old_string and new_string are identical.")
        if old == "":
            raise ToolError("old_string is empty. Use write_file to create files.")
        count = text.count(old)
        if count == 0:
            # forgiving fallback 1: model copied read_file line-number prefixes
            stripped = re.sub(r"(?m)^\s*\d+\t", "", old)
            if stripped != old and text.count(stripped) >= 1:
                old, count = stripped, text.count(stripped)
            else:
                # fallback 2: whitespace-insensitive unique match of whole lines
                span = _fuzzy_find(text, old)
                if span:
                    old = text[span[0]:span[1]]
                    count = 1
                else:
                    raise ToolError("old_string not found in %s.%s" % (path, _closest(text, old)))
        if count > 1 and not args.get("replace_all"):
            raise ToolError("old_string occurs %d times. Add more context to make it unique, "
                            "or set replace_all=true." % count)
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        if crlf:
            updated = updated.replace("\n", "\r\n")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(updated)
        ctx.read_files[key] = path.stat().st_mtime
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "before", "after", n=1))
        return "Edited %s (%d replacement%s).\n%s" % (path, count if args.get("replace_all") else 1,
                                                      "s" if count > 1 and args.get("replace_all") else "",
                                                      truncate(diff, 3000))


def _fuzzy_find(text: str, old: str):
    """Match old against text ignoring leading/trailing whitespace on each line."""
    olines = [l.strip() for l in old.strip("\n").split("\n")]
    tlines = text.split("\n")
    n = len(olines)
    hits = []
    for i in range(len(tlines) - n + 1):
        if all(tlines[i + j].strip() == olines[j] for j in range(n)):
            hits.append(i)
    if len(hits) != 1:
        return None
    i = hits[0]
    start = sum(len(l) + 1 for l in tlines[:i])
    end = start + sum(len(l) + 1 for l in tlines[i:i + n]) - 1
    return start, end


def _closest(text: str, old: str) -> str:
    first = old.strip().split("\n")[0].strip()
    if not first:
        return ""
    lines = text.split("\n")
    m = difflib.get_close_matches(first, [l.strip() for l in lines], n=1, cutoff=0.5)
    if not m:
        return " Re-read the file and copy the text exactly."
    idx = [l.strip() for l in lines].index(m[0])
    ctx_lines = "\n".join("%6d\t%s" % (i + 1, lines[i]) for i in range(max(0, idx - 2), min(len(lines), idx + 3)))
    return " The closest match is near line %d:\n%s\nCopy the text exactly (without line numbers)." % (idx + 1, ctx_lines)


def _suggest(path: Path) -> str:
    try:
        parent = path.parent
        if parent.is_dir():
            names = os.listdir(parent)
            m = difflib.get_close_matches(path.name, names, n=3, cutoff=0.5)
            if m:
                return ". Did you mean: " + ", ".join(str(parent / x) for x in m)
    except OSError:
        pass
    return ""


class ListDir(Tool):
    name = "list_dir"
    description = """List the contents of a directory (local, mapped drive or UNC network share).
Shows sizes and modification dates. depth>1 shows a tree."""
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "Directory (default: cwd)"},
        "depth": {"type": "integer", "description": "Tree depth, 1-4 (default 1)"},
        "show_hidden": {"type": "boolean", "description": "Include dotfiles (default false)"},
    }, "required": []}

    def run(self, args, ctx):
        path = ctx.resolve(args.get("path") or ".")
        if not path.is_dir():
            raise ToolError("Not a directory: %s%s" % (path, _suggest(path)))
        depth = max(1, min(4, int(args.get("depth") or 1)))
        hidden = bool(args.get("show_hidden"))
        out: List[str] = [str(path)]
        count = [0]

        def walk(p: Path, level: int, prefix: str):
            try:
                entries = sorted(os.scandir(p), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except OSError as e:
                out.append(prefix + "[cannot list: %s]" % e)
                return
            for e in entries:
                if not hidden and e.name.startswith("."):
                    continue
                count[0] += 1
                if count[0] > 1000:
                    out.append(prefix + "... (truncated at 1000 entries)")
                    return
                try:
                    st = e.stat(follow_symlinks=False)
                    isdir = e.is_dir(follow_symlinks=False)
                except OSError:
                    out.append(prefix + e.name + "  [unreadable]")
                    continue
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                if isdir:
                    out.append("%s%s/  %s" % (prefix, e.name, stamp))
                    if level < depth and e.name not in SKIP_DIRS:
                        walk(Path(e.path), level + 1, prefix + "  ")
                else:
                    out.append("%s%s  %s  %s" % (prefix, e.name, _human(st.st_size), stamp))

        walk(path, 1, "  ")
        return "\n".join(out)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%d%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n


def _iter_files(root: Path, include_glob: str = None, time_budget: float = 60.0, max_files: int = 200000):
    """Walk files under root, skipping junk dirs, with a time budget (network drives can be slow)."""
    t0 = time.time()
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if include_glob and not _glob_match(fn, os.path.join(dirpath, fn), root, include_glob):
                continue
            n += 1
            yield Path(dirpath) / fn
            if n >= max_files:
                return
        if time.time() - t0 > time_budget:
            raise TimeoutError("time budget of %ds exceeded" % time_budget)


def _glob_match(name: str, full: str, root: Path, pattern: str) -> bool:
    pats = [p.strip() for p in re.split(r"[,;](?![^{]*\})", pattern) if p.strip()]
    for pat in _expand_braces(pats):
        if "/" in pat or "\\" in pat or "**" in pat:
            rel = os.path.relpath(full, root).replace("\\", "/")
            if fnmatch.fnmatch(rel, pat.replace("\\", "/")) or fnmatch.fnmatch(rel, pat.replace("**/", "")):
                return True
        elif fnmatch.fnmatch(name.lower(), pat.lower()):
            return True
    return False


def _expand_braces(pats: List[str]) -> List[str]:
    out = []
    for p in pats:
        m = re.search(r"\{([^{}]+)\}", p)
        if m:
            out.extend(_expand_braces([p[:m.start()] + alt + p[m.end():] for alt in m.group(1).split(",")]))
        else:
            out.append(p)
    return out


class Glob(Tool):
    name = "glob"
    description = """Find files by glob pattern, e.g. "**/*.py", "src/**/*.{js,ts}", "*report*.docx".
Returns paths sorted by modification time (newest first)."""
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Glob pattern"},
        "path": {"type": "string", "description": "Directory to search (default: cwd)"},
    }, "required": ["pattern"]}

    def run(self, args, ctx):
        root = ctx.resolve(args.get("path") or ".")
        if not root.is_dir():
            raise ToolError("Not a directory: %s" % root)
        pat = args["pattern"]
        hits = []
        try:
            for f in _iter_files(root, pat, time_budget=60):
                hits.append(f)
        except TimeoutError:
            hits.append(None)
        timed_out = None in hits
        hits = [h for h in hits if h is not None]
        hits.sort(key=lambda p: _mtime(p), reverse=True)
        if not hits:
            return "No files matched %r under %s" % (pat, root)
        shown = hits[:300]
        out = "\n".join(str(p) for p in shown)
        if len(hits) > 300:
            out += "\n... (%d more)" % (len(hits) - 300)
        if timed_out:
            out += "\n[search stopped early: time budget exceeded - narrow the path]"
        return out


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0


class Grep(Tool):
    name = "grep"
    description = """Search file contents with a regular expression (Python re syntax). Uses ripgrep if
installed, else a pure-Python search. output_mode: "files" (default, paths only),
"content" (matching lines with line numbers), "count"."""
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Regex to search for"},
        "path": {"type": "string", "description": "File or directory (default: cwd)"},
        "glob": {"type": "string", "description": "Only files matching this glob, e.g. *.py"},
        "ignore_case": {"type": "boolean"},
        "output_mode": {"type": "string", "enum": ["files", "content", "count"]},
        "context": {"type": "integer", "description": "Lines of context around matches (content mode)"},
        "max_results": {"type": "integer", "description": "Default 200"},
    }, "required": ["pattern"]}

    def run(self, args, ctx):
        root = ctx.resolve(args.get("path") or ".")
        mode = args.get("output_mode") or "files"
        maxr = int(args.get("max_results") or 200)
        flags = re.I if args.get("ignore_case") else 0
        try:
            rx = re.compile(args["pattern"], flags)
        except re.error as e:
            raise ToolError("Invalid regex: %s" % e)
        rg = shutil.which("rg")
        if rg:
            try:
                return self._rg(rg, args, root, mode, maxr)
            except Exception:
                pass
        C = int(args.get("context") or 0)
        files = [root] if root.is_file() else None
        out: List[str] = []
        nfiles = 0
        try:
            it = files or _iter_files(root, args.get("glob"), time_budget=90)
            for f in it:
                if f.stat().st_size > 20 * 1024 * 1024:
                    continue
                try:
                    data = f.read_bytes()
                except OSError:
                    continue
                if f.suffix.lower() in DOC_EXTS:
                    text = extract_any(data, f.name)[1]
                elif looks_binary(data):
                    continue
                else:
                    text = decode_text(data)
                lines = text.splitlines()
                hits = [i for i, l in enumerate(lines) if rx.search(l)]
                if not hits:
                    continue
                nfiles += 1
                if mode == "files":
                    out.append(str(f))
                elif mode == "count":
                    out.append("%s:%d" % (f, len(hits)))
                else:
                    shown = set()
                    for i in hits:
                        for j in range(max(0, i - C), min(len(lines), i + C + 1)):
                            if j not in shown:
                                shown.add(j)
                                out.append("%s:%d%s%s" % (f, j + 1, ":" if j == i else "-", lines[j][:500]))
                        if C:
                            out.append("--")
                if len(out) >= maxr:
                    out.append("... (stopped at %d results)" % maxr)
                    break
        except TimeoutError:
            out.append("[search stopped early: time budget exceeded - narrow the path or glob]")
        return "\n".join(out) if out else "No matches for %r in %s" % (args["pattern"], root)

    def _rg(self, rg, args, root, mode, maxr):
        cmd = [rg, "--no-heading", "--color", "never"]
        if args.get("ignore_case"):
            cmd.append("-i")
        if mode == "files":
            cmd.append("-l")
        elif mode == "count":
            cmd.append("-c")
        else:
            cmd.append("-n")
            if args.get("context"):
                cmd += ["-C", str(int(args["context"]))]
        if args.get("glob"):
            for g in re.split(r"[,;]", args["glob"]):
                cmd += ["--glob", g.strip()]
        cmd += ["-e", args["pattern"], str(root)]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        out = r.stdout.decode("utf-8", "replace").splitlines()
        if not out:
            return "No matches for %r in %s" % (args["pattern"], root)
        extra = "\n... (%d more)" % (len(out) - maxr) if len(out) > maxr else ""
        return "\n".join(out[:maxr]) + extra


class FindFiles(Tool):
    name = "find_files"
    description = """Find files or folders by (partial) name across the configured search locations
(local folders and network drives listed in config "locations") or a given path.
Case-insensitive; space-separated words must all appear in the name. Use this when
you don't know where something lives. Call with no query to list the locations."""
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "Words that appear in the file/folder name"},
        "path": {"type": "string", "description": "Search only here (optional)"},
        "max_results": {"type": "integer"},
        "max_seconds": {"type": "integer", "description": "Time budget per location (default 60)"},
    }, "required": []}

    def run(self, args, ctx):
        locs = dict(ctx.config.get("locations") or {})
        if not args.get("query"):
            if not locs:
                return "No locations configured. Add them to config 'locations' (e.g. network shares)."
            return "\n".join("%s: %s%s" % (k, v, "" if os.path.isdir(v) else "  [NOT REACHABLE]")
                             for k, v in locs.items())
        words = args["query"].lower().split()
        roots = [ctx.resolve(args["path"])] if args.get("path") else \
            [Path(v) for v in locs.values()] + [ctx.cwd]
        maxr = int(args.get("max_results") or 100)
        budget = int(args.get("max_seconds") or 60)
        hits, notes = [], []
        for root in roots:
            if not root.is_dir():
                notes.append("[unreachable: %s]" % root)
                continue
            t0 = time.time()
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for nm in dirnames + filenames:
                    low = nm.lower()
                    if all(w in low for w in words):
                        hits.append(os.path.join(dirpath, nm) + ("/" if nm in dirnames else ""))
                if len(hits) >= maxr:
                    break
                if time.time() - t0 > budget:
                    notes.append("[time budget hit in %s - results partial]" % root)
                    break
            if len(hits) >= maxr:
                break
        out = "\n".join(hits[:maxr]) or "No names matched %r." % args["query"]
        return out + ("\n" + "\n".join(notes) if notes else "")


TOOLS = [ReadFile, WriteFile, EditFile, ListDir, Glob, Grep, FindFiles]
