# atlas

Project documentation as an index, not as context fuel.

A local MCP server for coding agents. Ask "where is the combat system
described?" and get back **file, line and anchor** — not 40,000 tokens of
document you have to read, pay for, and mostly ignore.

Works with any folder of Markdown. Optionally indexes Google Docs & Sheets
when that is where your documentation lives.

```
Without atlas:  read the whole doc        20,364 tokens
With atlas:     atlas_where → atlas_read    875 tokens   (−96%)
```

---

## The problem it actually solves

An agent implemented 1,070 lines, forgot the single dispatcher call, and
reported "done". The next cleanup run saw an unused-function warning and
deleted all of it. The work was gone — because nobody noticed that a
function without a caller is not a feature.

`atlas` refuses that acceptance:

```json
{
  "written": false,
  "refused": true,
  "refusal": "Acceptance refused — wiring check failed:\n  ✗ '_dispatch_unit_class_skill' is defined in scripts/turn_manager.gd:3774 but never called or bound (0 calls, 0 references)\n\nA ✅ without a caller documents an intention, not a result."
}
```

That check runs **before** any network or filesystem write, so it costs nothing
when it blocks. It has caught this exact class of mistake twice in real use.

---

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/) (or pipx).

```bash
# recommended
uv tool install git+https://github.com/drproof/atlas-brief-mcp

# or with pipx
pipx install git+https://github.com/drproof/atlas-brief-mcp

# or from a clone, for development
git clone https://github.com/drproof/atlas-brief-mcp
cd atlas-brief-mcp
uv sync
uv run atlas --help
```

If `atlas` is not found afterwards, `uv tool` put it in `~/.local/bin`
(Windows: `%USERPROFILE%\.local\bin`) — add that to your `PATH`, or run
`uv tool update-shell` once.

Verify:

```bash
atlas --help          # 22 subcommands
```

No Google account, no API key, no network access is needed for any of this.

---

## Quickstart (offline, 30 seconds)

```bash
git clone https://github.com/drproof/atlas-brief-mcp
cd atlas-brief-mcp

atlas --root examples/demo doctor
atlas --root examples/demo where "Where is the damage model?"
atlas --root examples/demo brief "damage calculation"
```

Any folder of `.md` files works — `docs/`, `specs/`, `atlas/`, `gdd/`. The index
lands in `<root>/.atlas/index.sqlite`, is regenerable at any time, and your
Markdown stays the source of truth.

---

## Bootstrap an existing repository

```bash
cd your-project
atlas init
```

Scans the repository — no LLM, no network, **under 0.3 seconds for 50 files** —
and writes `docs/ARCHITECTURE.md` containing:

- **Domains**: every source folder with files, line counts, token estimates and
  the largest functions per domain (not the first ones — those are getters)
- **Context hotspots**: files over 10,000 tokens that must never be read whole
- **Unwired symbols**: functions over 15 lines whose name appears nowhere else —
  candidates for "implemented but never connected"

It also writes `AGENTS.md`, `.antigravityrules`, `.mcp.json` and adds `.atlas/`
to `.gitignore` — **but only if they don't exist.** `atlas init` never
overwrites your rules. A bootstrap that flattens hand-written conventions gets
used exactly once.

```bash
atlas init --dry-run            # show what would be created, write nothing
atlas init --out docs/ARCH.md   # different path
atlas init --force              # regenerate deliberately
```

`init` records what it created in `.atlas/managed.json`. `atlas lint` uses that
to separate **managed** documents (must be clean, exit code 1) from **discovered**
legacy docs (reported as hints, exit code 0). Without `managed.json` — in every
project before the first `init` — everything counts as managed, exactly as
before.

### What it understands

GDScript, Python, TypeScript/JavaScript (including `React.memo(...)` and
`forwardRef<...>(...)` wrappers), Rust, C#, Go, Java, Kotlin, Swift. Test
functions (`test_*`, `Test*`, `*_test`) and framework-bound functions
(`@decorator`, `@mcp.tool()`) are not reported as unwired — a warning list that
is 75% noise gets ignored.

---

## Writing without Google

`atlas init` creates `docs/ARCHITECTURE.md` with `Work Order 1 — ⬜ OPEN`.
If no Google document is bound to the project, `atlas` writes straight into that
file — same contract, same verification (read back from disk), same audit log:

```bash
atlas set-status --wo 1 --status done    # ⬜ OPEN -> ✅ DONE in the Markdown file
atlas status                            # reads the new state
```

So the whole loop — `init` → `where`/`read` → `code` → `set-status` with the
wiring gate → `doctor` — works **without a cloud account and without a network.**

---

## Connect your agent

The server speaks MCP over stdio. Once `atlas` is on your `PATH`, the config is
three lines everywhere.

**Claude Code / Cursor / any `.mcp.json` client** — in the project root:

```json
{
  "mcpServers": {
    "atlas": {
      "command": "atlas",
      "args": ["serve"],
      "env": { "ATLAS_ROOT": "." }
    }
  }
}
```

