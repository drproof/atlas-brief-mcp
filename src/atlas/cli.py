"""Command-line interface: same logic as the MCP server, without a client."""

from __future__ import annotations

import argparse
import json
import sys

from . import search as search_mod
from .config import cache_dir, find_root
from .indexer import open_index, refresh, stats


def _emit(payload: str, as_json: bool) -> None:
    if as_json:
        try:
            data = json.loads(payload)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(payload)
        return

    data = json.loads(payload)

    if "hits" in data:
        if not data["hits"]:
            print("No matches.")
            if data.get("hint"):
                print(f"Hint: {data['hint']}")
            return
        for hit in data["hits"]:
            print(f"{hit['path']}:{hit['line']}  § {hit['heading']}")
            print(f"  {hit['anchor']}  (score {hit['score']})")
            if hit.get("snippet"):
                print(f"  {hit['snippet']}")
            print()
        return

    if data.get("found"):
        print(f"{data['path']}:{data['line']}  § {data['heading']}")
        if data.get("breadcrumb"):
            print(f"  Path: {data['breadcrumb']}")
        print(f"  Anchor: {data['anchor']}")
        for alt in data.get("alternatives", []):
            print(f"  Alternative: {alt['heading']}  [{alt['anchor']}]")
        return

    if data.get("found") is False:
        print("Nothing found.")
        if data.get("hint"):
            print(f"Hint: {data['hint']}")
        return

    if "body" in data:
        print(f"§ {data['heading']}   ({data['path']}:{data['line']})")
        if data.get("breadcrumb"):
            print(f"  Path: {data['breadcrumb']}")
        print()
        print(data["body"])
        if data.get("truncated"):
            print(f"\n… truncated ({data['chars']}/{data['total_chars']} chars). "
                  f"Continue with offset={data['next_offset']}")
        return

    if "outline" in data:
        print(data["outline"])
        return

    if "tasks" in data:
        if not data["tasks"]:
            print("No tasks.")
            return
        for item in data["tasks"]:
            mark = "x" if item["done"] else " "
            owner = f" @{item['owner']}" if item["owner"] else ""
            print(f"[{mark}] {item['text']}{owner}   ({item['anchor']})")
        return

    print(json.dumps(data, ensure_ascii=False, indent=2))


def _symbols(raw: str) -> list[str] | None:
    """'a, b' -> ['a', 'b'] — None when nothing was provided."""
    items = [part.strip() for part in (raw or "").split(",") if part.strip()]
    return items or None


def _pairs(raw: list[str]) -> dict:
    """['HP=45', 'Status=✅'] -> {'HP': 45, 'Status': '✅'}"""
    from .sheets import parse_scalar

    result: dict = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"Expected COLUMN=VALUE, got: {item!r}")
        name, value = item.split("=", 1)
        result[name.strip()] = parse_scalar(value)
    return result


