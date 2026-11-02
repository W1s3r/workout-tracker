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

## 3. Configure intranet search (the big one)

Edit `search.sources` in the config (see `config.example.json` for every option), then run `python -m hsa
doctor` to see sample results.

| Your search engine… | Use |
|---|---|
| only has a web page | `"type": "html"` with `"url": "https://host/search?q={query}&page={page}"`. Link and snippet extraction is heuristic and drops nav and pagination links. For precision, add `result_selector_regex` with named groups `url`, `title`, `snippet`. |
| has a JSON API | `"type": "json"` with `url` (and `body` for POST), `results_path`, and a `fields` mapping. |
| is Elasticsearch / Solr / SharePoint | `"type": "elasticsearch"` / `"solr"` / `"sharepoint"` |
| needs Windows login (Kerberos/NTLM) or a smart card | Add `"http_command": "powershell ... scripts\\win_fetch.ps1 -Url {url} -Out {out}"` to that source, or to `search.fetch` for page fetching. |
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

## 4. Use it

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

## 5. Tools the agent has

`read_file` `write_file` `edit_file` `list_dir` `glob` `grep` `find_files` · `shell` `shell_output`
`run_python` · `intranet_search` `fetch_url` `doc_search` `deep_research` · `gitlab` · `memory`
`todo_write` `ask_user` `task` (sub-agents). Run `/tools` to see them, and add names to
`agent.disabled_tools` to turn any of them off.

## 6. Tests

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
  config.py       layered JSON config with ${ENV:...} secrets
  context.py      AGENT.md loading, memory/todo/ask_user tools
  permissions.py  modes + allow/deny rules     ui.py  terminal UI
  llm/            base types, JSON repair, prompted-tool protocol, OpenAI client, mock, custom template
  tools/          files, shell, research tools, gitlab, sub-agents
  research/       search sources, fetcher+cache, BM25, doc index, deep research engine
  extract.py      HTML/docx/pptx/xlsx/pdf/ipynb text extraction (stdlib)
  net.py          urllib HTTP with CA bundles, client certs, proxies, retries, http_command
```
