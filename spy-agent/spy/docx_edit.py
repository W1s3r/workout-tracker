"""Precise text replacement inside Word .docx files using only the stdlib.

The XML is edited as text (not re-serialised), so everything except the
changed words stays byte-for-byte identical: styles, tables, images, tracked
formatting. Text split across several formatting runs is handled: the new text
goes into the first run of the match (keeping its formatting) and the rest of
the matched text is removed from the following runs.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import List, Tuple
from xml.sax.saxutils import escape, unescape

_PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_TEXT = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
PARTS = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
_ENT = {"&quot;": '"', "&apos;": "'"}


class DocxEditError(Exception):
    pass


def _replace_in_paragraph(p: str, old: str, new: str, limit: int) -> Tuple[str, int]:
    nodes = list(_TEXT.finditer(p))
    if not nodes:
        return p, 0
    texts = [unescape(m.group(2), _ENT) for m in nodes]
    joined = "".join(texts)
    if old not in joined:
        return p, 0
    starts, pos = [], 0
    for t in texts:
        starts.append(pos)
        pos += len(t)
    count = 0
    search_from = 0
    while count < limit:
        idx = joined.find(old, search_from)
        if idx < 0:
            break
        end = idx + len(old)
        # node containing idx, node containing end-1
        first = max(i for i, s in enumerate(starts) if s <= idx)
        last = max(i for i, s in enumerate(starts) if s <= max(idx, end - 1))
        for i in range(first, last + 1):
            s, e = starts[i], starts[i] + len(texts[i])
            a, b = max(idx, s) - s, min(end, e) - s
            texts[i] = texts[i][:a] + (new if i == first else "") + texts[i][b:]
        joined = "".join(texts)
        starts, pos = [], 0
        for t in texts:
            starts.append(pos)
            pos += len(t)
        search_from = idx + len(new)
        count += 1
    out, last_end = [], 0
    for m, t in zip(nodes, texts):
        open_tag = m.group(1)
        if (t[:1].isspace() or t[-1:].isspace()) and "xml:space" not in open_tag:
            open_tag = open_tag[:-1] + ' xml:space="preserve">'
        out.append(p[last_end:m.start()])
        out.append(open_tag + escape(t) + m.group(3))
        last_end = m.end()
    out.append(p[last_end:])
    return "".join(out), count


def docx_replace(data: bytes, old: str, new: str, replace_all: bool = False) -> Tuple[bytes, int]:
    if "\n" in old.strip("\n") or "\n" in new.strip("\n"):
        raise DocxEditError("In Word documents, edit one paragraph at a time (old_string/new_string must not "
                            "contain line breaks).")
    old, new = old.strip("\n"), new.strip("\n")
    zin = zipfile.ZipFile(io.BytesIO(data))
    changed = {}
    total = 0
    for info in zin.infolist():
        if not PARTS.search(info.filename):
            continue
        xml = zin.read(info.filename).decode("utf-8")
        parts: List[str] = []
        last = 0
        part_count = 0
        for m in _PARA.finditer(xml):
            remaining = 10 ** 9 if replace_all else (1 - total - part_count)
            if remaining <= 0:
                break
            np_, n = _replace_in_paragraph(m.group(0), old, new, remaining)
            if n:
                parts.append(xml[last:m.start()])
                parts.append(np_)
                last = m.end()
                part_count += n
        if part_count:
            parts.append(xml[last:])
            changed[info.filename] = "".join(parts).encode("utf-8")
            total += part_count
        if total and not replace_all:
            break
    if not total:
        return data, 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        for info in zin.infolist():
            zout.writestr(info, changed.get(info.filename, zin.read(info.filename)))
    return buf.getvalue(), total


def docx_count(data: bytes, old: str) -> int:
    """How many paragraphs contain `old` (for uniqueness checks)."""
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if PARTS.search(name):
                xml = z.read(name).decode("utf-8")
                for m in _PARA.finditer(xml):
                    joined = "".join(unescape(t.group(2), _ENT) for t in _TEXT.finditer(m.group(0)))
                    n += joined.count(old)
    return n