def build_parser() -> argparse.ArgumentParser:
    # --root/--json apply both globally AND after the subcommand ('atlas serve --root X'
    # is the most natural invocation for MCP clients). SUPPRESS prevents the
    # subparser from overwriting the globally set value with None.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help="project root (default: ATLAS_ROOT or upward search)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit raw JSON")

    from . import __version__

    parser = argparse.ArgumentParser(
        prog="atlas",
        description="atlas: project and design documentation, searchable for agents and terminals.",
        parents=[common],
    )
    parser.add_argument("-V", "--version", action="version", version=f"atlas {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", parents=[common], help="rebuild the index")
    p.add_argument("--force", action="store_true", help="re-read all files")

    p = sub.add_parser("where", parents=[common], help="where is a topic? (compact)")
    p.add_argument("topic")

    p = sub.add_parser("search", parents=[common], help="full-text search")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--doc")

    p = sub.add_parser("read", parents=[common], help="read a section by anchor")
    p.add_argument("anchor")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-chars", type=int, default=3000)

    p = sub.add_parser("outline", parents=[common], help="table of contents")
    p.add_argument("--doc")

    p = sub.add_parser("tasks", parents=[common], help="tasks from checkboxes")
    p.add_argument("--status", default="open", choices=["open", "done", "all"])
    p.add_argument("--owner")

    p = sub.add_parser("links", parents=[common], help="relations of a section")
    p.add_argument("anchor")

    p = sub.add_parser("status", parents=[common], help="progress: work order status")
    sub.add_parser("lint", parents=[common], help="check structure (cleanup list)")

    p = sub.add_parser("brief", parents=[common], help="build a delegation prompt from the docs")
    p.add_argument("goal")
    p.add_argument("--sections", type=int, default=5, help="number of sections (default 5)")
    p.add_argument("--max-chars", type=int, default=8000)
    p.add_argument("--wo", type=int, help="work order number for the closing block")
    p.add_argument("--file", dest="files", action="append", default=[], metavar="PATH",
                   help="affected code file (repeatable)")
    p.add_argument("--no-rules", action="store_true", help="omit guardrails")

    p = sub.add_parser("auth", parents=[common], help="Google consent (OAuth, opens the browser)")
    p.add_argument("--client-secret", help="path to client_secret.json")
    p.add_argument("--force", action="store_true", help="discard the existing token")

    p = sub.add_parser("sync", parents=[common], help="fetch Google Docs/Sheets as Markdown")
    p.add_argument("--query", help="name filter, e.g. 'MYPROJECT'")
    p.add_argument("--force", action="store_true", help="also re-fetch unchanged documents")

    p = sub.add_parser("publish", parents=[common],
                       help="append a work order status line")
    p.add_argument("--wo", type=int, required=True, help="work order number")
    p.add_argument("--status", default="done", choices=["done", "open"])
    p.add_argument("--title", default="", help="short title in parentheses")
    p.add_argument("--desc", default="", help="one sentence after it")
    p.add_argument("--section", default="Work Orders",
                   help="heading of the target section")
    p.add_argument("--dry-run", action="store_true", help="show what would be written, write nothing")
    p.add_argument("--verify-symbols", default="",
                   help="comma-separated symbols this order built (wiring gate)")

    p = sub.add_parser("set-status", parents=[common],
                       help="change the status of a work order or checkbox task")
    p.add_argument("--wo", type=int, default=None, help="work order number")
    p.add_argument("--task", default=None, help="checkbox task substring (- [ ] <-> - [x])")
    p.add_argument("--status", required=True, choices=["done", "open"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify-symbols", default="")

    sub.add_parser("sheets", parents=[common],
                   help="known spreadsheets and their tabs")

    p = sub.add_parser("sheet-update", parents=[common],
                       help="set cells of one row via key + column name")
    p.add_argument("--tab", required=True, help="sheet name, e.g. 'Units'")
    p.add_argument("--key", required=True, help="key column value, e.g. 'monster_goblin'")
    p.add_argument("--set", dest="updates", action="append", default=[], metavar="COLUMN=VALUE",
                   help="repeatable, e.g. --set HP=45 --set Status=✅")
    p.add_argument("--sheet", help="spreadsheet name or id (with several sources)")
    p.add_argument("--key-column", help="key column name (default: first column)")
    p.add_argument("--raw", action="store_true",
                   help="RAW instead of USER_ENTERED (no formula/date interpretation)")
    p.add_argument("--dry-run", action="store_true", help="show the diff only")

    p = sub.add_parser("sheet-append", parents=[common],
                       help="append a new row via column names")
    p.add_argument("--tab", required=True)
    p.add_argument("--set", dest="values", action="append", default=[], metavar="COLUMN=VALUE")
    p.add_argument("--sheet")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("measure", parents=[common],
                       help="token cost per question: naive read vs. index")
    p.add_argument("--questions", help="file with one question per line")
    p.add_argument("--sample", type=int, default=8,
                   help="number of generated questions (default 8)")
    p.add_argument("--limit", type=int, default=12,
                   help="rows shown in the table (default 12)")

    sub.add_parser("doctor", parents=[common], help="diagnostics")
    sub.add_parser("check", parents=[common],
                   help="verify completed work-order symbols and implements links (CI / pre-commit)")

    p = sub.add_parser("init", parents=[common],
                       help="bootstrap a repo: doc skeleton, rule files, index")
    p.add_argument("--out", default="docs/ARCHITECTURE.md",
                   help="target path of the generated architecture doc")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing docs (default: only add)")
    p.add_argument("--update", action="store_true",
                   help="refresh domain metrics, hotspots, and unwired symbols while keeping notes and Work Orders")
    p.add_argument("--hook", action="store_true",
                   help="install a Git pre-commit hook running 'atlas check'")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be created — write nothing")

    p = sub.add_parser("log", parents=[common], help="show write operations (audit log)")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("code", parents=[common],
                       help="find a function/class in code (instead of reading the file)")
    p.add_argument("symbol")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--usage", action="store_true",
                   help="also show call sites (definition vs. usage)")

    sub.add_parser("serve", parents=[common], help="start the MCP server over stdio")
    return parser


def main(argv: list[str] | None = None) -> int:
    import warnings

    # The Google client warns about Python EOL on import. That does not belong
    # in an agent's tool output.
    warnings.filterwarnings("ignore", category=FutureWarning)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    root = find_root(getattr(args, "root", None))
    as_json = bool(getattr(args, "json", False))

    # Project synonyms for search (.atlas/aliases.yml), mtime-checked.
    from . import normalize

    normalize.load_aliases(cache_dir(root) / "aliases.yml")

    if args.command == "serve":
        from . import mcp_server

        mcp_server.configure(root)
        mcp_server.run()
        return 0

    if args.command == "auth":
        from . import gdocs

        if args.force and gdocs.TOKEN_PATH.exists():
            gdocs.TOKEN_PATH.unlink()
        try:
            gdocs.load_credentials(interactive=True, client_secret=args.client_secret,
                                   scopes=gdocs.WRITE_SCOPES)
        except gdocs.SyncError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        print(json.dumps(gdocs.token_status(), ensure_ascii=False, indent=2))
        print("\nAccess granted: read, Docs write (`publish`) "
              "and Sheets write (`sheet-update`).")
        return 0

    if args.command == "sync":
        from . import gdocs

        conn = open_index(root)
        try:
            result = gdocs.sync(root, query=args.query, force=args.force, conn=conn)
        except gdocs.SyncError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        for item in result["written"]:
            print(f"+ {item['name']}  ({item['kind']}, {item['chars']} chars)")
            print(f"  -> {item['cache']}")
        for item in result["skipped"]:
            print(f"= {item['name']}  ({item['reason']})")
        for item in result["errors"]:
            print(f"! {item['name']}: {item['error']}", file=sys.stderr)
        print(f"\nIndex: {stats(conn)['sections']} sections in {stats(conn)['docs']} documents.")
        return 1 if result["errors"] else 0

    if args.command == "publish":
        from . import gdocs, publish as publish_mod

        conn = open_index(root)
        try:
            result = publish_mod.publish(
                root, conn, args.wo,
                status=args.status,
                title=args.title,
                description=args.desc,
                section=args.section,
                dry_run=args.dry_run,
                verify_symbols=_symbols(args.verify_symbols),
            )
        except (gdocs.SyncError, ValueError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("refused"):
            return 1
        if result.get("dry_run"):
            return 0
        return 0 if result.get("verified") else 1

    if args.command == "set-status":
        from . import gdocs, publish as publish_mod

        conn = open_index(root)
        try:
            result = publish_mod.set_status(
                root, conn, args.wo, status=args.status, dry_run=args.dry_run,
                verify_symbols=_symbols(args.verify_symbols),
                task=args.task,
            )
        except (gdocs.SyncError, ValueError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("refused"):
            return 1
        if result.get("dry_run") or result.get("changed") is False:
            return 0
        return 0 if result.get("verified") else 1

    if args.command in ("sheets", "sheet-update", "sheet-append"):
        from . import gdocs, sheets as sheets_mod

        conn = open_index(root)

        if args.command == "sheets":
            sources = sheets_mod.known_spreadsheets(conn)
            if not sources:
                print("No spreadsheet source known. "
                      "Run `atlas sync --query <Name>` first.", file=sys.stderr)
                return 1
            service = sheets_mod._service()
            payload = []
            for source in sources:
                item = {"name": source["name"], "id": source["id"], "tabs": []}
                try:
                    item["tabs"] = sheets_mod.list_tabs(service, source["id"])
                except Exception as error:
                    item["error"] = str(error)
                payload.append(item)
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0
            for item in payload:
                print(f"{item['name']}  ({item['id']})")
                for tab in item["tabs"]:
                    print(f"  - {tab}")
                if item.get("error"):
                    print(f"  ! {item['error']}", file=sys.stderr)
            return 0

        try:
            values = _pairs(args.values if args.command == "sheet-append" else args.updates)
            if args.command == "sheet-update":
                result = sheets_mod.update(
                    root, conn, args.tab, args.key, values,
                    sheet=args.sheet,
                    key_column=args.key_column,
                    dry_run=args.dry_run,
                    value_input="RAW" if args.raw else "USER_ENTERED",
                )
            else:
                result = sheets_mod.append(
                    root, conn, args.tab, values,
                    sheet=args.sheet, dry_run=args.dry_run,
                )
        except (gdocs.SyncError, sheets_mod.SheetError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("dry_run"):
                return 0
            return 0 if result.get("verified") else 1

        prefix = "[dry-run] " if result.get("dry_run") else ""
        print(f"{prefix}{result['spreadsheet']} / {result['tab']}")
        if result.get("row_number"):
            print(f"  Row {result['row_number']} · key {result['row_key']!r} "
                  f"in column {result['key_column']!r}")
        for column, change in (result.get("changes") or {}).items():
            print(f"  {column} [{change['cell']}]: {change['old']!r} -> {change['new']!r}")
        if result.get("values"):
            print("  new row: " + ", ".join(
                f"{column}={value!r}" for column, value in result["values"].items()
            ))
        if result.get("appended_range"):
            print(f"  range: {result['appended_range']}")
        if result.get("dry_run"):
            return 0
        status = "verified" if result.get("verified") else "NOT verified"
        print(f"  {status}" + ("" if result.get("resynced") else " · index not updated"))
        if result.get("warning"):
            print(f"  ! {result['warning']}", file=sys.stderr)
        return 0 if result.get("verified") else 1

    conn = open_index(root)

    # Pull the latest Google state before any read command (5-minute cache).
    # `where`/`search`/`read`/`outline`/`links` used to be missing here — a change
    # in the doc was only visible after an explicit `atlas sync`, even though the
    # MCP counterpart (`_fresh`) always runs it.
    if args.command in ("status", "brief", "lint", "where", "search", "read",
                        "outline", "links", "tasks", "publish", "set-status"):
        from . import gdocs

        gdocs.auto_sync(root, conn)

    if args.command == "index":
        _emit(json.dumps(refresh(conn, root, force=args.force), ensure_ascii=False), as_json)
    elif args.command == "init":
        from . import bootstrap, lint as lint_mod

        report = bootstrap.init_project(
            root, out=args.out, force=args.force, dry_run=args.dry_run,
            update=args.update, hook=args.hook,
        )
        if args.dry_run:
            # Nothing written, so nothing to index or lint.
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        indexed = refresh(conn, root, force=True)
        # Lint with scope after writing managed.json: otherwise `init` fails
        # on unrelated legacy doc issues in any repo with existing docs.
        lint_result = json.loads(lint_mod.lint(conn, managed=bootstrap.load_managed(root)))
        report["indexed"] = indexed.get("sections", indexed)
        report["lint"] = {
            "sections": lint_result.get("sections"),
            "issues": lint_result.get("managed_issues", 0),
            "discovered": lint_result.get("discovered_issues", 0),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # Return 0 only when atlas-managed documents are clean.
        return 0 if lint_result.get("ok") else 1
    elif args.command == "where":
        _emit(search_mod.where(conn, args.topic), as_json)
    elif args.command == "search":
        _emit(search_mod.search(conn, args.query, k=args.k, doc=args.doc), as_json)
    elif args.command == "read":
        _emit(search_mod.read(conn, args.anchor, offset=args.offset,
                             max_chars=args.max_chars), as_json)
    elif args.command == "outline":
        _emit(search_mod.outline(conn, doc=args.doc), as_json)
    elif args.command == "tasks":
        _emit(search_mod.tasks(conn, status=args.status, owner=args.owner), as_json)
    elif args.command == "links":
        _emit(search_mod.links(conn, args.anchor), as_json)
    elif args.command == "brief":
        from . import brief as brief_mod

        print(brief_mod.build(
            conn, root, args.goal,
            max_sections=args.sections,
            max_chars=args.max_chars,
            include_rules=not args.no_rules,
            work_order=args.wo,
            extra_files=args.files,
        ))
    elif args.command == "status":
        from . import status as status_mod

        raw = status_mod.status(conn)
        if as_json:
            print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
            return 0
        data = json.loads(raw)
        from . import status as status_mod

        print(status_mod.status_text(data))
    elif args.command == "lint":
        from . import bootstrap, lint as lint_mod

        # Without managed.json everything is managed (legacy projects, Google Docs) —
        # with managed.json only atlas-managed docs determine the exit code.
        managed = bootstrap.load_managed(root)
        raw = lint_mod.lint(conn, managed=managed)
        data = json.loads(raw)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0 if data.get("ok") else 1

        print(f"Structure check: {data['sections']} sections")
        if managed is not None:
            print(f"  managed: {data['managed_issues']} issues · "
                  f"discovered: {data['discovered_issues']}")
        if not data["counts"]:
            print("No issues found.")
            return 0

        for kind, count in data["counts"].items():
            print(f"  {kind:<22} {count}")
        print()
        for issue in data["issues"][:15]:
            where = f"{issue['path']}:{issue['line']}" if issue["path"] else "—"
            scope = "" if issue.get("scope") == "managed" else " (discovered)"
            print(f"[{issue['severity']:<7}] {issue['type']:<18} {where}{scope}")
            print(f"          § {issue['heading']}")
            print(f"          {issue['detail']}")
        if len(data["issues"]) > 15:
            print(f"\n… {len(data['issues']) - 15} more (--json for all)")

        if data["discovered_issues"] and not data["managed_issues"]:
            print("\nFindings only affect discovered legacy docs — atlas-managed "
                  "documents are clean.")
        return 0 if data.get("ok") else 1
    elif args.command == "measure":
        from . import measure as measure_mod

        questions = None
        if args.questions:
            path_obj = Path(args.questions)
            questions = [
                line.strip() for line in path_obj.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        payload = measure_mod.measure(root, conn, questions=questions, sample=args.sample)
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(measure_mod.render(payload, limit=args.limit))

    elif args.command == "doctor":
        from . import status as status_mod, verify

        payload = stats(conn)
        payload["active_root"] = str(root)
        # Regression guard: check symbols of completed work orders against the code.
        # Anything listed here is a ✅ that no longer matches current reality.
        try:
            items = json.loads(status_mod.status(conn))["work_orders"]
            mapping = verify.load_wo_symbols(root)
            payload["attachment_regressions"] = verify.regressions(root, items)
            payload["wo_symbols"] = mapping
            # Completed WOs without recorded symbols are unmonitored —
            # a regression there would go unnoticed. Surface them without failing.
            payload["work_orders_without_symbols"] = [
                item["wo"] for item in items
                if str(item.get("status", "")).lower() in {"done", "erledigt"}
                and item["wo"] not in mapping
            ]
        except Exception as error:  # empty/None index — diagnostics must never crash
            payload["attachment_regressions"] = []
            payload["verify_error"] = str(error)
        _emit(json.dumps(payload, ensure_ascii=False), as_json)
    elif args.command == "check":
        from . import verify

        report = verify.check_project(root, conn)
        if as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(verify.render_check(report))
        return 0 if report["ok"] else 1
    elif args.command == "log":
        from . import audit

        if as_json:
            print(json.dumps(audit.tail(root, limit=args.limit), ensure_ascii=False, indent=2))
        else:
            print(audit.render(root, limit=args.limit))
    elif args.command == "code":
        from . import code as code_mod

        if as_json:
            print(json.dumps(code_mod.find(root, args.symbol, limit=args.limit),
                             ensure_ascii=False, indent=2))
        else:
            print(code_mod.render(root, args.symbol, limit=args.limit,
                                  with_usage=args.usage))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
