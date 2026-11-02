"""Guards against the classic LLM editing failure: "the model knew the change,
but replaced the whole file with just the changed snippet".

Layers:
  1. write_file on an EXISTING file that would drop most of its lines is refused
     unless full_rewrite=true - and then the user is always asked, even in auto mode.
  2. Content containing "rest of the code unchanged"-style placeholders is refused
     (the model elided code it meant to keep).
  3. Content pasted with read_file's line-number prefixes is refused.
  4. edit_file edits that delete a large part of a file always ask the user.
  5. Every change is backed up in memory; /undo restores it.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

_PLACEHOLDER = re.compile(
    r"^\s*(#|//|/\*|<!--|--|;|'|\*|rem\b)?\s*(\.\.\.|…|\[\.\.\.\]|\(\.\.\.\))?\s*"
    r"[\[(]?\s*(\.\.\.\s*)?(the\s+)?(rest|remainder|remaining|existing|unchanged|previous|other|same|original|"
    r"omitted|truncated|more)\b[^\n]{0,60}\b(code|file|content|contents|lines|functions|methods|implementation|"
    r"text|unchanged|same|here|below|above|as before)\b[^\n]{0,40}$",
    re.I | re.M)
_ELLIPSIS_LINE = re.compile(r"^\s*(#|//|<!--|--|;)?\s*(\.\.\.|…)\s*(-->)?\s*$", re.M)
_LINE_NUM = re.compile(r"^\s*\d+\t", re.M)

LARGE_FILE_LINES = 20       # files smaller than this are never "large deletion" cases
KEEP_FRACTION = 0.5         # a rewrite keeping < 50% of the old lines is suspicious


def placeholder_lines(new: str, old: str = "") -> List[str]:
    hits = []
    for m in list(_PLACEHOLDER.finditer(new)) + list(_ELLIPSIS_LINE.finditer(new)):
        line = m.group(0).strip()
        if line and line not in old:
            hits.append(line)
    return hits


def has_line_number_prefixes(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    return sum(1 for l in lines if _LINE_NUM.match(l)) >= 0.6 * len(lines)


def kept_fraction(old: str, new: str) -> Tuple[float, int, int]:
    """Fraction of the old file's non-blank lines that still appear in the new content."""
    old_lines = [l.strip() for l in old.splitlines() if l.strip()]
    if not old_lines:
        return 1.0, 0, 0
    new_set = set(l.strip() for l in new.splitlines() if l.strip())
    kept = sum(1 for l in old_lines if l in new_set)
    return kept / len(old_lines), len(old_lines) - kept, len(old_lines)


def check_write(old: Optional[str], new: str, full_rewrite: bool) -> Optional[str]:
    """Return an error message if this write_file looks like a destructive mistake."""
    if has_line_number_prefixes(new):
        return ("Your content still has the line-number prefixes from read_file (e.g. '    12<TAB>'). "
                "Remove them - write only the real file text.")
    ph = placeholder_lines(new, old or "")
    if ph:
        return ("Your content contains placeholder text standing in for code you left out: %r. Writing it would "
                "DELETE that code. Use edit_file to change only the part you need, or write the complete file."
                % ph[0][:120])
    if old is None:
        return None
    frac, lost, total = kept_fraction(old, new)
    if total >= LARGE_FILE_LINES and frac < KEEP_FRACTION and not full_rewrite:
        return ("REFUSED: this would replace %s (%d lines) and delete %d of its lines. write_file replaces the "
                "WHOLE file. To change part of a file use edit_file with a small, unique old_string. Only if you "
                "really intend to replace the entire file, call write_file again with full_rewrite=true and the "
                "COMPLETE new content." % ("the file", total, lost))
    return None


def is_large_deletion(old: str, new: str) -> bool:
    frac, lost, total = kept_fraction(old, new)
    return total >= LARGE_FILE_LINES and frac < KEEP_FRACTION


class FileHistory:
    """In-memory backups of every file Spy changes (for /undo)."""

    def __init__(self, max_bytes: int = 100 * 1024 * 1024):
        self.entries: List[dict] = []
        self.max_bytes = max_bytes

    def record(self, path: Path, tool: str):
        try:
            before = path.read_bytes() if path.exists() else None
        except OSError:
            return
        self.entries.append({"path": str(path), "before": before, "tool": tool, "time": time.time()})
        total = sum(len(e["before"] or b"") for e in self.entries)
        while total > self.max_bytes and len(self.entries) > 1:
            total -= len(self.entries.pop(0)["before"] or b"")

    def undo(self) -> str:
        if not self.entries:
            return "Nothing to undo."
        e = self.entries.pop()
        p = Path(e["path"])
        if e["before"] is None:
            try:
                p.unlink()
            except OSError as ex:
                return "Could not remove %s: %s" % (p, ex)
            return "Undid %s: removed newly created file %s" % (e["tool"], p)
        p.write_bytes(e["before"])
        return "Undid %s: restored %s (%d bytes)" % (e["tool"], p, len(e["before"]))

    def summary(self, n: int = 15) -> str:
        if not self.entries:
            return "No file changes this session."
        return "\n".join("%s  %-10s %s" % (time.strftime("%H:%M:%S", time.localtime(e["time"])), e["tool"], e["path"])
                         for e in self.entries[-n:])
