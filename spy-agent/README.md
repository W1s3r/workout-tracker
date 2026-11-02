# Spy

**Spy** is a Claude Code–style terminal agent for air-gapped and internal networks. Open a terminal, `cd` to
a folder, type `spy`, and you get an interactive agent in the terminal. No IDE is needed.

It's written in plain Python with **no required third-party packages** (Python 3.7+ standard library;
Anaconda's `prompt_toolkit` is used for the input box when present). It works with any LLM behind an HTTP API:
OpenAI-compatible out of the box (vLLM, TGI, LiteLLM, Ollama, most enterprise gateways).

What it does:

- **Coding agent loop.** Reads files and makes precise edits; runs shell commands and Python; supports git
  and the GitLab API; keeps a todo list; and can hand work to sub-agents. It keeps working until the task is
  done or it is genuinely blocked.
- **Safe editing.** Edits change only the exact text targeted. Guards stop the classic "replaced the whole
  file with just the snippet" mistake, every change is backed up, and `/undo` reverts. Word `.docx` files
  can be edited in place with their formatting kept.
- **File navigation across local and network drives.** Finds files by name across configured network
  locations (UNC and mapped drives), uses `glob`/`grep` with time budgets for slow shares, and reads
  Word/PowerPoint/Excel/PDF/HTML/notebook files.
- **Intranet search and deep research.** Has five ways to reach internal web pages, up to driving your
  logged-in Edge, plus pluggable search connectors. A multi-round research engine turns a question into a
  cited report.
- **Memory: `SPY.md`.** A master `~/.spy/SPY.md` applies everywhere, and each project has its own `SPY.md`.
  Spy updates them itself, and you can add notes with `# note`.
- **Separate instances per project.** Each folder you start Spy in has its own `SPY.md`, saved sessions, and
  input history.
- **Safety and control.** Permission modes (default / accept edits / plan / auto, cycled with Shift+Tab),
  allow/deny rules, diff previews, hooks, and Ctrl+C interrupt.
- **Model-agnostic.** Uses native tool calling when the API supports it. Otherwise it falls back to
  prompt-based tool calling with a forgiving parser.

---

## 1. Install (no internet needed)

1. Bring the `spy-agent/` folder across. It's small: just `.py` and `.md` files.
2. Put it somewhere permanent, e.g. `C:\tools\spy-agent`.
3. Open an **Anaconda Prompt** once (so `python` is Anaconda's) and run:

```bat
cd C:\tools\spy-agent
python install.py
```

The installer:

- creates the `spy` command (`%USERPROFILE%\.spy\bin\spy.cmd`) and adds it to your user PATH (no admin
  rights needed)
- pins it to the Anaconda Python you ran it with, so `spy` works from **any** terminal: cmd, PowerShell, or
  Windows Terminal
- creates `%USERPROFILE%\.spy\config.json` and your master `SPY.md`

`python install.py --uninstall` removes the command again.

4. Open a **new** terminal, fill in the config, and check it:

```bat
notepad %USERPROFILE%\.spy\config.json      :: fill in the <PLACEHOLDERS> (API URL, key, model ids)
spy doctor                                  :: API, tool calling, streaming, search sources, drives
spy webtest "https://<SEARCH_HOST>/search?q={query}" --query test   :: how to reach the internal web
spy evalloop                                :: how well your model drives the agent loop
```

5. Use it:

```bat
cd C:\path\to\your\project
spy
```

Secrets don't have to go in the file. Set environment variables instead (`setx SPY_API_KEY ...`,
`SPY_BASE_URL`, `SPY_GITLAB_TOKEN`, `SPY_GITLAB_URL`). The default config reads them through
`${ENV:NAME}` placeholders.

### The terminal interface

```
╭────────────────────────────────────────────────────────────────╮
│ ✻ Welcome to Spy 0.2.0                                         │
│   /help for commands · @file to attach · shift+tab for modes   │
│   cwd: C:\work\tool   project SPY.md: C:\work\tool\SPY.md      │
╰────────────────────────────────────────────────────────────────╯
> fix the failing test in calc.py
● read_file(calc.py)
  ⎿      1  def average(nums): ...
● edit_file(calc.py)
  ⎿ Edited calc.py: 1 replacement. The rest of the file is unchanged.
● shell: python -m pytest -q
  ⎿ 3 passed
Fixed the off-by-one in average(); tests pass.
┌────────────────────────────────────────────────────────────────┐
│>                                                               │
└────────────────────────────────────────────────────────────────┘
  ⏵⏵ accept edits on (shift+tab to cycle)   inkling-small · ctx 23%
```