**Claude Desktop** — same block in `claude_desktop_config.json`. On Windows use
`"command": "atlas.exe"` if the bare name is not resolved.

**Antigravity** — same structure in `~/.gemini/config/mcp_config.json`.

`ATLAS_ROOT=.` resolves relative to the client's working directory, so one
installation serves every project. The server ships its own `instructions`: the
client reads them on connect and knows that doc questions go through
`atlas_where`, not `Read`/`Grep`, and that one tool call is enough — no
verification round-trip through the CLI.

---

## The 15 tools

| Tool | Purpose |
|---|---|
| `atlas_where` / `atlas_search` | topic → anchor (BM25, German + English stop words) |
| `atlas_read` | anchor → full section, budget-controlled |
| `atlas_outline` | document outline |
| `atlas_tasks` | open checkbox tasks |
| `atlas_links` | cross-references of a section |
| `atlas_brief` | task brief: sections + files + project rules |
| `atlas_code_find` | symbol → file, line, signature **and wiring** (calls/refs) |
| `atlas_status` | work orders, with a freshness timestamp |
| `atlas_publish` / `atlas_set_status` | write / close a status line, verified |
| `atlas_sheets` / `atlas_sheet_update` / `atlas_sheet_append` | read/write spreadsheets |
| `atlas_lint` | structure and format check |

---

## Google Docs & Sheets (optional)

If your documentation lives in Google Docs, `atlas` indexes it through the Docs
API — including multiple document tabs.

```bash
atlas auth                     # one-time OAuth consent in the browser
atlas sync --query MYPROJECT   # pull documents into the cache
atlas status
atlas publish --wo 7 --title "Mission 1" --desc "Rooms 1-9"
```

Three details that make the difference:

- **Export to read, query directly to judge.** The search index is filled from
  the Drive export; verification after a write goes through `documents.get`,
  because the Docs API is consistent immediately after `batchUpdate` while the
  Drive export lags 0.5–2 seconds. Verifying through the export builds you a
  `verified: false` that isn't one.
- **Writes are append-only with a revision lock.** `requiredRevisionId` prevents
  a write from landing on a stale revision.
- **Status changes are tab-scoped.** Google Docs has tabs; `replaceAllText`
  without `tabsCriteria` also rewrites the same string in archive copies.

Balancing values go through `atlas_sheet_update` with a `row_key` and a
**column name**, never a cell coordinate. An agent that writes `C14` miscounts by
one row and overwrites its neighbour.

---

## How it works

- **Index**: SQLite with FTS5 (`external content`), BM25 ranking, prefix search.
  Section anchors instead of file granularity: ~60-token hits instead of 2,000.
- **German + English**: umlaut folding, stop words, compound splitting — but only
  when both parts are in the vocabulary. Better no hit than a wrong one.
- **No embeddings**: synonyms come from `.atlas/aliases.yml` in your project,
  not from a model. Auditable, offline, milliseconds.
- **Writes**: atomic, verified by reading back, logged to `.atlas/mutations.log`.

### Measure it on your own project

The numbers below come from one real project of ours. You don't have to trust
them — run the same measurement on yours:

```bash
cd your-project
atlas init          # or point at existing docs
atlas measure       # naive read vs. index, per question
```

```
Documents: 4 · naive baseline: 28,011 tokens per question

Question                                           naive   atlas   saved
------------------------------------------------------------------------
Where is 7.6 Built engine features described?     28,011     885     97%
Where is 01_Units_Stats described?                28,011     689     98%
...
------------------------------------------------------------------------
Total                                            224,088   5,945     97%
```

By default the questions are derived from your section headings — which means
each one contains part of its own answer, so the saving is optimistic. The
report says so. For an independent test, write your own questions in a file:

```bash
atlas measure --questions my-questions.txt    # one per line
```

Reference values from that real project (5-tab doc, 124 sections, 218 GDScript
files) — measured the same way:

| Task | Without atlas | With atlas |
|---|---|---|
| Locate 8 symbols | 291,447 tokens | 3,106 tokens |
| One doc question | 20,364 tokens | 875 tokens |
| Read one monolith file | 74,522 tokens | 388 tokens |

---

## What it is not

- **Not a document editor.** It writes status lines and spreadsheet cells, not
  design prose.
- **Not a crawler.** It indexes what is in your documentation folders.
- **Not a test replacement.** The wiring gate checks that a function is called —
  not that it does the right thing.
- **Not Windows-only**, but developed and tested there.

---

## Tests

```bash
uv run pytest -q      # 197 tests, no network access
uv run python scripts/smoke_mcp.py --root examples/demo   # JSON-RPC smoke: 15 tools
```

The tests run without Google access. Everything that touches the Docs API is
faked — including the cases where the fake swallows a write, because that is
exactly what happened in production.

## License

MIT — see [LICENSE](LICENSE).
