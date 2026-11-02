"""Response style: language, verbosity, format, tone, named styles, and
cleanup of common small-model output artifacts.

Settings are layered:  config["style"]  <  ~/.spy/style.json  <  <project>/.spy/style.json
and are changed at runtime with /style (and saved with /style save).
Named styles: built-ins below, or your own markdown files in ~/.spy/styles/NAME.md
or <project>/.spy/styles/NAME.md (the file's text becomes the style instructions).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

BUILTIN_STYLES: Dict[str, str] = {
    "default": "",
    "concise": ("Keep answers short: lead with the answer, no preamble or recap, no filler like "
                "\"Great question\" or \"Let me know if you need anything else\". Use bullets over paragraphs."),
    "explanatory": ("Explain your reasoning and teach as you go: say why you chose an approach, point out "
                    "relevant concepts and trade-offs, and define jargon the first time you use it."),
    "bluf": ("Use BLUF format. Start with 'BLUF:' and the bottom line in 1-2 sentences. Then 'Details:' as short "
             "bullets with specifics (names, numbers, dates, sources). End with 'Next steps:' if any action is needed. "
             "No filler."),
    "plain": ("Write plain text only - NO markdown: no #, *, backticks, or tables. Use simple dashes for lists and "
              "blank lines between sections, so the text can be pasted into email or documents."),
    "report": ("Write in a formal, professional report style: clear headings, complete sentences, neutral tone, "
               "precise wording, and citations for every factual claim drawn from sources."),
}

VERBOSITY = {
    "terse": "Be extremely brief: one or two sentences, or a short list, unless more is essential.",
    "concise": "Be concise: answer directly, avoid preamble and repetition.",
    "normal": "",
    "detailed": "Be thorough: include relevant detail, context and examples.",
}

FORMAT = {
    "markdown": "Format responses as GitHub-flavored markdown (headings, bullets, `code`, fenced code blocks).",
    "plain": BUILTIN_STYLES["plain"],
}

DEFAULT_STYLE = {"name": "concise", "language": "English", "verbosity": "concise", "format": "markdown",
                 "tone": "", "custom": ""}


def _load(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def current_style(config) -> Dict[str, Any]:
    st = dict(DEFAULT_STYLE)
    st.update(config.get("style") or {})
    st.update(_load(config.home / "style.json"))
    st.update(_load(config.project_root / ".spy" / "style.json"))
    st.update(getattr(config, "style_overrides", {}) or {})
    return st


def style_dirs(config) -> List[Path]:
    return [config.home / "styles", config.project_root / ".spy" / "styles"]


def available_styles(config) -> Dict[str, str]:
    out = dict(BUILTIN_STYLES)
    for d in style_dirs(config):
        if d.is_dir():
            for f in d.glob("*.md"):
                try:
                    out[f.stem] = f.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
    return out


def style_prompt(config) -> str:
    st = current_style(config)
    lines = []
    lang = (st.get("language") or "English").strip()
    lines.append("- Always respond in %s%s." % (lang, "" if lang.lower() == "english" else
                                              " (keep code, commands, file paths and quoted source text unchanged)"))
    if VERBOSITY.get(st.get("verbosity", "")):
        lines.append("- " + VERBOSITY[st["verbosity"]])
    fmt = st.get("format", "markdown")
    if FORMAT.get(fmt):
        lines.append("- " + FORMAT[fmt])
    if st.get("tone"):
        lines.append("- Tone: %s." % st["tone"])
    named = available_styles(config).get(st.get("name") or "default", "")
    if named:
        lines.append("- " + named)
    if st.get("custom"):
        lines.append("- " + st["custom"])
    lines.append("- Never output raw tool-call JSON, XML tags, or role labels like \"Assistant:\" in your replies.")
    return "# Response style\n" + "\n".join(lines)


def save_style(config, updates: Dict[str, Any], scope: str = "global") -> Path:
    p = (config.home / "style.json") if scope == "global" else (config.project_root / ".spy" / "style.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _load(p)
    data.update(updates)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


# --------------------------------------------------------------- cleanup -----
_SPECIAL_TOKENS = re.compile(r"<\|[a-z_]{2,30}\|>|</?(tool_call|tool_result|function_call|response|answer|final)>", re.I)
_ROLE_PREFIX = re.compile(r"^\s*(assistant|ai|spy|response|answer)\s*:\s*", re.I)
_WRAPPED = re.compile(r"^\s*```(?:markdown|md|text)?\s*\n(.*)\n```\s*$", re.S | re.I)
_FILLER_END = re.compile(
    r"\n*\s*(let me know if (you (need|have|want)|there('s| is)) [^\n]*|i hope (this|that) helps[^\n]*|"
    r"feel free to (ask|reach out)[^\n]*|is there anything else[^\n]*)\s*$", re.I)


FILLER_LINE = re.compile(
    r"^(let me know if (you (need|have|want)|there('s| is))|i hope (this|that) helps|feel free to (ask|reach out)|"
    r"is there anything else)[^\n]*$", re.I)


def clean_response(text: str, strip_filler: bool = True) -> str:
    """Remove artifacts weak models commonly leave in final answers."""
    if not text:
        return text
    t = _SPECIAL_TOKENS.sub("", text)
    t = _ROLE_PREFIX.sub("", t, count=1)
    m = _WRAPPED.match(t)
    if m and "```" not in m.group(1):
        t = m.group(1)  # whole answer wrapped in a markdown fence
    if strip_filler:
        t = _FILLER_END.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