| Key | Action |
|---|---|
| Enter | send |
| Alt+Enter, Ctrl+J, or `\` then Enter | new line |
| Shift+Tab | cycle mode: default → accept edits → plan |
| `/` | command menu with descriptions; Tab completes |
| `@path` | attach a file (Tab completes paths) |
| ↑ ↓ | history (kept per project) |
| Ctrl+C | interrupt Spy while it works; clear the box; press twice to exit |

This interface needs Anaconda's `prompt_toolkit`. Without it, Spy uses a plain prompt with the same commands.

### Projects and `SPY.md` (Spy's equivalent of CLAUDE.md)

| File | Scope |
|---|---|
| `~/.spy/SPY.md` | **Master.** Your standing rules and preferences, loaded in every project. Add to it with `#g note`. |
| `<project>/SPY.md` | This project: commands, conventions, paths. `/init` writes it; add to it with `# note`. |
| `<project>/sub/SPY.md` | Extra rules when you work inside that sub-folder. |
| `<project>/.spy/SPY.local.md` | Your private notes for the project (don't commit). |

The project is the nearest folder, going up from where you typed `spy`, that has a `SPY.md`, a `.spy/`
folder, or `.git`. If there is none, the project is the folder itself.

Spy is told that master rules apply everywhere and that project files add specifics, with the more specific
file winning on project matters. Each project also has its own saved sessions (`spy -c` continues *this*
project's last conversation) and its own input history.

### Editing safety

`edit_file` replaces one exact, unique piece of text and leaves the rest of the file byte-for-byte unchanged.
That includes line endings and encoding. In addition:

| Classic mistake | What Spy does |
|---|---|
| Model "edits" by writing only the changed function with `write_file` | **Refused.** The file is untouched and the model is told to use `edit_file`. |
| Content containing `# ... rest of code unchanged ...` | **Refused**: that would delete the elided code |
| Content pasted with `read_file` line numbers | Refused (or stripped automatically when editing) |
| Wrong indentation in `old_string` | Matched anyway, and the replacement is re-indented to fit |
| Any change that deletes more than half of a file | **You are always asked**, even in auto mode |
| Writing plain text into a `.docx`, `.xlsx`, or `.pdf` | Refused. `.docx` text is edited in place instead, with formatting kept. |

Every change is backed up for the session: `/changes` lists them and `/undo` reverts the most recent.

## 2. Configure the LLM

The **`doctor`** command is the key step. For each model it checks:

- basic chat
- streaming
- **native tool calls**, including a full tool-result round trip
- **prompted tool calls**

Then it recommends the `tool_mode` and `stream` values to use.

- `"tool_mode": "auto"`: native tools, and also parses `<tool_call>` text if the model emits that instead.
  This is the default.
- `"tool_mode": "prompted"`: tools are described in the system prompt and calls are parsed from the text.
  Use this if the API ignores or rejects `tools`.
- If the gateway is **not** OpenAI-shaped, copy `spy/llm/custom_template.py`, fill in `build_request()`
  and `parse_response()`, and set the provider to `{"type": "custom", "module": "spy.llm.yourfile"}`.

**Context window.** `"context_tokens": "auto"` (the default) reads the real limit from the server: vLLM
reports `max_model_len` on `/models`. If the server doesn't report it, `context_fallback` is used.
`spy doctor` and `/context` show which value is in effect. For **Inkling-Small**:

- The model supports up to **1M tokens**. Hosted versions offer 64K and 256K tiers, but your deployment's
  limit is whatever its server was started with.
- Spy falls back to **64K** and, by default, works within **256K** at most (`context_cap`). Long local
  prompts get slow, and Spy compacts long conversations anyway.
- It **reasons by default**, and reasoning counts against `max_output_tokens`, so the default is a generous
  16K. If reasoning still uses the whole budget, Spy retries once with double the budget.
- If tool calls arrive as raw text, the vLLM server needs `--enable-auto-tool-choice --tool-call-parser
  inkling --reasoning-parser inkling`. Until then, `"tool_mode": "prompted"` works.

Otherwise, set `context_tokens` honestly for each model. Compaction, tool-output truncation, and research batching all
size themselves from it. Use `roles` to choose which model does what. For example, `"fast": "gpt-oss"` sends
search-result triage to a cheaper model.

## 3. Reach the internal web (do this before configuring search)

Getting a page's HTML from a script is often harder than opening it in a browser. Common reasons are a
Windows/SSO login, a smart card, an internal certificate authority, or search results that are built by
JavaScript. Spy has five ways to fetch a page:

| Method | How it works | Handles |
|---|---|---|
| `python` | plain HTTP from Python | open sites; cookie or certificate you configure |
| `curl` | `curl.exe`, built into Windows 10+, with `--negotiate` | **Windows login** (Kerberos/NTLM), Windows certificate store |
| `powershell` | `Invoke-WebRequest -UseDefaultCredentials` | Windows login |
| `headless` | a hidden Edge/Chrome that Spy drives | Windows login **and pages built with JavaScript** |
| `cdp` | **your own logged-in Edge window**, driven in background tabs | **anything you can open by hand**: smart card, SSO portals, JavaScript |

### Find what works

```bat
spy webtest "https://<SEARCH_HOST>/search?q={query}" --query "some known topic"
```

`webtest` tries each method and flags responses that are login pages or JavaScript shells. It then shows the
search results it could extract and suggests an `include_url_regex`.

### If only `cdp` works (typical with smart-card login)

```bat
spy browser "https://<SEARCH_HOST>/"
```

This opens a separate Edge window with its own profile, which Spy can control. Log in there once, as you
normally would, then minimize the window and leave it open. Spy loads pages in background tabs of that
window, so it has your session. Logins usually persist in that profile. Your existing hotkey script can run
this command.

Leave `"web": {"via": "auto"}` and Spy tries methods in the table's order, skipping ones that fail. It
remembers which method worked for each host in `~/.spy/web_methods.json`. A source can also force a method
with `"via": "cdp"`. The agent can ask for a browser render of a single page with
`fetch_url(..., render=true)`.

## 4. Configure intranet search (the big one)

Edit `search.sources` in the config (see `config.example.json` for every option), then run `spy
doctor` to see sample results.

| Your search engine… | Use |
|---|---|
| only has a web page | `"type": "html"` with `"url": "https://host/search?q={query}&page={page}"`. Link and snippet extraction is heuristic and drops nav and pagination links. For precision, add `result_selector_regex` with named groups `url`, `title`, `snippet`. |
| has a JSON API | `"type": "json"` with `url` (and `body` for POST), `results_path`, and a `fields` mapping. |
| is Elasticsearch / Solr / SharePoint | `"type": "elasticsearch"` / `"solr"` / `"sharepoint"` |
| needs Windows login, a smart card, or builds results with JavaScript | Covered by the web-access methods in section 3. `"via": "auto"` (the default) finds a working one, or you can force one per source with `"via": "cdp"`. |
| is something else entirely | `"type": "command"`: any script that prints a JSON list of `{title, url, snippet}`. |

Placeholders available in URLs and bodies: `{query}`, `{query_raw}`, `{page}`, `{page0}`, `{count}`,
`{offset}`.

**Glossary:** put your organisation's acronyms in `research.glossary` (`{"ACRONYM": "expansion"}`). On a
keyword-only search engine, searching both forms makes a big difference.

### How deep research works

`/research <question>`, `spy research "<question>"`, or the agent's `deep_research` tool all run
the same loop. Each round:

1. **Plan.** Break the question into sub-questions and generate 12–20 *short keyword* queries, including
   acronym and expansion pairs, synonyms, and document-type words.
2. **Search.** Run every query on every source in parallel, pages 1–2, and de-duplicate. A document found by
   several queries gets a higher score.
3. **Rank.** Re-rank locally with BM25 (the engine's own ranking is ignored), then have the fast model triage
   titles and snippets.
4. **Read.** Fetch the best pages (HTML/PDF/Office/network files), pull the most relevant passages, and
   extract cited facts, new search terms, and **links worth following** (a one-hop crawl, which matters when
   the search engine misses things).
5. **Reflect.** Check which sub-questions are still unanswered and generate new queries for the next round.

After the last round, the engine writes a **cited report** to `research/<date>_<slug>.md`, plus
`.notes.json` with every query, source, and fact. Ctrl+C during research stops searching and still writes a
report from what was found.

### Index your network drives

```bat
spy index team_share            :: a name from "locations", or any folder path
spy index \\fileserver\share\docs H:\projects
```

The command is incremental, so re-run it to pick up changes; only modified files are re-read. The agent
searches the index with `doc_search`. To include the index in deep research, add a source
`{"type": "local_index"}`.

## 5. Use it

```bat
cd C:\path\to\your\project
spy                                   :: interactive
spy "why does build.ps1 fail?"        :: start with a prompt
spy -p "summarise README.md" > out.md :: one-shot, scriptable
spy -c                                :: continue last session
spy --model gpt-oss --mode plan       :: pick a model / permission mode
```

Inside the session:

| Input | Effect |
|---|---|
| plain text | talk to the agent |
| `# always use pytest -q` | save a note to the project `SPY.md` (`#g …` saves to the master `~/.spy/SPY.md`) |
| `!git status` | run a command yourself; the agent sees the output |
| `/init` | agent explores the project and writes `SPY.md` |
| `/reflect` | agent reviews the session and saves lessons to memory |
| `/plan`, `/mode auto` | permission modes |
| `/undo`, `/changes` | revert / list file changes Spy made |
| `/style …` | response style, language, format |
| `/last` | why the last turn ended, how many auto-continues |
| `/web <url>` | test internal-web access for a URL |
| `/research …`, `/search …`, `/index …` | research tools directly |
| `/compact`, `/context`, `/cost`, `/clear` | context management |
| `/model gemini`, `/models` | switch models |
| `/resume`, `/sessions` | saved conversations |
| `"""` | start/end a multi-line message (or end a line with `\`) |
| Ctrl+C | interrupt the agent |

### Memory details

The `SPY.md` hierarchy is described in "Projects and `SPY.md`" above. A line containing only `@other.md`
imports that file. Spy is told to save durable lessons proactively (corrections, commands, paths,
conventions) with its `memory` tool, and `/reflect` asks it to review the session for anything worth saving.
`/memory`, `/memory master`, and `/memory local` show a file and its path; set `EDITOR` to open it directly.

### Custom slash commands

Put markdown files in `.spy/commands/` (per project) or `~/.spy/commands/` (global). `/review focus on
SQL` runs `review.md` with `$ARGUMENTS` replaced by `focus on SQL`. There are starters in
`examples/commands/` (`review`, `commit`, `brief`).

### The agent loop: answering vs. working

Every message is first routed:

- **Chat**: "hi", "thanks". Gets a quick reply. No tools are offered, so a small model can't wander off.
- **Task**: "fix…", "create…", "search…", a file path, "continue", or "yes" in reply to the agent's own
  question.
- **Auto**: everything else. The model decides: answer directly, or start using tools.

Once the model is working with tools, it continues on its own until it stops calling them. At that point
Spy checks whether it's really finished. These are the common small-model failures, and how Spy responds:

| The model… | Spy |
|---|---|
| ends with "I'll now update the file." without doing it | tells it to do it now |
| asks "Shall I proceed?" after you told it to | tells it to proceed (the permission system still asks *you* when needed) |
| stops with todo items still open | lists them and tells it to continue, or to explain what's blocking it |
| edited code but never ran it | reminds it once to run or test it, or to say it's untested |
| returns an empty reply, or the reply is cut off | asks it to continue |
| fails 4 tool calls in a row, or repeats the same call 3 times | adds a "step back and try a different approach" note |
| runs out of steps | asks for a status summary (done / remaining / blocked); type `continue` to resume |

A turn ends when the model gives a final answer, asks you a genuine question, or reports that it's blocked.
Automatic continuations are capped (`agent.max_auto_continue`), so the loop can't run forever. Each
continuation shows as a grey `(auto-continue: reason)` line, and `/last` shows how the last turn went.

If your model ends tasks too early, set `"agent": {"completion": "finish_tool"}`. The model then has to call
an explicit `finish(summary)` tool to end a task. **Measure rather than guess:**

```bat
spy evalloop                          :: 7 scenarios: chat, knowledge, create+run, find, multi-step, bug fix, blocked
spy evalloop --completion finish_tool  :: compare ending styles
spy evalloop --model gpt-oss           :: compare models
spy evalloop --only fix_bug --verbose  :: watch one scenario
```

### Response style, formatting and language

The style is added to every request:

| Command | Effect |
|---|---|
| `/style` | show the current style and the available named styles |
| `/style bluf` | use a named style: `concise` (default), `explanatory`, `bluf`, `plain` (no markdown, for pasting into email/Word), `report`, `default` |
| `/style language Spanish` | answer in another language (code, paths and quotes stay unchanged) |
| `/style verbosity detailed` | `terse` / `concise` / `normal` / `detailed` |
| `/style format plain` | `markdown` / `plain` |
| `/style tone professional, direct` | set the tone |
| `/style custom Always give file paths as UNC paths` | add a standing instruction |
| `/style save` | keep the settings in `~/.spy/style.json`; `/style save project` keeps them for this project only |

To make your own named style, put a markdown file of instructions in `~/.spy/styles/NAME.md` and select it
with `/style NAME`.

Answers are also cleaned of artifacts small models leave behind:

- leftover tool-call tags and special tokens
- "Assistant:" prefixes
- whole answers wrapped in a markdown code fence
- "Let me know if you need anything else!" filler

Markdown is rendered in the terminal (headings, bullets, code blocks), and it works while streaming.

### Permissions

- `default` asks before any change.
- `accept_edits` auto-approves file edits inside the project.
- `auto` approves everything except `deny` rules.
- `plan` is read-only: the agent proposes a plan instead of acting.

Answer a prompt with `y`, `a` (always for this session), or `n`. After `n` you can type feedback, and the
agent receives it. Rules look like `"shell(git status*)"`, `"write_file(*.md)"`, or `"gitlab(create_mr)"`.

### Hooks

`"hooks": {"post_tool": [{"match": "write_file|edit_file", "command": "python -m black -q ."}]}`. A failing
`pre_tool` hook blocks the call. Hooks receive `SPY_TOOL` and `SPY_TOOL_INPUT` environment variables.

## 6. Tools the agent has

`read_file` `write_file` `edit_file` `list_dir` `glob` `grep` `find_files` · `shell` `shell_output`
`run_python` · `intranet_search` `fetch_url` `doc_search` `deep_research` · `gitlab` · `memory`
`todo_write` `ask_user` `task` (sub-agents). Run `/tools` to see them, and add names to
`agent.disabled_tools` to turn any of them off.

## 7. Tests

```bat
python -m unittest discover -s tests -t .
```

The tests run fully offline. They include a fake OpenAI-compatible server (JSON and SSE streaming, native
and prompted tool calls) and a fake intranet with deliberately bad search ranking, used for end-to-end deep
research. `spy --model mock` starts the UI with an offline mock model: type `@tool list_dir {}`
to exercise a tool.

## Layout

```
spy/
  cli.py          REPL, slash commands, subcommands (doctor/setup/index/research)
  agent.py        agent loop, permissions/hook dispatch, compaction, robustness
  turn_policy.py  chat-vs-task routing and "is it really done?" auto-continue rules
  style.py        response style/language/format + output cleanup;  mdrender.py  terminal markdown
  webaccess.py    internal-web access: python/curl/powershell/headless/cdp (+ stdlib WebSocket)
  evals.py        `spy evalloop` behaviour scenarios
  config.py       layered JSON config with ${ENV:...} secrets
  context.py      SPY.md loading, memory/todo/ask_user tools
  permissions.py  modes + allow/deny rules     ui.py  terminal UI
  llm/            base types, JSON repair, prompted-tool protocol, OpenAI client, mock, custom template
  tools/          files, shell, research tools, gitlab, sub-agents
  research/       search sources, fetcher+cache, BM25, doc index, deep research engine
  extract.py      HTML/docx/pptx/xlsx/pdf/ipynb text extraction (stdlib)
  net.py          urllib HTTP with CA bundles, client certs, proxies, retries, http_command
```
