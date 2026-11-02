# HSA — High Side Agent

A Claude Code–style terminal agent for air-gapped and internal networks. It's written in plain Python with
**no required third-party packages** (Python 3.7+ standard library only), and works with any LLM behind an
HTTP API (OpenAI-compatible out of the box: vLLM, TGI, LiteLLM, Ollama, most enterprise gateways).

What it does:

- **Coding agent loop.** Reads, writes, and edits files; runs shell commands and Python; supports git and the
  GitLab API; keeps a todo list; and can hand work to sub-agents.
- **File navigation across local and network drives.** Finds files by name across configured network
  locations (UNC and mapped drives), uses `glob`/`grep` with time budgets for slow shares, and reads
  Word/PowerPoint/Excel/PDF/HTML/notebook files.
- **Intranet search and deep research.** Has pluggable search connectors (HTML scraping, JSON APIs,
  Elasticsearch, Solr, SharePoint, or your own script). A multi-round research engine turns a question into
  a cited markdown report.
- **Local document index.** Builds a BM25 full-text index (sqlite) of network shares so searching them is
  instant.
- **Persistent, self-updating memory.** `AGENT.md` files work like Claude Code's `CLAUDE.md`. The agent
  updates them itself through the `memory` tool, and you can add notes with `# note`.
- **Safety and control.** Permission modes (default / accept_edits / auto / plan), allow/deny rules,
  diff previews, pre/post tool hooks, and Ctrl+C interrupt.
- **Session management.** Auto-compaction of long conversations, `/compact`, `/context`, `/cost`, saved
  sessions with `--continue` / `--resume`, and one-shot `-p` mode for scripting.
- **Model-agnostic.** Uses native tool calling when the API supports it. Otherwise it falls back to
  prompt-based tool calling with a forgiving parser, so smaller or older models still work.

---

## 1. Install (no internet needed)

