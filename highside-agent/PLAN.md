# HSA plan and architecture

## Goals and constraints

| Constraint | Design response |
|---|---|
| Air-gapped. New software only arrives through a media request. | Only the Python 3.7+ standard library is required. Optional Anaconda packages (`rich`, `prompt_toolkit`, `openpyxl`, `pypdf`) are detected and used when present. |
| Unknown LLM API shape; tool calling is known to work. | An OpenAI-compatible client is the default. A `custom` provider template covers other shapes. `doctor` probes the API and reports what it supports. |
| Weaker local models (Inkling Small, GPT-OSS, Gemini). | Each piece of work is a small, focused step. JSON output is parsed leniently and every parse has a fallback. Text-based tool calls are accepted when native ones fail. Unknown tool names map through an alias table. Repeated identical calls are detected. Argument types are coerced. Errors tell the model how to fix the call. `edit_file` matching tolerates small mistakes. |
| Must be model-agnostic. | Models are listed in config. Roles (`main`, `fast`, `research`) choose which model does which job. `/model` switches at runtime, and `tool_mode` is set per model. |
| Internal search engine with poor ranking. | Queries are expanded into many short variants, with acronym/glossary pairs. Results from all queries are pooled, re-ranked locally with BM25, triaged by the LLM, and followed one link deep. Several research rounds look for gaps. |
| Files spread across network drives. | `locations` registry, `find_files` across all of them, time budgets on slow walks, and a full-text index of shares (`hsa index`). |
| Confidential values (hosts, keys). | Every such value is a `<PLACEHOLDER>` or an `${ENV:...}` reference. Nothing is hard-coded. |

## Architecture

```
            ┌──────────── cli.py (REPL / -p / subcommands) ────────────┐
 user ⇄ ui.py                                                           │
            └──► agent.py  ── system prompt = core + env + AGENT.md memory + todos
                    │  loop: LLM → tool calls → permissions → hooks → tool → result
                    │  auto-compaction (trim old tool output → summarize)
                    ▼
            llm/ (OpenAI-compatible | custom | mock)  ← native or prompted tool calls
                    │
   tools/ ──────────┼─ files (read/write/edit/list/glob/grep/find_files)
                    ├─ shell (persistent cwd, background jobs), run_python
                    ├─ research_tools → research/ (sources, fetch+cache, BM25, docindex, engine)
                    ├─ gitlab (REST v4)       ├─ memory / todo_write / ask_user
                    └─ task (sub-agent with a fresh context and a restricted toolset)
```

## Claude Code feature parity

| Claude Code | HSA | Status |
|---|---|---|
| Read / Write / Edit | `read_file` / `write_file` / `edit_file` | ✅ (reads Office docs and PDFs too) |
| Glob / Grep / LS | `glob` / `grep` (uses ripgrep if installed) / `list_dir` | ✅ |
| Bash, background shells | `shell` (bash, PowerShell, or cmd), `shell_output` | ✅ |
| WebSearch / WebFetch | `intranet_search` / `fetch_url` | ✅ (internal sources) |
| Research subagents | `deep_research` engine | ✅ (goes beyond Claude Code) |
| Task (subagents) | `task` (explore / general) | ✅ |
| TodoWrite | `todo_write` | ✅ |
| CLAUDE.md hierarchy, `#` memory, `@imports` | `AGENT.md` (also reads `CLAUDE.md`), `#` / `#g`, `@file.md` | ✅ |
| Self-updating memory | `memory` tool + `/reflect` | ✅ |
| Permission modes, allow/deny rules | default / accept_edits / auto / plan | ✅ |
| Hooks | `pre_tool` / `post_tool` | ✅ (basic) |
| Output styles | `/style` (named styles, language, verbosity, format) | ✅ |
| Browser automation | `webaccess` (headless and logged-in browser via DevTools protocol) | ✅ |
| Slash commands, custom commands | built-ins + `.hsa/commands/*.md` | ✅ |
| /compact, auto-compact, /context, /cost | ✅ | ✅ |
| /init, /resume, --continue, -p | ✅ | ✅ |
| Git workflow | `shell` git + `gitlab` tool (MRs, issues, pipelines, job logs) | ✅ |
| Local doc index | `doc_search` + `hsa index` | ✅ (new) |
| MCP servers | — | Roadmap: a stdio JSON-RPC client is feasible with the stdlib |
| NotebookEdit | reads `.ipynb` only | Roadmap |
| Skills (packaged instructions + scripts) | custom commands cover most uses | Roadmap |
| Images / vision, IDE integration | — | Depends on the model and API |

## The agent loop (how a turn ends)

`turn_policy.py` routes each message to chat, task, or auto. When the model stops calling tools, a set of
checks decides whether the task is actually done. If not, HSA sends a capped "auto-continue" nudge. The
cases covered: announced-but-not-done, unnecessary "shall I proceed?", open todos, edited-but-untested code,
empty or truncated replies, error streaks, repeated calls, and the step limit. The alternative ending
mode, `completion: finish_tool`, makes the model call `finish()` explicitly. `hsa evalloop` measures both
modes on 7 scenarios with your real model, so tuning is based on data.

## Roadmap (suggested order once it runs on the high side)

1. **Calibrate on real systems.** Run `hsa evalloop` and `hsa webtest` first. Run `doctor`, fill in `context_tokens`, pick `tool_mode`, and write the
   `html` search source regex for the real engine. Load the glossary with your org's acronyms.
2. **Measure research quality.** Collect 10 real research questions with known answers. Tune
   `queries_per_round`, `pages_to_read_per_round`, and `max_rounds`, and try `fast` on a different model.
3. **Index the key shares** on a schedule (Windows Task Scheduler: `python -m hsa index team_share`).
4. **MCP client** (stdio) if any internal tools expose MCP.
5. **Multi-edit and notebook editing.** An `apply_patch`-style tool that some models prefer.
6. **Prompt tuning per model.** Optional `system_prompt_extra` per model profile, for model-specific habits.
7. **Parallel LLM calls in research.** Read several pages at once if the inference server can take
   concurrent requests.

## Open questions for you (answers change the config, not the code)

1. **API shape.** Is the work API OpenAI-compatible (`POST .../v1/chat/completions` with
   `messages`/`tools`)? If not, a redacted example of one request and one response is enough to fill in
   `custom_template.py`.
2. **Context windows** of Inkling Small, GPT-OSS, and Gemini as deployed. Local servers are often configured
   smaller than the model's maximum.
3. **OS and shell.** Windows with PowerShell? Is Git for Windows installed, so `git` is on PATH? Which Python
   version does Anaconda have?
4. **Search engine.** What product is it, if known? Does it have a JSON API, or only an HTML results page?
   `python -m hsa webtest <search-url-with-{query}>` answers how to reach it: it tries Python, curl
   (Windows login), PowerShell, a headless browser, and your logged-in browser (`hsa browser`). Send me the
   (redacted) output if extraction looks wrong.
5. **Network drives.** UNC paths (`\\server\share`), mapped letters, or both? How large are they? (This
   decides whether to index them.)
6. **GitLab.** Can you create a personal access token (`api` scope)? Is it self-signed TLS (then you need
   `ca_bundle`)?
7. **Internal conda mirror.** If one exists, `pypdf` and `prompt_toolkit` are worthwhile optional additions.
