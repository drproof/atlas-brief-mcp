"""MCP server (stdio) exposing the project documentation index."""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import search as search_mod
from .config import find_root
from .indexer import ensure_fresh, open_index

INSTRUCTIONS = (
    "This server indexes the project's design and architecture documentation. "
    "READING: For ANY documentation question use atlas_where or atlas_search first — "
    "never open the Markdown sources or .atlas/cache with Read/Grep. Those files are "
    "large; the index is faster and returns only the relevant section. "
    "Flow: atlas_where('topic') -> atlas_read(anchor) for the full text. "
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

    # Projekt-Synonyme laden — über cache_dir, damit auch `.gdd/`-Projekte
    # ihre aliases.yml behalten.
    from .config import cache_dir

    normalize.load_aliases(cache_dir(root) / "aliases.yml")
    # Erst den Google-Stand nachziehen (mit 5-Minuten-Cache), dann indexieren.
    gdocs.auto_sync(root, conn)
    ensure_fresh(conn, root)
    return root, conn


@mcp.tool()
@_health
def atlas_where(topic: str) -> str:
    """Where is something in the docs? Returns file, line, heading and anchor — no full text.

    FIRST CHOICE for 'where do I find X?', 'which file holds Y?', 'which section
    describes Z?'. The answer is tiny (~60 tokens). Follow up with atlas_read(anchor)
    if you need the text.
    """
    _, conn = _fresh()
    return search_mod.where(conn, topic)


@mcp.tool()
@_health
def atlas_search(query: str, k: int = 5, doc: str | None = None) -> str:
    """Full-text search across all sections, with snippets and BM25 ranking.

    Lexical, via SQLite FTS5 — **no** embeddings, no synonyms. Use words that appear
    in the documentation. Umlauts are folded ("Hoehe" finds "Höhe") and German
    compounds are split partially ("Kampfsystem" finds "Kampf-System").

    For fuzzy topics ('how does cover work?') or when atlas_where finds nothing.
    k = number of hits (default 5). doc = optionally restrict to one document id.
    """
    _, conn = _fresh()
    return search_mod.search(conn, query, k=k, doc=doc)


@mcp.tool()
@_health
def atlas_read(anchor: str, offset: int = 0, max_chars: int = 3000) -> str:
    """Read exactly ONE section by its anchor (e.g. 'combat-system#damage-model').

    Use the anchor from atlas_where/atlas_search/atlas_outline. If truncated=true,
    continue with next_offset. This is the only way to read documentation content.
    """
    _, conn = _fresh()
    return search_mod.read(conn, anchor, offset=offset, max_chars=max_chars)


@mcp.tool()
@_health
def atlas_outline(doc: str | None = None) -> str:
    """Table of contents of all documents (or a single one via doc='id').

    Use this when you don't know the structure yet, or when a topic was not found
    anywhere.
    """
    _, conn = _fresh()
    return search_mod.outline(conn, doc=doc)


@mcp.tool()
@_health
def atlas_tasks(status: str = "open", owner: str | None = None) -> str:
    """Open design/development tasks from the documentation's checkbox list.

    status: 'open' | 'done' | 'all'. owner: optionally filter by '@name'.
    """
    _, conn = _fresh()
    return search_mod.tasks(conn, status=status, owner=owner)


@mcp.tool()
@_health
def atlas_links(anchor: str) -> str:
    """A section's relations: affected code files (implements), tags, sibling sections.

    For 'what is connected to this?' or 'which code implements this section?'.
    """
    _, conn = _fresh()
    return search_mod.links(conn, anchor)


@mcp.tool()
@_health
def atlas_brief(goal: str, max_sections: int = 5, work_order: int | None = None,
              files: list[str] | None = None) -> str:
    """Build a ready-to-use task brief for a subagent: relevant sections,
    affected code files, the project guardrails and the closing step.

    Use this when delegating a task instead of handing the subagent your
    documentation file. The brief is self-contained and contains stable anchors.

    Args:
        goal: The task in one sentence, e.g. 'wire the dwarf vault-charger perk'.
        max_sections: number of sections to include (default 5).
        work_order: optional work order number — the brief then contains the
            ready-made `atlas_publish` call for closing it.
        files: optional affected code files, e.g. ['scripts/unit.gd'].
    """
    root, conn = _fresh()
    from . import brief as brief_mod

    return brief_mod.build(conn, root, goal, max_sections=max_sections,
                           work_order=work_order, extra_files=files)


@mcp.tool()
@_health
def atlas_publish(wo: int, title: str, description: str = "", status: str = "done",
                verify_symbols: list[str] | None = None) -> str:
    """Append a work-order status line — append-only and verified.

    Use this as the LAST step after a task instead of writing into the document
    yourself. The tool appends the line to the work-order section, then reads the
    document back through the Docs API and only reports success (`verified: true`)
    if the line is really there. In local projects (no Google Doc bound) it writes
    into the managed Markdown file with the same verification.

    status: 'done' (✅ DONE) or 'open' (⬜ OPEN).

    verify_symbols: the symbols this work order built. With 'done', the tool checks
    BEFORE writing whether they are defined AND used. A symbol that is defined but
    never called makes the write fail (`written: false, refused: true`) — exactly
    the failure mode that killed WO 8. Also record them in `.atlas/wo-symbols.yml`
    so later regressions are caught by `atlas doctor`.
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


@mcp.tool()
@_health
def atlas_set_status(wo: int, status: str = "done",
                   verify_symbols: list[str] | None = None) -> str:
    """Change the status of an ALREADY documented work order (✅ <-> ⬜).

    For open orders that just finished: `atlas_set_status(8, "done")`.
    Uses replaceAllText on the unique status string — no index arithmetic.
    To add a NEW order use `atlas_publish`, not this tool.

    With 'done' the same wiring check applies as in `atlas_publish`: symbols from
    verify_symbols and from `.atlas/wo-symbols.yml` must be defined and used, or the
    status is not set.
    """
    from . import gdocs, publish as publish_mod

    root, conn = _fresh()
    try:
        result = publish_mod.set_status(root, conn, wo, status=status,
                                        verify_symbols=verify_symbols)
    except (gdocs.SyncError, ValueError) as error:
        return json.dumps({"changed": False, "error": str(error)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
@_health
def atlas_sheet_update(
    tab: str,
    row_key: str,
    updates: dict,
    sheet: str | None = None,
    key_column: str | None = None,
    dry_run: bool = False,
) -> str:
    """Change individual cells of one row — via key plus column name.

    NEVER use cell coordinates like 'C14'. The row key is the value of the first
    column (e.g. 'monster_goblin'); columns are named as in the header
    (e.g. {"HP": 45, "Status": "✅"}).

    Args:
        tab: sheet name exactly as in the spreadsheet (e.g. 'Units').
        row_key: value of the key column that identifies the row.
        updates: {column name: new value} — only the columns to change.
        sheet: spreadsheet name or id, only needed when several sources exist.
        key_column: name of the key column if it is not the first one.
        dry_run: true = show the diff only, write nothing.

    A wrong column name returns the list of allowed headers — fix the call and
    retry. After success the search index is updated immediately.
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


@mcp.tool()
@_health
def atlas_sheet_append(tab: str, values: dict, sheet: str | None = None,
                     dry_run: bool = False) -> str:
    """Append a new row to a sheet — columns by header, not by position.

    Example: tab='Units', values={"ID": "monster_goblin", "Name": "Goblin",
    "HP": 100, "Armor": 2}. Missing columns stay empty.

    Args:
        tab: sheet name exactly as in the spreadsheet.
        values: {column name: value} for the new row.
        sheet: spreadsheet name or id, only needed with several sources.
        dry_run: true = show what would be appended.
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


@mcp.tool()
@_health
def atlas_sheets() -> str:
    """List the known spreadsheets and their tab names.

    Use this before calling atlas_sheet_update/atlas_sheet_append if you don't know
    the exact sheet name. Returns names only, no cell contents.
    """
    from . import gdocs, sheets as sheets_mod

    _, conn = _fresh()
    try:
        sources = sheets_mod.known_spreadsheets(conn)
        if not sources:
            return json.dumps({
                "spreadsheets": [],
                "hint": "Kein Spreadsheet als Quelle bekannt. `atlas sync --query <Name>` ausführen.",
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


@mcp.tool()
@_health
def atlas_code_find(symbol: str, limit: int = 10, usage: bool = False) -> str:
    """Find the definition of a function or class in code — file, line, signature.

    Use this BEFORE reading a source file: a 9,000-line manager module is
    ~125,000 tokens, one signature costs ~30. For 'where is X defined and what is
    its signature?' this is the cheap path.

    Supports GDScript, Python, TypeScript/JavaScript, Rust, C#, Go, Java, Kotlin
    and Swift — the same patterns `atlas init` uses.

    Args:
        symbol: function or class name (a substring is enough).
        limit: maximum hits (default 10).
        usage: true = also show the call sites. That tells you whether a function
            is used at all — an implementation without a caller is dead code, even
            if it looks complete.

    Variables and signals are NOT found this way — use `grep -n` for those.
    """
    root, _ = _fresh()
    from . import code as code_mod

    return code_mod.render(root, symbol, limit=limit, with_usage=usage)


@mcp.tool()
@_health
def atlas_lint() -> str:
    """Check documentation structure and return the formatting problems that make
    the index hard to read (empty headings, sections without content, overly long
    sections, work orders without a status marker, navigation lines).

    Use this before restructuring the docs — or after an agent run.
    """
    from . import bootstrap, lint as lint_mod

    root, conn = _fresh()
    # Ohne managed-Liste gilt alles als verwaltet — das ist der Altvertrag und
    # für Google-Docs-Projekte richtig. Mit Liste zählt nur atlas-eigene Doku.
    return lint_mod.lint(conn, managed=bootstrap.load_managed(root))


@mcp.tool()
@_health
def atlas_status() -> str:
    """Project progress: which work orders are done, which are open.

    Use this when you need the current state instead of reading the documentation.
    Returns number, status and title as a ready-made list, plus one line saying how
    old the state is. This answer is the source: no second call, no CLI, no look at
    the source code needed.
    """
    import json as _json

    from . import status as status_mod

    _, conn = _fresh()
    # Alle Work Orders, nicht die ersten 20: eine gekürzte Liste ist der Grund,
    # warum Agenten anfangen, gegen die CLI nachzuprüfen.
    payload = _json.loads(status_mod.status(conn, limit=60, max_chars=60000))
    return status_mod.status_text(payload)


# `atlas_index` und `atlas_doctor` gibt es bewusst nur in der CLI.
# Der Index aktualisiert sich bei jedem Tool-Call selbst (auto_sync + ensure_fresh)
# und nach jedem Write (Write-Through). Ein Agent muss ihn nie neu bauen, und jede
# Tool-Definition kostet permanent System-Kontext.


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


def run() -> None:
    # Einmal beim Start synchronisieren — dann ist der erste Tool-Call sofort da.
    try:
        root, conn = _state()
        from . import gdocs

        gdocs.auto_sync(root, conn, quiet=True)
    except Exception:
        pass
    mcp.run()