1. Bring the `highside-agent/` folder across (it's small: just `.py` files).
2. Put it somewhere permanent, e.g. `C:\tools\highside-agent`.
3. Open an **Anaconda Prompt** (or any terminal where `python` is Anaconda's) and run:

```bat
cd C:\tools\highside-agent
python -m hsa setup          :: writes %USERPROFILE%\.hsa\config.json + a global AGENT.md
notepad %USERPROFILE%\.hsa\config.json   :: fill in the <PLACEHOLDERS>
python -m hsa doctor         :: tests the API, tool calling, streaming, search sources, drives
python -m hsa webtest "https://<SEARCH_HOST>/search?q={query}" --query test   :: how to reach the internal web
python -m hsa evalloop       :: how well your model drives the agent loop
```

4. Optional: add `C:\tools\highside-agent\scripts` to your PATH. You can then type `hsa` from any folder
   (it uses `scripts\hsa.bat`). Otherwise run `python -m hsa` with `PYTHONPATH` set to the folder.

Secrets don't have to go in the file. Set environment variables instead (`setx HSA_API_KEY ...`,
`HSA_BASE_URL`, `HSA_GITLAB_TOKEN`, `HSA_GITLAB_URL`). The default config reads them through
`${ENV:NAME}` placeholders.

Optional extras are used automatically if your Anaconda has them: `prompt_toolkit` (better line editing and
history), `rich` (markdown rendering), `openpyxl`, `pypdf`/`PyPDF2`/`PyMuPDF` (PDF text). `python -m hsa
doctor` lists which ones it found.

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
- If the gateway is **not** OpenAI-shaped, copy `hsa/llm/custom_template.py`, fill in `build_request()`
  and `parse_response()`, and set the provider to `{"type": "custom", "module": "hsa.llm.yourfile"}`.

Set `context_tokens` honestly for each model. Compaction, tool-output truncation, and research batching all
size themselves from it. Use `roles` to choose which model does what. For example, `"fast": "gpt-oss"` sends
search-result triage to a cheaper model.

## 3. Reach the internal web (do this before configuring search)

Getting a page's HTML from a script is often harder than opening it in a browser. Common reasons are a
Windows/SSO login, a smart card, an internal certificate authority, or search results that are built by
JavaScript. HSA has five ways to fetch a page:

| Method | How it works | Handles |
|---|---|---|
| `python` | plain HTTP from Python | open sites; cookie or certificate you configure |
| `curl` | `curl.exe`, built into Windows 10+, with `--negotiate` | **Windows login** (Kerberos/NTLM), Windows certificate store |
| `powershell` | `Invoke-WebRequest -UseDefaultCredentials` | Windows login |
| `headless` | a hidden Edge/Chrome that HSA drives | Windows login **and pages built with JavaScript** |
| `cdp` | **your own logged-in Edge window**, driven in background tabs | **anything you can open by hand**: smart card, SSO portals, JavaScript |

### Find what works

```bat
python -m hsa webtest "https://<SEARCH_HOST>/search?q={query}" --query "some known topic"
```

`webtest` tries each method and flags responses that are login pages or JavaScript shells. It then shows the
search results it could extract and suggests an `include_url_regex`.

### If only `cdp` works (typical with smart-card login)

```bat
python -m hsa browser "https://<SEARCH_HOST>/"
```

This opens a separate Edge window with its own profile, which HSA can control. Log in there once, as you
normally would, then minimize the window and leave it open. HSA loads pages in background tabs of that
window, so it has your session. Logins usually persist in that profile. Your existing hotkey script can run
this command.

Leave `"web": {"via": "auto"}` and HSA tries methods in the table's order, skipping ones that fail. It
remembers which method worked for each host in `~/.hsa/web_methods.json`. A source can also force a method
with `"via": "cdp"`. The agent can ask for a browser render of a single page with
`fetch_url(..., render=true)`.

## 4. Configure intranet search (the big one)

Edit `search.sources` in the config (see `config.example.json` for every option), then run `python -m hsa
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

`/research <question>`, `python -m hsa research "<question>"`, or the agent's `deep_research` tool all run
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
python -m hsa index team_share            :: a name from "locations", or any folder path
python -m hsa index \\fileserver\share\docs H:\projects
```

The command is incremental, so re-run it to pick up changes; only modified files are re-read. The agent
searches the index with `doc_search`. To include the index in deep research, add a source
`{"type": "local_index"}`.

## 5. Use it

```bat
cd C:\path\to\your\project
hsa                                   :: interactive
hsa "why does build.ps1 fail?"        :: start with a prompt
hsa -p "summarise README.md" > out.md :: one-shot, scriptable
hsa -c                                :: continue last session
hsa --model gpt-oss --mode plan       :: pick a model / permission mode
```

Inside the session:

| Input | Effect |
|---|---|
| plain text | talk to the agent |
| `# always use pytest -q` | save a note to project `AGENT.md` (`#g …` for global memory) |
| `!git status` | run a command yourself; the agent sees the output |
| `/init` | agent explores the project and writes `AGENT.md` |
| `/reflect` | agent reviews the session and saves lessons to memory |
| `/plan`, `/mode auto` | permission modes |
| `/style …` | response style, language, format |
| `/last` | why the last turn ended, how many auto-continues |
| `/web <url>` | test internal-web access for a URL |
| `/research …`, `/search …`, `/index …` | research tools directly |
| `/compact`, `/context`, `/cost`, `/clear` | context management |
| `/model gemini`, `/models` | switch models |
| `/resume`, `/sessions` | saved conversations |
| `"""` | start/end a multi-line message (or end a line with `\`) |
| Ctrl+C | interrupt the agent |

### Memory files (`AGENT.md`)

All memory files that exist are loaded into every request, in this order:

1. `~/.hsa/AGENT.md` (global)
2. `AGENT.md` (or `CLAUDE.md`) from the project root down to the current directory
3. `.hsa/AGENT.local.md` (private)

A line containing only `@other.md` imports that file. The agent is told to save durable lessons
proactively: corrections, commands, paths, and conventions.

### Custom slash commands

Put markdown files in `.hsa/commands/` (per project) or `~/.hsa/commands/` (global). `/review focus on
SQL` runs `review.md` with `$ARGUMENTS` replaced by `focus on SQL`. There are starters in
`examples/commands/` (`review`, `commit`, `brief`).

### The agent loop: answering vs. working

Every message is first routed:

- **Chat**: "hi", "thanks". Gets a quick reply. No tools are offered, so a small model can't wander off.
- **Task**: "fix…", "create…", "search…", a file path, "continue", or "yes" in reply to the agent's own
  question.
- **Auto**: everything else. The model decides: answer directly, or start using tools.

Once the model is working with tools, it continues on its own until it stops calling them. At that point
HSA checks whether it's really finished. These are the common small-model failures, and how HSA responds:

| The model… | HSA |
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
python -m hsa evalloop                          :: 7 scenarios: chat, knowledge, create+run, find, multi-step, bug fix, blocked
python -m hsa evalloop --completion finish_tool  :: compare ending styles
python -m hsa evalloop --model gpt-oss           :: compare models
python -m hsa evalloop --only fix_bug --verbose  :: watch one scenario
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
| `/style save` | keep the settings in `~/.hsa/style.json`; `/style save project` keeps them for this project only |

To make your own named style, put a markdown file of instructions in `~/.hsa/styles/NAME.md` and select it
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
`pre_tool` hook blocks the call. Hooks receive `HSA_TOOL` and `HSA_TOOL_INPUT` environment variables.

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
research. `python -m hsa --model mock` starts the UI with an offline mock model: type `@tool list_dir {}`
to exercise a tool.

## Layout

```
hsa/
  cli.py          REPL, slash commands, subcommands (doctor/setup/index/research)
  agent.py        agent loop, permissions/hook dispatch, compaction, robustness
  turn_policy.py  chat-vs-task routing and "is it really done?" auto-continue rules
  style.py        response style/language/format + output cleanup;  mdrender.py  terminal markdown
  webaccess.py    internal-web access: python/curl/powershell/headless/cdp (+ stdlib WebSocket)
  evals.py        `hsa evalloop` behaviour scenarios
  config.py       layered JSON config with ${ENV:...} secrets
  context.py      AGENT.md loading, memory/todo/ask_user tools
  permissions.py  modes + allow/deny rules     ui.py  terminal UI
  llm/            base types, JSON repair, prompted-tool protocol, OpenAI client, mock, custom template
  tools/          files, shell, research tools, gitlab, sub-agents
  research/       search sources, fetcher+cache, BM25, doc index, deep research engine
  extract.py      HTML/docx/pptx/xlsx/pdf/ipynb text extraction (stdlib)
  net.py          urllib HTTP with CA bundles, client certs, proxies, retries, http_command
```
