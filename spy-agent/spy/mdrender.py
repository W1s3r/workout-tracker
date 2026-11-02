"""Tiny streaming markdown -> ANSI renderer (stdlib only).

Renders line by line, so it works on streamed output: text appears a line
at a time, nicely formatted, instead of raw '**' and '#' characters."""
from __future__ import annotations

import re

RESET, BOLD, DIM, ITAL, UNDER = "\033[0m", "\033[1m", "\033[2m", "\033[3m", "\033[4m"
CYAN, YELLOW, MAGENTA, GREY, GREEN = "\033[36m", "\033[33m", "\033[35m", "\033[90m", "\033[32m"


class MarkdownStream:
    def __init__(self, write, color: bool = True, width: int = 100, skip=None):
        self.write = write
        self.skip = skip  # compiled regex: whole lines matching it are not shown (filler)
        self.color = color
        self.buf = ""
        self.in_code = False
        self.width = width

    def c(self, s, *codes):
        return "".join(codes) + s + RESET if self.color else s

    def feed(self, chunk: str):
        self.buf += chunk
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._emit(line)

    def _emit(self, line: str):
        if self.skip is not None and not self.in_code and line.strip() and self.skip.match(line.strip()):
            return
        self.write(self.render_line(line) + "\n")

    def flush(self):
        if self.buf:
            self._emit(self.buf)
            self.buf = ""
        self.in_code = False

    def inline(self, s: str) -> str:
        if not self.color:
            s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
            s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
            s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)
            return s
        s = re.sub(r"`([^`]+)`", lambda m: self.c(m.group(1), CYAN), s)
        s = re.sub(r"\*\*(.+?)\*\*", lambda m: self.c(m.group(1), BOLD), s)
        s = re.sub(r"__(.+?)__", lambda m: self.c(m.group(1), BOLD), s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", lambda m: self.c(m.group(1), ITAL), s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: m.group(1) + " " + self.c("(" + m.group(2) + ")", GREY), s)
        return s

    def render_line(self, line: str) -> str:
        stripped = line.strip()
        if stripped.startswith("```"):
            self.in_code = not self.in_code
            lang = stripped[3:].strip()
            return self.c(("┌─ " + lang) if self.in_code and lang else ("┌─" if self.in_code else "└─"), GREY)
        if self.in_code:
            return self.c("│ ", GREY) + self.c(line, YELLOW) if self.color else "  " + line
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            lvl = len(m.group(1))
            text = self.inline(m.group(2))
            if not self.color:
                return text.upper() if lvl == 1 else text
            return self.c(text, BOLD, MAGENTA if lvl == 1 else CYAN)
        m = re.match(r"^(\s*)[-*+]\s+(\[[ xX]\]\s+)?(.*)", line)
        if m:
            box = ""
            if m.group(2):
                box = "☒ " if "x" in m.group(2).lower() else "☐ "
            return "%s%s %s%s" % (m.group(1), self.c("•", GREEN), box, self.inline(m.group(3)))
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if m:
            return "%s%s %s" % (m.group(1), self.c(m.group(2) + ".", GREEN), self.inline(m.group(3)))
        if stripped.startswith(">"):
            return self.c("▌ ", GREY) + self.c(self.inline(stripped.lstrip("> ")), ITAL)
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            return self.c("─" * min(self.width, 60), GREY)
        if re.match(r"^\s*\|?\s*:?-{3,}", line) and "|" in line:
            return self.c(re.sub(r"[^|]", "─", line), GREY)
        return self.inline(line)


def render(text: str, color: bool = True) -> str:
    out = []
    r = MarkdownStream(out.append, color)
    r.feed(text)
    r.flush()
    return "".join(out).rstrip("\n")
