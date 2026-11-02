"""Text extraction from HTML and office documents using only the stdlib
(PDF uses pypdf / PyPDF2 / PyMuPDF if Anaconda has them, else `pdftotext`)."""
from __future__ import annotations

import html
import io
import json
import re
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from typing import List, Optional, Tuple
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

TEXT_EXTS = {".txt", ".md", ".rst", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml",
             ".ini", ".cfg", ".conf", ".py", ".ps1", ".sh", ".bat", ".cmd", ".sql", ".js", ".ts",
             ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rb", ".r", ".m", ".tex", ".toml"}


# ---------------------------------------------------------------- HTML -----
class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "table", "ul", "ol", "pre", "blockquote", "hr", "dd", "dt", "header", "footer"}
    BOILER = {"nav", "footer", "aside"}

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: List[str] = []
        self.links: List[Tuple[str, str]] = []
        self.title = ""
        self._skip = 0
        self._boiler = 0
        self._in_title = False
        self._a_href: Optional[str] = None
        self._a_text: List[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BOILER:
            self._boiler += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3", "h4"):
            self.out.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.out.append("- ")
        if tag in ("td", "th"):
            self.out.append(" | ")
        if tag == "a" and a.get("href"):
            self._a_href = urljoin(self.base_url, a["href"]) if self.base_url else a["href"]
            self._a_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BOILER and self._boiler:
            self._boiler -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a" and self._a_href:
            txt = " ".join("".join(self._a_text).split())
            if not self._a_href.startswith(("javascript:", "mailto:", "#")):
                self.links.append((self._a_href, txt))
            self._a_href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        if self._a_href is not None:
            self._a_text.append(data)
        if not self._boiler:
            self.out.append(data)


def html_to_text(raw: str, base_url: str = "") -> Tuple[str, str, List[Tuple[str, str]]]:
    """Returns (title, text, links[(url, anchor_text)])."""
    p = _HTMLText(base_url)
    try:
        p.feed(raw)
        p.close()
    except Exception:
        pass
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return html.unescape(p.title.strip()), text, p.links


# -------------------------------------------------------------- Office -----
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def docx_to_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(W_NS + "p"):
        style = ""
        ppr = p.find(W_NS + "pPr")
        if ppr is not None:
            ps = ppr.find(W_NS + "pStyle")
            if ps is not None:
                style = ps.get(W_NS + "val", "")
        txt = "".join(t.text or "" for t in p.iter(W_NS + "t"))
        if style.lower().startswith("heading") and txt:
            lvl = re.sub(r"\D", "", style) or "1"
            txt = "#" * min(int(lvl), 4) + " " + txt
        paras.append(txt)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(paras)).strip()


def pptx_to_text(data: bytes) -> str:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted([n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for i, n in enumerate(slides, 1):
            root = ET.fromstring(z.read(n))
            lines = []
            for p in root.iter(A_NS + "p"):
                t = "".join(r.text or "" for r in p.iter(A_NS + "t"))
                if t.strip():
                    lines.append(t)
            out.append("## Slide %d\n%s" % (i, "\n".join(lines)))
    return "\n\n".join(out)


def xlsx_to_text(data: bytes, max_rows: int = 2000) -> str:
    try:
        import openpyxl  # in Anaconda
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append("## Sheet: %s" % ws.title)
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    out.append("... (truncated)")
                    break
                if any(c is not None for c in row):
                    out.append(" | ".join("" if c is None else str(c) for c in row))
        return "\n".join(out)
    except ImportError:
        pass
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(S_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(S_NS + "t")))
        out = []
        sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        for n in sheets:
            out.append("## " + n.split("/")[-1])
            for i, row in enumerate(ET.fromstring(z.read(n)).iter(S_NS + "row")):
                if i >= max_rows:
                    break
                cells = []
                for c in row.iter(S_NS + "c"):
                    v = c.find(S_NS + "v")
                    val = v.text if v is not None else ""
                    if c.get("t") == "s" and val:
                        val = shared[int(val)]
                    elif c.get("t") == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter(S_NS + "t"))
                    cells.append(val or "")
                out.append(" | ".join(cells))
        return "\n".join(out)


def pdf_to_text(data: bytes, path: Optional[str] = None) -> str:
    for modname in ("pypdf", "PyPDF2"):
        try:
            mod = __import__(modname)
            reader = mod.PdfReader(io.BytesIO(data))
            return "\n\n".join("## Page %d\n%s" % (i + 1, (pg.extract_text() or ""))
                               for i, pg in enumerate(reader.pages))
        except ImportError:
            continue
        except Exception as e:
            return "[PDF extraction failed with %s: %s]" % (modname, e)
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        return "\n\n".join("## Page %d\n%s" % (i + 1, p.get_text()) for i, p in enumerate(doc))
    except ImportError:
        pass
    exe = shutil.which("pdftotext")
    if exe:
        r = subprocess.run([exe, "-layout", "-", "-"], input=data, capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace")
    return "[Cannot read PDF: no pypdf/PyPDF2/PyMuPDF installed and no pdftotext on PATH]"


def ipynb_to_text(data: bytes) -> str:
    nb = json.loads(data.decode("utf-8", "replace"))
    out = []
    for i, c in enumerate(nb.get("cells", [])):
        src = "".join(c.get("source", []))
        out.append("### Cell %d [%s]\n%s" % (i, c.get("cell_type"), src))
        for o in c.get("outputs", []) or []:
            t = "".join(o.get("text", [])) or "".join((o.get("data") or {}).get("text/plain", []))
            if t:
                out.append("[output]\n" + t[:2000])
    return "\n\n".join(out)


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return False
    sample = data[:8192]
    return b"\x00" in sample


def extract_any(data: bytes, name: str = "", content_type: str = "", base_url: str = ""):
    """Return (title, text, links) for any supported document."""
    n = name.lower()
    ct = (content_type or "").lower()
    try:
        if n.endswith(".docx") or "wordprocessingml" in ct:
            return "", docx_to_text(data), []
        if n.endswith(".pptx") or "presentationml" in ct:
            return "", pptx_to_text(data), []
        if n.endswith((".xlsx", ".xlsm")) or "spreadsheetml" in ct:
            return "", xlsx_to_text(data), []
        if n.endswith(".pdf") or "pdf" in ct or data[:5] == b"%PDF-":
            return "", pdf_to_text(data, name), []
        if n.endswith(".ipynb"):
            return "", ipynb_to_text(data), []
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        return "", "[Could not parse document: %s]" % e, []
    text = decode_text(data)
    if n.endswith((".html", ".htm", ".aspx", ".asp", ".php", ".jsp")) or "html" in ct or \
            re.match(r"\s*(<!doctype html|<html)", text[:500], re.I):
        return html_to_text(text, base_url)
    return "", text, []
