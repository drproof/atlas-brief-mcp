"""MCP server (stdio) exposing the project documentation index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import search as search_mod
from .config import find_root
from .indexer import ensure_fresh, open_index

INSTRUCTIONS = (
    "This server indexes the project's design and architecture documentation. "
    "READING: For ANY documentation question use atlas_where or atlas_search first — "
    "never open the Markdown sources or .atlas/cache with Read/Grep. Those files are "
    "large; the index is faster and returns only the relevant section. "
    "Flow: atlas_where('topic') -> atlas_read(anchor) for the full text. "
    "PLANNING: use atlas_plan to create a new open Work Order with `- [ ]` steps "
    "(it automatically links the matching spec anchor via atlas_where). "
    "WRITING: call atlas_set_status and atlas_publish only after tests are green — "
    "marking something done is the last step, never the first. Design prose, formulas "
    "and balancing numbers are read-only; autonomous writes are limited to status "
    "markers, implementation columns and file paths. "
    "NEVER write balancing values as a cell coordinate (C14): atlas_sheet_update takes "
    "a row_key plus column name and computes the cell itself. "
    # --- Directness: prevents 15-call chains for one question ---
    "DIRECTNESS: one matching tool answers the question in ONE call. "
    "Work order status = atlas_status. Open tasks = atlas_tasks. Symbol/file = atlas_code_find. "
    "Do NOT additionally run `list tools`, the CLI, the MCP sources or the index to "
    "confirm a result: the tools read the same source and do not lie. "
    "A second call is justified only if the first returned an error message. "
    "Status and diagnostics are separate: atlas_status returns the work orders, "
    "`atlas doctor` (CLI only, not an MCP tool) checks index health."
)

mcp = FastMCP("atlas", instructions=INSTRUCTIONS)

_READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

_root: Path | None = None
_conn = None


def configure(root: str | Path | None = None) -> None:
    """Set the project root (the CLI uses this for --root)."""
    global _root, _conn
    _root = find_root(str(root) if root else None)
    _conn = None


def _state():
    global _root, _conn
    if _conn is None:
        if _root is None:
            _root = find_root()
        _conn = open_index(_root)
    return _root, _conn


def _health(fn):
    """Warning header when an invariant is violated (token scopes, unconfirmed write).

    When everything is fine the header is empty and the response is unchanged —
    the safety net then costs zero tokens.

    JSON responses stay JSON: the warning is attached as a `_health` field, not
    prepended. A prefix before `{…}` would break any agent that parses the answer.
    """
    import functools
    import json

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        text = fn(*args, **kwargs)
        from . import health as health_mod

        try:
            root, _conn = _state()
            note = health_mod.header(root)
        except Exception:
            note = ""
        if not note:
            return text

        if text.lstrip().startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                return f"{note}\n{text}"
            data["_health"] = note
            return json.dumps(data, ensure_ascii=False, indent=2)
        return f"{note}\n{text}"

    return wrapper


def _fresh():
    root, conn = _state()
    from . import gdocs, normalize
    from .config import cache_dir

    normalize.load_aliases(cache_dir(root) / "aliases.yml")
    gdocs.auto_sync(root, conn)
    ensure_fresh(conn, root)
    return root, conn


@mcp.tool(
    annotations=ToolAnnotations(
        title="Locate Documentation Section",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_where(
    topic: Annotated[
        str,
        Field(
            description=(
                "Natural-language question or topic keywords to locate in the project "
                "documentation (e.g. 'damage model', 'Where is dialogue branching described?'). "
                "Supports English and German terms with automatic stop-word removal and umlaut folding."
            ),
            min_length=1,
        ),
    ],
) -> str:
    """Locate the best-matching documentation section for a topic and return its pointer (~60 tokens) without full text.

    Use this tool FIRST for 'where is X documented?', 'which file covers Y?', or 'which section defines Z?'.
    Follow up with `atlas_read(anchor)` only if you need the section body, or fall back to `atlas_search`
    if you need multiple ranked snippets across documents.

    Behavior & Output:
    - Read-only, deterministic BM25 lookup against the local SQLite FTS5 index (auto-refreshed before query).
    - Returns a JSON string with `{found: true, anchor, heading, path, line, alternatives}` when matched,
      or `{found: false, hint}` when no section matches.
    """
    _, conn = _fresh()
    return search_mod.where(conn, topic)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Full-Text Documentation Search",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_search(
    query: Annotated[
        str,
        Field(
            description=(
                "Keywords or phrase to search across all indexed documentation sections "
                "(e.g. 'cover reduction', 'turn order'). Uses lexical FTS5 matching with "
                "umlaut folding ('Hoehe' matches 'Höhe') and German compound splitting."
            ),
            min_length=1,
        ),
    ],
    k: Annotated[
        int,
        Field(
            default=5,
            ge=1,
            le=50,
            description="Maximum number of ranked section hits to return (1-50, default 5).",
        ),
    ] = 5,
    doc: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional document ID (e.g. 'combat' or '02-combat') to restrict the search "
                "to a single document. Omit or pass null to search all documents."
            ),
        ),
    ] = None,
) -> str:
    """Run a BM25 full-text search across documentation sections and return ranked snippets with anchors.

    Use this tool for broad or multi-section topics ('where all is armor mentioned?') or when
    `atlas_where` returns no single match. Pass a returned `anchor` to `atlas_read` to inspect the full section.

    Behavior & Output:
    - Read-only lexical search via SQLite FTS5 (no embeddings; project synonyms come from `.atlas/aliases.yml`).
    - Returns a JSON string `{query, count, hits: [{anchor, title, path, line, snippet, score}]}`.
      Returns `count: 0` and an empty `hits` list if nothing matches.
    """
    _, conn = _fresh()
    return search_mod.search(conn, query, k=k, doc=doc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read Documentation Section by Anchor",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_read(
    anchor: Annotated[
        str,
        Field(
            description=(
                "Section anchor identifier in `<doc_id>#<section-slug>` format (or `<doc_id>` for "
                "the document root), as returned by `atlas_where`, `atlas_search`, or `atlas_outline` "
                "(e.g. 'combat#damage-model')."
            ),
            min_length=1,
        ),
    ],
    offset: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description=(
                "Character offset to start reading from (default 0). When a previous call returned "
                "`truncated: true`, pass its `next_offset` value here to read the next page."
            ),
        ),
    ] = 0,
    max_chars: Annotated[
        int,
        Field(
            default=3000,
            ge=100,
            le=50000,
            description="Maximum number of characters to return in one call (default 3000).",
        ),
    ] = 3000,
) -> str:
    """Read the Markdown content of a single documentation section by its anchor with character-budget pagination.

    Use this tool after obtaining an `anchor` from `atlas_where`, `atlas_search`, or `atlas_outline`.
    Never read raw documentation files from disk when `atlas_read` can fetch the exact section.

    Behavior & Output:
    - Read-only lookup from the local documentation index.
    - Returns a JSON string `{anchor, heading, path, lines, content, truncated, next_offset}`.
      If the anchor does not exist, returns `{error, suggestions}` with the closest valid anchors.
    """
    _, conn = _fresh()
    return search_mod.read(conn, anchor, offset=offset, max_chars=max_chars)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Documentation Table of Contents",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_outline(
    doc: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional document ID (e.g. '02-combat' or 'architecture') to list headings "
                "for a single document. Omit or pass null to list the table of contents for all documents."
            ),
        ),
    ] = None,
) -> str:
    """Return the hierarchical table of contents (headings and section anchors) across indexed documentation.

    Use this tool when exploring an unfamiliar project structure, browsing available documents,
    or when keyword searches (`atlas_where` / `atlas_search`) did not match your query terms.

    Behavior & Output:
    - Read-only, side-effect-free query of indexed document headings.
    - Returns a JSON string `{doc, outline, truncated}` where `outline` lists each heading level,
      title, and `anchor` ready for `atlas_read`.
    """
    _, conn = _fresh()
    return search_mod.outline(conn, doc=doc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Documentation Checkbox Tasks",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_tasks(
    status: Annotated[
        Literal["open", "done", "all"],
        Field(
            default="open",
            description=(
                "Filter tasks by completion state: 'open' for unchecked `- [ ]` items (default), "
                "'done' for checked `- [x]` items, or 'all' for both."
            ),
        ),
    ] = "open",
    owner: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional owner handle to filter tasks assigned via `@name` tags in Markdown "
                "(e.g. '@design' or 'design'). Omit or pass null for all owners."
            ),
        ),
    ] = None,
) -> str:
    """List design and development action items (`- [ ]` / `- [x]` checkboxes) extracted from all documentation sections.

    Use this tool to inspect granular checklist items embedded in specs. To inspect numbered
    high-level Work Orders (`Work Order N — ⬜ OPEN / ✅ DONE`), use `atlas_status` instead.

    Behavior & Output:
    - Read-only query over indexed Markdown checkboxes.
    - Returns a JSON string `{status, owner, count, tasks: [{text, done, owner, tags, anchor, path, line}]}`.
    """
    _, conn = _fresh()
    return search_mod.tasks(conn, status=status, owner=owner)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Inspect Section Links and Code Relations",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_links(
    anchor: Annotated[
        str,
        Field(
            description=(
                "Section anchor identifier in `<doc_id>#<section-slug>` or `<doc_id>` format "
                "(e.g. 'combat#damage-model'), as returned by `atlas_where`, `atlas_search`, or `atlas_outline`."
            ),
            min_length=1,
        ),
    ],
) -> str:
    """Inspect a documentation section's relationships: linked source files (`implements`), tags, cross-references, and sibling sections.

    Use this tool to answer 'which code files implement this spec section?' or 'what other sections
    reference or belong to the same document as this anchor?'.

    Behavior & Output:
    - Read-only lookup against the local section graph and frontmatter metadata (`implements`, `tags`).
    - Returns a JSON string `{anchor, heading, path, implements: [...], tags: [...], outgoing_links: [...], incoming_links: [...], siblings: [...]}`
      or `{error}` if the anchor is unknown.
    """
    _, conn = _fresh()
    return search_mod.links(conn, anchor)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Build Self-Contained Subagent Task Brief",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_brief(
    goal: Annotated[
        str,
        Field(
            default="",
            description=(
                "Optional one-sentence description of the task to prepare a brief for "
                "(e.g. 'wire the dwarf vault-charger perk into turn_manager'). "
                "When omitted or empty, automatically builds the brief for `work_order` or the next open Work Order."
            ),
        ),
    ] = "",
    max_sections: Annotated[
        int,
        Field(
            default=5,
            ge=1,
            le=20,
            description="Maximum number of relevant documentation sections to embed in the brief (1-20, default 5).",
        ),
    ] = 5,
    work_order: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Optional Work Order number (e.g. 7). When provided, loads that Work Order's linked `spec_anchor`, "
                "`- [ ]` steps, and target symbol status from `.atlas/wo-symbols.yml`."
            ),
        ),
    ] = None,
    files: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Optional list of relative source file paths to include alongside auto-detected "
                "`implements` files (e.g. ['scripts/unit.gd', 'src/combat.py'])."
            ),
        ),
    ] = None,
    all_open: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "If true (`atlas next+` mode), build a sequential execution queue covering ALL open "
                "Work Orders in order so the agent can work through the entire backlog step by step (default false)."
            ),
        ),
    ] = False,
) -> str:
    """Assemble a self-contained Markdown task brief for a coding agent or subagent (for one Work Order, or the full open queue via `all_open=True`).

    Use this tool before starting or delegating a feature task. Call `atlas_brief()` with no arguments to
    automatically brief the next open (or `🚧` in-progress) Work Order, or pass `all_open=True` (`atlas next+`)
    to generate a sequential execution queue for all open Work Orders.

    Behavior & Output:
    - Read-only aggregation of indexed documentation sections, Work Order steps, and code metadata.
    - Returns a formatted Markdown brief string ready to follow or paste into a subagent prompt.
    """
    root, conn = _fresh()
    from . import brief as brief_mod

    return brief_mod.build(conn, root, goal, max_sections=max_sections,
                           work_order=work_order, extra_files=files, all_open=all_open)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Publish Verified Work-Order Entry",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@_health
def atlas_publish(
    wo: Annotated[
        int,
        Field(
            ge=1,
            description="Work Order number (positive integer, e.g. 7 for 'Work Order 7').",
        ),
    ],
    title: Annotated[
        str,
        Field(
            min_length=1,
            description="Short title of the Work Order (e.g. 'Critical Hit Calculation').",
        ),
    ],
    description: Annotated[
        str,
        Field(
            default="",
            description="Optional summary of what was implemented and which files were touched.",
        ),
    ] = "",
    status: Annotated[
        Literal["done", "open"],
        Field(
            default="done",
            description="Target Work Order status: 'done' (writes `✅ DONE`, default) or 'open' (writes `⬜ OPEN`).",
        ),
    ] = "done",
    verify_symbols: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "List of function or class names built in this Work Order (e.g. ['apply_crit_bonus']). "
                "When `status='done'`, each symbol is checked before writing: if any symbol is undefined "
                "or has 0 callers/references across the codebase, the write is refused."
            ),
        ),
    ] = None,
) -> str:
    """Append a new Work Order status entry to the documentation — gated by a code-wiring check and verified by read-back.

    Use this tool as the FINAL step after tests pass to record a new Work Order in the bound Google Doc
    or local managed Markdown file (`docs/ARCHITECTURE.md`). If the Work Order heading already exists and
    you only need to flip `⬜ OPEN` to `✅ DONE`, use `atlas_set_status` instead.

    Behavior & Output:
    - Pre-write wiring gate: when `status='done'`, verifies that every symbol in `verify_symbols` (and
      `.atlas/wo-symbols.yml`) is both defined and referenced/called (`calls + references > 0`). If any
      symbol is dead code, returns `{written: false, refused: true, refusal: "..."}` without touching disk/network.
    - Atomic verified write: appends the formatted status line, reads the document back to confirm presence,
      logs to `.atlas/mutations.log`, and returns `{written: true, verified: true, wo, line}`.
    """
    from . import gdocs, publish as publish_mod

    root, conn = _fresh()
    try:
        result = publish_mod.publish(
            root, conn, wo, status=status, title=title, description=description,
            verify_symbols=verify_symbols,
        )
    except (gdocs.SyncError, ValueError) as error:
        return json.dumps({"written": False, "error": str(error)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Plan New Open Work Order with Steps",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_plan(
    topic: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Title or topic of the Work Order to create (e.g. 'Damage Over Time' or 'Dialogue conditions'). "
                "Automatically searched against the documentation index (`atlas_where`) to link the matching spec anchor and `implements:` files."
            ),
        ),
    ],
    steps: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Optional list of concrete implementation steps to record as `- [ ]` checkboxes under the Work Order "
                "(e.g. ['Apply burn stacks in effects.py', 'Call tick_effects at end of turn', 'Add unit test']). "
                "All steps must be checked off via `atlas_set_status(task=..., status='done')` before the Work Order can be marked done."
            ),
        ),
    ] = None,
    wo: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description="Optional explicit Work Order number. Omit or pass null to automatically assign the next free Work Order number.",
        ),
    ] = None,
    verify_symbols: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Optional list of target function or class names expected from this Work Order "
                "(pre-registered in `.atlas/wo-symbols.yml` so the wiring gate enforces them on completion)."
            ),
        ),
    ] = None,
) -> str:
    """Create a new `⬜ OPEN` Work Order in local documentation, automatically linking the matching spec section (`atlas_where`), `implements:` files, and `- [ ]` checklist steps.

    Use this tool when planning a new feature or breaking a user request into structured steps before coding.
    Unlike `atlas_publish` (which records a finished result), `atlas_plan` assigns the next free Work Order
    number, looks up the relevant spec anchor via BM25 search, and writes a checkable `- [ ]` step list.

    Behavior & Output:
    - Queries the documentation index for `topic`, attaches `Spec <anchor> (<path>:<line>)` and any `implements:` files.
    - Writes `Work Order <N> — ⬜ OPEN (<topic>): ...` plus `- [ ] <step>` lines into `docs/ARCHITECTURE.md`.
    - Pre-records `verify_symbols` in `.atlas/wo-symbols.yml` when provided.
    - Returns JSON `{written, verified, wo, title, spec_anchor, spec_origin, implements, steps, line}`.
    """
    from . import gdocs, publish as publish_mod

    root, conn = _fresh()
    try:
        result = publish_mod.plan(
            root, conn, topic, steps=steps, wo=wo, verify_symbols=verify_symbols,
        )
    except (gdocs.SyncError, ValueError) as error:
        return json.dumps({"written": False, "error": str(error)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Update Work-Order or Checkbox Task Status",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@_health
def atlas_set_status(
    wo: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Existing Work Order number to update (positive integer, e.g. 8 for 'Work Order 8'). "
                "Provide either `wo` or `task`."
            ),
        ),
    ] = None,
    status: Annotated[
        Literal["done", "open"],
        Field(
            default="done",
            description="New status: 'done' (`✅ DONE` / `- [x]`, default) or 'open' (`⬜ OPEN` / `- [ ]`).",
        ),
    ] = "done",
    verify_symbols: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Optional list of function or class names that must be defined and called/referenced "
                "in the codebase before `status='done'` is allowed. Merged with `.atlas/wo-symbols.yml`."
            ),
        ),
    ] = None,
    task: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            description=(
                "Substring identifying a Markdown checkbox item (`- [ ] ...`) to toggle between "
                "`- [ ]` ('open') and `- [x]` ('done') when `wo` is omitted (e.g. 'Balance damage formula')."
            ),
        ),
    ] = None,
) -> str:
    """Flip the status marker of an existing Work Order (`⬜ OPEN` <-> `✅ DONE`) or Markdown checkbox task (`- [ ]` <-> `- [x]`).

    Use this tool as the final step when completing an existing open Work Order (`wo=N`) or a checkbox
    task from `atlas_tasks` (`task='...'`). To append a brand-new Work Order entry that is not yet in the
    document, use `atlas_publish` instead.

    Behavior & Output:
    - Pre-write wiring gate: when `status='done'`, checks that all symbols in `verify_symbols` (and
      `.atlas/wo-symbols.yml`) are defined and have at least one caller/reference. Refuses with
      `{changed: false, refused: true, refusal: "..."}` if any symbol is unwired.
    - Verified in-place update: updates the status marker or checkbox in the local Markdown file (or active
      Google Doc tab for Work Orders), verifies by reading back, and returns JSON `{changed, verified, status}`.
    """
    from . import gdocs, publish as publish_mod

    root, conn = _fresh()
    try:
        result = publish_mod.set_status(root, conn, wo, status=status,
                                        verify_symbols=verify_symbols, task=task)
    except (gdocs.SyncError, ValueError) as error:
        return json.dumps({"changed": False, "error": str(error)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Update Spreadsheet Row by Key and Column Name",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@_health
def atlas_sheet_update(
    tab: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact worksheet/tab name inside the spreadsheet (e.g. 'Units' or 'Weapons'). Use `atlas_sheets` to list valid tabs.",
        ),
    ],
    row_key: Annotated[
        str,
        Field(
            min_length=1,
            description="Primary key value identifying the target row in the key column (e.g. 'monster_goblin').",
        ),
    ],
    updates: Annotated[
        dict[str, str | int | float | bool | None],
        Field(
            description=(
                "Mapping of `{column_header_name: new_value}` for the cells to update in that row "
                "(e.g. `{\"HP\": 45, \"Status\": \"✅\"}`). Never pass raw cell coordinates like 'C14'."
            ),
        ),
    ],
    sheet: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional spreadsheet name or ID when multiple spreadsheets are registered. Omit if only one spreadsheet exists.",
        ),
    ] = None,
    key_column: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional header name of the key column used to match `row_key`. Defaults to the first column of the tab.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        Field(
            default=False,
            description="If true, compute and return the cell diff without writing changes to the spreadsheet (default false).",
        ),
    ] = False,
) -> str:
    """Update specific cells of an existing spreadsheet row by `row_key` and column header name (never by cell coordinate).

    Use this tool to modify balancing values, implementation markers, or metadata in an existing row.
    To add a brand-new row to a tab, use `atlas_sheet_append`. If unsure of tab names, call `atlas_sheets` first.

    Behavior & Output:
    - Resolves the exact cell range from `row_key` + header names, preventing off-by-one row overwrites.
    - If a column name is unknown, aborts without writing and returns the list of valid column headers.
    - Returns JSON `{ok, sheet, tab, row, key, changes, verified, resynced, dry_run}` and refreshes the local search index.
    """
    from . import gdocs, sheets as sheets_mod

    root, conn = _fresh()
    try:
        result = sheets_mod.update(
            root, conn, tab, row_key, updates,
            sheet=sheet, key_column=key_column, dry_run=dry_run,
        )
    except (gdocs.SyncError, sheets_mod.SheetError) as error:
        return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)

    return json.dumps({
        "ok": True,
        "sheet": result["spreadsheet"],
        "tab": result["tab"],
        "row": result["row_number"],
        "key": row_key,
        "changes": result["changes"],
        "verified": result.get("verified"),
        "resynced": result.get("resynced"),
        "dry_run": dry_run,
        **({"warning": result["warning"]} if result.get("warning") else {}),
    }, ensure_ascii=False)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Append Row to Spreadsheet by Column Name",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
@_health
def atlas_sheet_append(
    tab: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact worksheet/tab name inside the spreadsheet (e.g. 'Units'). Use `atlas_sheets` to discover valid tab names.",
        ),
    ],
    values: Annotated[
        dict[str, str | int | float | bool | None],
        Field(
            description=(
                "Mapping of `{column_header_name: value}` for the new row "
                "(e.g. `{\"ID\": \"monster_goblin\", \"Name\": \"Goblin\", \"HP\": 100}`). "
                "Omitted columns are left empty."
            ),
        ),
    ],
    sheet: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional spreadsheet name or ID when multiple spreadsheets are bound to the project.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        Field(
            default=False,
            description="If true, validate column headers and preview the row without writing to the spreadsheet (default false).",
        ),
    ] = False,
) -> str:
    """Append a new row to a spreadsheet tab by mapping column header names to values.

    Use this tool when adding a new entry (e.g. a new unit, item, or parameter row) to a bound spreadsheet.
    To modify cells of an existing row, use `atlas_sheet_update` instead.

    Behavior & Output:
    - Not idempotent: each non-dry-run call appends a new row at the bottom of the table and verifies it by read-back.
    - Validates all keys in `values` against the tab's header row before writing.
    - Returns JSON `{ok, sheet, tab, values, range, verified, resynced, dry_run}` and updates the local search index.
    """
    from . import gdocs, sheets as sheets_mod

    root, conn = _fresh()
    try:
        result = sheets_mod.append(root, conn, tab, values, sheet=sheet, dry_run=dry_run)
    except (gdocs.SyncError, sheets_mod.SheetError) as error:
        return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)

    return json.dumps({
        "ok": True,
        "sheet": result["spreadsheet"],
        "tab": result["tab"],
        "values": result["values"],
        "range": result.get("appended_range", ""),
        "verified": result.get("verified"),
        "resynced": result.get("resynced"),
        "dry_run": dry_run,
    }, ensure_ascii=False)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Bound Spreadsheets and Worksheet Tabs",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@_health
def atlas_sheets() -> str:
    """List all spreadsheets bound to the project along with their worksheet tab names.

    Takes no parameters. Call this tool before `atlas_sheet_update` or `atlas_sheet_append`
    whenever you need to discover the exact `sheet` name/ID or valid `tab` names.

    Behavior & Output:
    - Read-only metadata lookup; returns spreadsheet and tab names without dumping cell contents.
    - Returns JSON `{spreadsheets: [{name, id, tabs: [...]}]}` or `{spreadsheets: [], hint: "..."}`
      when no spreadsheet has been synced yet.
    """
    from . import gdocs, sheets as sheets_mod

    _, conn = _fresh()
    try:
        sources = sheets_mod.known_spreadsheets(conn)
        if not sources:
            return json.dumps({
                "spreadsheets": [],
                "hint": "No spreadsheet source known. Run `atlas sync --query <Name>` first.",
            }, ensure_ascii=False)
        service = sheets_mod._service()
        payload = []
        for source in sources:
            item = {"name": source["name"], "id": source["id"]}
            try:
                item["tabs"] = sheets_mod.list_tabs(service, source["id"])
            except Exception as error:
                item["error"] = gdocs._explain(error)
            payload.append(item)
    except gdocs.SyncError as error:
        return json.dumps({"spreadsheets": [], "error": str(error)}, ensure_ascii=False)
    return json.dumps({"spreadsheets": payload}, ensure_ascii=False)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Find Code Symbol Definitions and Call Sites",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_code_find(
    symbol: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Function, method, or class name (or substring) to search for across the codebase "
                "(e.g. 'apply_damage' or 'TurnManager'). Does not match local variables."
            ),
        ),
    ],
    limit: Annotated[
        int,
        Field(
            default=10,
            ge=1,
            le=100,
            description="Maximum number of matching symbol definitions to return (1-100, default 10).",
        ),
    ] = 10,
    usage: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "If true, also scan the repository for call sites and references to each matched symbol "
                "(including Godot `.tscn` signal bindings) to verify whether the symbol is wired up (default false)."
            ),
        ),
    ] = False,
    include_body: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "If true, also extract and return the function or class body (lines start..end, capped at 80 lines) "
                "so you can inspect the implementation without opening the full source file with Read (default false)."
            ),
        ),
    ] = False,
) -> str:
    """Locate function or class definitions across the codebase (file, line, signature, and optional body) and inspect call sites.

    Use this tool BEFORE reading source files to find where a symbol lives (~30 tokens per signature vs.
    thousands of tokens for a full file), set `include_body=True` to read just that function's implementation,
    or set `usage=True` to check if a symbol is actually called before marking a task done. Supports GDScript
    (`.gd`/`.tscn`), Python, TypeScript/JavaScript, Rust, C#, Go, Java, Kotlin, and Swift.

    Behavior & Output:
    - Read-only static scan of project source files; ignores build directories and `.atlas/`.
    - Returns a formatted summary of matching definitions (`path:line — signature`), optional function body,
      and, when `usage=True`, the caller/reference count and file:line call sites for each symbol.
    """
    root, _ = _fresh()
    from . import code as code_mod

    return code_mod.render(root, symbol, limit=limit, with_usage=usage, include_body=include_body)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Lint Documentation Structure and Links",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_lint() -> str:
    """Audit indexed documentation for structural and formatting problems that degrade retrieval quality.

    Takes no parameters. Checks managed documentation for duplicate section anchors, empty headings,
    oversized sections (>8,000 chars), Work Orders missing a status marker (`⬜`/`🚧`/`✅`), and broken
    `implements:` code file paths. Use this tool after editing documentation or running `atlas init`.

    Behavior & Output:
    - Read-only structural check; respects `.atlas/managed.json` (separating managed docs from legacy discovered docs).
    - Returns a JSON string `{managed_issues: [...], discovered_hints: [...], clean: bool}` detailing each issue by file and line.
    """
    from . import bootstrap, lint as lint_mod

    root, conn = _fresh()
    return lint_mod.lint(conn, managed=bootstrap.load_managed(root))


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Work-Order Progress Status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@_health
def atlas_status() -> str:
    """Return the complete list of documented Work Orders (`⬜ OPEN`, `🚧 IN PROGRESS`, `✅ DONE`) and index freshness.

    Takes no parameters. Use this tool first when asked about project progress, completed milestones,
    or which Work Order is next — one call returns the authoritative list without reading documentation files.
    For granular Markdown checkboxes (`- [ ]`), use `atlas_tasks` instead.

    Behavior & Output:
    - Read-only query over all Work Order headings/entries in the index (up to 60 orders).
    - Returns a human- and agent-readable summary starting with the index freshness timestamp,
      total counts (`done` / `open`), and one line per Work Order (`WO # — status — title (anchor)`).
    """
    import json as _json

    from . import status as status_mod

    _, conn = _fresh()
    payload = _json.loads(status_mod.status(conn, limit=60, max_chars=60000))
    return status_mod.status_text(payload)


@mcp.resource("atlas://outline")
def resource_outline() -> str:
    """Full document structure as text."""
    _, conn = _fresh()
    return search_mod.outline(conn, max_chars=8000)


@mcp.resource("atlas://section/{anchor}")
def resource_section(anchor: str) -> str:
    """A single section as text."""
    _, conn = _fresh()
    return search_mod.read(conn, anchor, max_chars=8000)


@mcp.prompt(
    name="atlas_onboard",
    title="Onboard New Coding Session",
    description=(
        "Load the project documentation outline, Work Order progress, and open checkbox "
        "tasks in one step so a fresh agent session has complete context without reading raw files."
    ),
)
def prompt_onboard() -> str:
    """Build a ready-to-use onboarding context snapshot for a new chat session."""
    from . import status as status_mod

    _, conn = _fresh()
    outline_data = json.loads(search_mod.outline(conn, max_chars=4000))
    status_data = json.loads(status_mod.status(conn, limit=60, max_chars=60000))
    tasks_data = json.loads(search_mod.tasks(conn, status="open"))
    return (
        "# Project Context Snapshot (atlas)\n\n"
        f"{INSTRUCTIONS}\n\n"
        "## 1. Work Orders (`atlas_status`)\n"
        f"{status_mod.status_text(status_data)}\n\n"
        "## 2. Open Checkbox Tasks (`atlas_tasks`)\n"
        f"Open tasks: {tasks_data.get('count', 0)}\n"
        + "\n".join(
            f"- [ ] {t['text']} ({t['anchor']})"
            for t in (tasks_data.get("tasks") or [])[:20]
        )
        + "\n\n## 3. Documentation Outline (`atlas_outline`)\n"
        f"{outline_data.get('outline', '')}\n"
    )


@mcp.prompt(
    name="atlas_task_brief",
    title="Prepare Subagent Task Brief",
    description=(
        "Assemble a self-contained task brief with relevant documentation sections, "
        "linked source files, guardrails, and the verified Work Order closing step."
    ),
)
def prompt_task_brief(
    goal: Annotated[
        str,
        Field(description="One-sentence description of the task to implement."),
    ],
    work_order: Annotated[
        int | None,
        Field(default=None, description="Optional Work Order number for the closing checkoff call."),
    ] = None,
) -> str:
    """Generate a self-contained subagent task brief."""
    from . import brief as brief_mod

    root, conn = _fresh()
    return brief_mod.build(conn, root, goal, work_order=work_order)


@mcp.prompt(
    name="atlas_pre_commit_audit",
    title="Pre-Commit Wiring and Documentation Audit",
    description=(
        "Verify that all symbols recorded for completed Work Orders remain defined and called, "
        "and audit documentation structure before committing or closing a task."
    ),
)
def prompt_pre_commit_audit() -> str:
    """Run wiring regression check and documentation lint before committing."""
    from . import bootstrap, lint as lint_mod, verify

    root, conn = _fresh()
    check_report = verify.check_project(root, conn)
    lint_report = json.loads(lint_mod.lint(conn, managed=bootstrap.load_managed(root)))
    return (
        "# Pre-Commit Audit (atlas)\n\n"
        "## 1. Wiring & Link Check (`atlas check`)\n"
        f"{verify.render_check(check_report)}\n\n"
        "## 2. Documentation Structure (`atlas lint`)\n"
        f"Managed issues: {lint_report.get('managed_issues', 0)} · "
        f"Discovered hints: {lint_report.get('discovered_issues', 0)} · "
        f"Clean: {lint_report.get('ok', False)}\n"
    )


def run() -> None:
    try:
        root, conn = _state()
        from . import gdocs

        gdocs.auto_sync(root, conn, quiet=True)
    except Exception:
        pass
    mcp.run()
