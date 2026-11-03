"""Configuration loading.

Config is JSON (no TOML/YAML dependency). Layers, later wins:
  1. built-in DEFAULTS (below)
  2. ~/.spy/config.json            (user/global)
  3. <project>/.spy/config.json    (project, committed)
  4. <project>/.spy/config.local.json (project, NOT committed - secrets)
  5. --config PATH on the command line

Any string value of the form "${ENV:NAME}" or "${ENV:NAME:default}" is replaced
with the environment variable, so secrets never have to live in a file.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME_DIR = Path(os.environ.get("SPY_HOME", str(Path.home() / ".spy")))

DEFAULTS: Dict[str, Any] = {
    # ---- LLM providers -----------------------------------------------------
    # A provider is an HTTP endpoint. "openai" type works with any
    # OpenAI-compatible /chat/completions server (vLLM, TGI, LiteLLM, Ollama,
    # most enterprise gateways). "mock" is an offline fake for testing.
    "providers": {
        "work": {
            "type": "openai",
            "base_url": "${ENV:SPY_BASE_URL:https://<LLM_HOST>/v1}",
            "api_key": "${ENV:SPY_API_KEY:<API_KEY>}",
            "headers": {},              # extra headers, e.g. {"X-Api-Key": "..."}
            "verify_ssl": True,
            "ca_bundle": None,          # path to internal CA .pem/.crt
            "client_cert": None,        # PKI cert (.pem) if the gateway needs it
            "client_key": None,
            "use_env_proxy": False,     # internal hosts usually must bypass proxies
            "timeout": 300,
            "max_retries": 3,
        },
        "mock": {"type": "mock"},
    },
    # ---- Models --------------------------------------------------------------
    # tool_mode: "native"  -> use the API's tools/tool_calls fields
    #            "prompted"-> describe tools in the prompt, parse <tool_call> tags
    #            "auto"    -> native, but also parse tags if the model emits them
    "models": {
        # Inkling-Small (Thinking Machines): 276B-total / 12B-active MoE, reasons by default,
        # native tool calling. Architecture supports a 1M-token context; deployments usually cap it
        # (hosted tiers are 64K / 256K). "auto" reads the real limit from the server (vLLM's
        # max_model_len); 64K is used if the server doesn't report it. Reasoning tokens count
        # against max_output_tokens, so it is set generously.
        "inkling-small": {
            "provider": "work",
            "model": "<MODEL_ID_FOR_INKLING_SMALL>",
            "context_tokens": "auto",
            "context_fallback": 65536,
            "context_cap": 262144,      # work within 256K even if the server allows 1M (speed/quality)
            "max_output_tokens": 16384,
            "vision": True,             # accepts image input (pasted screenshots, read_file on images)
            "audio": True,              # accepts audio input (used for /voice transcription)
            "temperature": 0.2,
            "tool_mode": "auto",
            "stream": True,
            "extra_body": {},           # merged into every request body
        },
        "gpt-oss": {
            "provider": "work",
            "model": "<MODEL_ID_FOR_GPT_OSS>",
            "vision": False,            # text-only model
            "context_tokens": "auto",
            "context_fallback": 32768,
            "max_output_tokens": 8192,
            "temperature": 0.2,
            "tool_mode": "auto",
            "stream": True,
            "extra_body": {},
        },
        "gemini": {
            "provider": "work",
            "model": "<MODEL_ID_FOR_GEMINI>",
            "vision": True,
            "context_tokens": "auto",
            "context_fallback": 128000,
            "max_output_tokens": 8192,
            "temperature": 0.2,
            "tool_mode": "auto",
            "stream": True,
            "extra_body": {},
        },
        "mock": {"provider": "mock", "model": "mock", "context_tokens": 16000,
                 "max_output_tokens": 2048, "tool_mode": "native", "stream": False},
    },
    # Which model is used for which job. "fast" is used for cheap sub-tasks
    # (search-result triage, summarising fetched pages). Can be the same model.
    "roles": {"main": "inkling-small", "fast": "inkling-small", "research": "inkling-small",
              "voice": "inkling-small"},

    # ---- Agent behaviour -----------------------------------------------------
    "agent": {
        "max_steps_per_turn": 40,
        "max_tool_output_chars": 16000,
        "compact_at_fraction": 0.75,     # auto-compact when context is this full
        "keep_recent_messages": 8,
        "context_file_names": ["SPY.md"],
        "show_reasoning": False,
        # ---- turn loop (see spy/turn_policy.py) ----
        "tool_profile": "auto",          # full | lean | auto (lean when the model's context < 16k tokens)
        "router": True,                  # greetings/thanks get a plain reply without tools
        "completion": "text",            # "text": a final reply ends the turn; "finish_tool": model must call finish
        "max_auto_continue": 5,          # max automatic "keep going" nudges per turn
        "max_nudges_per_reason": 2,
        "verify_nudge": True,            # nudge once to run/test code that was edited but never run
        "stuck_after_errors": 4,         # consecutive failed tool calls before a "step back" note
        "strip_filler": True,            # drop "Let me know if you need anything else!" endings
    },
    # ---- response style (also /style at runtime) ----
    # name: concise | explanatory | bluf | plain | report | default | <your ~/.spy/styles/NAME.md>
    "style": {"name": "concise", "language": "English", "verbosity": "concise",
              "format": "markdown", "tone": "", "custom": ""},
    # default | accept_edits | auto | plan
    "permissions": {
        "mode": "default",
        "allow": ["bash(git status*)", "bash(git diff*)", "bash(git log*)",
                  "bash(ls*)", "bash(dir*)", "bash(pwd)"],
        "deny": ["bash(rm -rf /*)", "bash(git push --force*)", "bash(format *)"],
    },
    "shell": {
        # null = auto: bash/sh on Linux/mac, powershell (or cmd) on Windows
        "program": None,
        "default_timeout": 120,
    },
    # Named roots the agent may search for files (local + network drives).
    # e.g. {"projects": "\\\\fileserver\\projects", "home": "H:\\"}
    "locations": {},

    # ---- Intranet search -----------------------------------------------------
    # See README "Configuring search". Each source is queried by intranet_search
    # and deep_research. Type: json | html | elasticsearch | solr | sharepoint
    "search": {
        "sources": {
            "intranet": {
                "type": "html",
                "enabled": False,
                "url": "https://<SEARCH_HOST>/search?q={query}&page={page}",
                "result_selector_regex": None,
                "include_url_regex": None,
                "exclude_url_regex": None,
                "headers": {},
                "cookie": None,
                "verify_ssl": True,
                "ca_bundle": None,
                "client_cert": None,
                "client_key": None,
                "timeout": 30,
                "max_pages": 2,
            }
        },
        "fetch": {"timeout": 30, "max_bytes": 15000000, "verify_ssl": True,
                  "ca_bundle": None, "client_cert": None, "client_key": None,
                  "headers": {"User-Agent": "Spy-Research/0.1"}, "cookie": None},
        "cache_days": 7,
    },
    "research": {
        "max_rounds": 3,
        "queries_per_round": 8,
        "results_per_query": 10,
        "pages_to_read_per_round": 8,
        "chunk_chars": 3000,
        "follow_links": True,
        "output_dir": "research",
        "glossary": {},     # {"ACRONYM": "expansion", ...} helps query expansion
    },
    "doc_index": {
        "path": None,       # default: ~/.spy/docindex.sqlite
        "extensions": [".txt", ".md", ".rst", ".csv", ".log", ".json", ".xml",
                       ".html", ".htm", ".py", ".ps1", ".sh", ".sql", ".yaml",
                       ".yml", ".ini", ".cfg", ".docx", ".pptx", ".xlsx", ".pdf"],
        "max_file_mb": 25,
    },
    # ---- Images (Alt+V / /paste to paste a screenshot, @image.png, read_file on images) ----
    "images": {"max_px": 1568, "keep_last": 4},
    # ---- Voice input (/voice) - see spy/voice.py ----
    "voice": {
        "engine": "llm",            # llm (model with audio input, e.g. Inkling) | transcriptions | command
        "model_role": "voice",      # which model/role transcribes for engine "llm"
        "audio_part": "input_audio",  # input_audio | audio_url  (`spy doctor` tells you which works)
        "url": "https://<TRANSCRIBE_HOST>/v1/audio/transcriptions",  # engine "transcriptions"
        "model": "whisper-1",
        "api_key": "${ENV:SPY_API_KEY:}",
        "language": None,
        "command": None,            # engine "command": e.g. "python my_stt.py {wav}"
        "record_command": None,     # override recording, e.g. ffmpeg ... {out}
        "max_seconds": 120,
        "sample_rate": 16000,
    },
    # ---- Internal web access (see spy/webaccess.py, `spy webtest URL`) ----
    "web": {
        "via": "auto",              # auto | python | curl | powershell | headless | cdp
        "auto_order": ["python", "curl", "powershell", "cdp", "headless"],
        "browser_path": None,       # null = find Edge/Chrome automatically
        "cdp_port": 9222,
        "profile_dir": None,        # default ~/.spy/browser-profile
        "auto_launch": True,        # start a headless browser for 'cdp' if none is running
        "render_wait": 3,           # seconds to let JavaScript-built pages settle
        "auth_server_allowlist": "*",  # hosts the browser may send Windows login to, e.g. "*.corp.local"
        "headless_timeout": 60,
    },
    # ---- GitLab ------------------------------------------------------------
    "gitlab": {
        "url": "${ENV:SPY_GITLAB_URL:https://<GITLAB_HOST>}",
        "token": "${ENV:SPY_GITLAB_TOKEN:<GITLAB_TOKEN>}",
        "verify_ssl": True,
        "ca_bundle": None,
    },
    # ---- Hooks (shell commands run around tool calls) --------------------------
    # {"pre_tool": [{"match": "write_file|edit_file", "command": "..."}],
    #  "post_tool": [...]}  Hook gets SPY_TOOL, SPY_TOOL_INPUT env vars.
    # A pre_tool hook exiting non-zero blocks the tool call.
    "hooks": {"pre_tool": [], "post_tool": []},
    "ui": {"color": True, "markdown": True},
}

_ENV_RE = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m):
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def find_project_root(start: Optional[Path] = None) -> Path:
    """The project is the nearest folder (from cwd upward) containing .spy/, SPY.md or .git;
    otherwise the folder Spy was started in. Each project is a separate Spy instance with
    its own SPY.md, sessions, and input history. The home folder itself never counts."""
    start = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    for p in [start] + list(start.parents):
        if p == home or p == HOME_DIR.parent.resolve():
            break
        if (p / ".spy").is_dir() or (p / "SPY.md").is_file() or (p / "spy.md").is_file() or (p / ".git").exists():
            return p
    return start


def project_data_dir(home: Path, project_root: Path) -> Path:
    """Per-project private state (sessions, input history) under ~/.spy/projects/."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(project_root)).strip("-")[-80:] or "root"
    d = home / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def strip_json_comments(text: str) -> str:
    """Remove // line comments (outside strings) and trailing commas, so config
    files can be annotated."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    text = strip_json_comments(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit("Config file %s is not valid JSON: %s" % (path, e))


class Config:
    def __init__(self, data: Dict[str, Any], project_root: Path, sources: List[str]):
        self.data = data
        self.project_root = project_root
        self.sources = sources
        self.home = HOME_DIR

    def __getitem__(self, key):
        return self.data[key]

    def get(self, *keys, default=None):
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def model_profile(self, name_or_role: str) -> Dict[str, Any]:
        name = self.data["roles"].get(name_or_role, name_or_role)
        models = self.data["models"]
        if name not in models:
            raise KeyError("Unknown model '%s'. Configured: %s" % (name, ", ".join(models)))
        prof = dict(models[name])
        prof["name"] = name
        prov_name = prof.get("provider", "work")
        prov = self.data["providers"].get(prov_name)
        if prov is None:
            raise KeyError("Model '%s' references unknown provider '%s'" % (name, prov_name))
        prof["provider_config"] = dict(prov)
        return prof


def load_config(extra_path: Optional[str] = None, cwd: Optional[Path] = None) -> Config:
    root = find_project_root(cwd)
    data = copy.deepcopy(DEFAULTS)
    sources = ["defaults"]
    for p in [HOME_DIR / "config.json", root / ".spy" / "config.json",
              root / ".spy" / "config.local.json"] + ([Path(extra_path)] if extra_path else []):
        if p.is_file():
            data = deep_merge(data, _load_json(p))
            sources.append(str(p))
    data = _expand_env(data)
    cfg = Config(data, root, sources)
    from spy import net
    net.configure_web(cfg)
    return cfg


def write_starter_config(path: Path) -> None:
    """Write a commented starter config the user can fill in."""
    path.parent.mkdir(parents=True, exist_ok=True)
    example = Path(__file__).resolve().parent.parent / "config.example.json"
    if example.is_file():
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        return
    starter = {
        "providers": {"work": DEFAULTS["providers"]["work"]},
        "models": {k: v for k, v in DEFAULTS["models"].items() if k != "mock"},
        "roles": DEFAULTS["roles"],
        "locations": {"example_share": "\\\\<FILESERVER>\\<SHARE>"},
        "search": {"sources": DEFAULTS["search"]["sources"]},
        "gitlab": DEFAULTS["gitlab"],
    }
    path.write_text(json.dumps(starter, indent=2), encoding="utf-8")
