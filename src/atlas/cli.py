"""Kommandozeile: gleiche Logik wie der MCP-Server, ohne Client."""

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
            print("Keine Treffer.")
            if data.get("hint"):
                print(f"Hinweis: {data['hint']}")
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
            print(f"  Pfad: {data['breadcrumb']}")
        print(f"  Anker: {data['anchor']}")
        for alt in data.get("alternatives", []):
            print(f"  Alternativ: {alt['heading']}  [{alt['anchor']}]")
        return

    if data.get("found") is False:
        print("Nichts gefunden.")
        if data.get("hint"):
            print(f"Hinweis: {data['hint']}")
        return

    if "body" in data:
        print(f"§ {data['heading']}   ({data['path']}:{data['line']})")
        if data.get("breadcrumb"):
            print(f"  Pfad: {data['breadcrumb']}")
        print()
        print(data["body"])
        if data.get("truncated"):
            print(f"\n… gekürzt ({data['chars']}/{data['total_chars']} Zeichen). "
                  f"Weiter mit offset={data['next_offset']}")
        return

    if "outline" in data:
        print(data["outline"])
        return

    if "tasks" in data:
        if not data["tasks"]:
            print("Keine Tasks.")
            return
        for item in data["tasks"]:
            mark = "x" if item["done"] else " "
            owner = f" @{item['owner']}" if item["owner"] else ""
            print(f"[{mark}] {item['text']}{owner}   ({item['anchor']})")
        return

    print(json.dumps(data, ensure_ascii=False, indent=2))


def _symbols(raw: str) -> list[str] | None:
    """'a, b' -> ['a', 'b'] — None, wenn nichts angegeben wurde."""
    items = [part.strip() for part in (raw or "").split(",") if part.strip()]
    return items or None


def _pairs(raw: list[str]) -> dict:
    """['HP=45', 'Status=✅'] -> {'HP': 45, 'Status': '✅'}"""
    from .sheets import parse_scalar

    result: dict = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"Erwartet SPALTE=WERT, bekommen: {item!r}")
        name, value = item.split("=", 1)
        result[name.strip()] = parse_scalar(value)
    return result


def build_parser() -> argparse.ArgumentParser:
    # --root/--json gelten global UND nach dem Subcommand ('atlas serve --root X'
    # ist die natürlichste Aufrufweise für MCP-Clients). SUPPRESS verhindert,
    # dass der Subparser den global gesetzten Wert mit None überschreibt.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help="Projektwurzel (Default: GDD_ROOT oder Aufwärtssuche)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="Roh-JSON ausgeben")

    parser = argparse.ArgumentParser(
        prog="atlas",
        description="atlas: Projekt- und Design-Dokumentation durchsuchbar für Agenten und Terminal.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", parents=[common], help="Index neu bauen")
    p.add_argument("--force", action="store_true", help="Alle Dateien neu einlesen")

    p = sub.add_parser("where", parents=[common], help="Wo steht ein Thema? (kompakt)")
    p.add_argument("topic")

    p = sub.add_parser("search", parents=[common], help="Volltextsuche")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--doc")

    p = sub.add_parser("read", parents=[common], help="Abschnitt über Anker lesen")
    p.add_argument("anchor")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-chars", type=int, default=3000)

    p = sub.add_parser("outline", parents=[common], help="Inhaltsverzeichnis")
    p.add_argument("--doc")

    p = sub.add_parser("tasks", parents=[common], help="Tasks aus Checkboxen")
    p.add_argument("--status", default="open", choices=["open", "done", "all"])
    p.add_argument("--owner")

    p = sub.add_parser("links", parents=[common], help="Beziehungen eines Abschnitts")
    p.add_argument("anchor")

    p = sub.add_parser("status", parents=[common], help="Fortschritt: Work-Order-Status")
    sub.add_parser("lint", parents=[common], help="Struktur prüfen (Sanierungs-Liste)")

    p = sub.add_parser("brief", parents=[common], help="Delegations-Prompt aus dem GDD bauen")
    p.add_argument("goal")
    p.add_argument("--sections", type=int, default=5, help="Anzahl GDD-Abschnitte (Default 5)")
    p.add_argument("--max-chars", type=int, default=8000)
    p.add_argument("--wo", type=int, help="Work-Order-Nummer für den Abschluss-Block")
    p.add_argument("--file", dest="files", action="append", default=[], metavar="PFAD",
                   help="Betroffene Code-Datei (mehrfach angebbar)")
    p.add_argument("--no-rules", action="store_true", help="Leitplanken weglassen")

    p = sub.add_parser("auth", parents=[common], help="Google-Freigabe (OAuth, öffnet Browser)")
    p.add_argument("--client-secret", help="Pfad zur client_secret.json")
    p.add_argument("--force", action="store_true", help="Vorhandenes Token verwerfen")

    p = sub.add_parser("sync", parents=[common], help="Google Docs/Sheets als Markdown holen")
    p.add_argument("--query", help="Name-Filter, z. B. 'GOREQUEST'")
    p.add_argument("--force", action="store_true", help="Auch unveränderte Dokumente neu holen")

    p = sub.add_parser("publish", parents=[common],
                       help="Work-Order-Statuszeile ins Master-Doc schreiben")
    p.add_argument("--wo", type=int, required=True, help="Work-Order-Nummer")
    p.add_argument("--status", default="done", choices=["done", "open"])
    p.add_argument("--title", default="", help="Kurzer Titel in Klammern")
    p.add_argument("--desc", default="", help="Ein Satz danach")
    p.add_argument("--section", default="Prototyp-Meilenstein-Plan",
                   help="Überschrift des Zielabschnitts")
    p.add_argument("--dry-run", action="store_true", help="Nur zeigen, was geschrieben würde")
    p.add_argument("--verify-symbols", default="",
                   help="Kommagetrennte Symbole, die diese WO gebaut hat (Anbindungs-Gate)")

    p = sub.add_parser("set-status", parents=[common], help="Status einer Work Order ändern")
    p.add_argument("--wo", type=int, required=True)
    p.add_argument("--status", required=True, choices=["done", "open"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify-symbols", default="")

    sub.add_parser("sheets", parents=[common],
                   help="Bekannte Spreadsheets und ihre Tabs")

    p = sub.add_parser("sheet-update", parents=[common],
                       help="Zellen einer Zeile über Schlüssel + Spaltenname setzen")
    p.add_argument("--tab", required=True, help="Blattname, z. B. 'Units'")
    p.add_argument("--key", required=True, help="Wert der Schlüsselspalte, z. B. 'monster_goblin'")
    p.add_argument("--set", dest="updates", action="append", default=[], metavar="SPALTE=WERT",
                   help="Mehrfach angebbar, z. B. --set HP=45 --set Status=✅")
    p.add_argument("--sheet", help="Spreadsheet-Name oder ID (bei mehreren Quellen)")
    p.add_argument("--key-column", help="Name der Schlüsselspalte (Default: erste Spalte)")
    p.add_argument("--raw", action="store_true",
                   help="RAW statt USER_ENTERED (keine Formel-/Datums-Interpretation)")
    p.add_argument("--dry-run", action="store_true", help="Nur den Diff zeigen")

    p = sub.add_parser("sheet-append", parents=[common],
                       help="Neue Zeile über Spaltennamen anhängen")
    p.add_argument("--tab", required=True)
    p.add_argument("--set", dest="values", action="append", default=[], metavar="SPALTE=WERT")
    p.add_argument("--sheet")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("doctor", parents=[common], help="Diagnose")

    p = sub.add_parser("init", parents=[common],
                       help="Projekt erfassen: Doku-Gerüst, Regeldateien, Index")
    p.add_argument("--out", default="docs/ARCHITECTURE.md",
                   help="Zielpfad der generierten Architektur-Doku")
    p.add_argument("--force", action="store_true",
                   help="Bestehende Doku überschreiben (Standard: nur ergänzen)")
    p.add_argument("--dry-run", action="store_true",
                   help="Nur zeigen, was entstehen würde — nichts schreiben")

    p = sub.add_parser("log", parents=[common], help="Schreibzugriffe anzeigen (Audit-Log)")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("code", parents=[common],
                       help="Funktion/Klasse im Code finden (statt die Datei zu lesen)")
    p.add_argument("symbol")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--usage", action="store_true",
                   help="Auch Aufrufstellen zeigen (Definition vs. Benutzung)")

    sub.add_parser("serve", parents=[common], help="MCP-Server über stdio starten")
    return parser


def main(argv: list[str] | None = None) -> int:
    import warnings

    # Der Google-Client warnt beim Import über das Python-EOL. Das gehört nicht
    # in die Tool-Ausgabe eines Agenten.
    warnings.filterwarnings("ignore", category=FutureWarning)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    root = find_root(getattr(args, "root", None))
    as_json = bool(getattr(args, "json", False))

    # Projekt-Synonyme für die Suche (.atlas/aliases.yml), mtime-geprüft.
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
            print(f"Fehler: {error}", file=sys.stderr)
            return 1
        print(json.dumps(gdocs.token_status(), ensure_ascii=False, indent=2))
        print("\nFreigabe erteilt: Lesen, Docs-Schreiben (`publish`) "
              "und Sheets-Schreiben (`sheet-update`).")
        return 0

    if args.command == "sync":
        from . import gdocs

        conn = open_index(root)
        try:
            result = gdocs.sync(root, query=args.query, force=args.force, conn=conn)
        except gdocs.SyncError as error:
            print(f"Fehler: {error}", file=sys.stderr)
            return 1

        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        for item in result["written"]:
            print(f"+ {item['name']}  ({item['kind']}, {item['chars']} Zeichen)")
            print(f"  -> {item['cache']}")
        for item in result["skipped"]:
            print(f"= {item['name']}  ({item['reason']})")
        for item in result["errors"]:
            print(f"! {item['name']}: {item['error']}", file=sys.stderr)
        print(f"\nIndex: {stats(conn)['sections']} Abschnitte in {stats(conn)['docs']} Dokumenten.")
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
            print(f"Fehler: {error}", file=sys.stderr)
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
            )
        except (gdocs.SyncError, ValueError) as error:
            print(f"Fehler: {error}", file=sys.stderr)
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
                print("Kein Spreadsheet als Quelle bekannt. "
                      "`atlas sync --query <Name>` ausführen.", file=sys.stderr)
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
            print(f"Fehler: {error}", file=sys.stderr)
            return 1

        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("dry_run"):
                return 0
            return 0 if result.get("verified") else 1

        prefix = "[dry-run] " if result.get("dry_run") else ""
        print(f"{prefix}{result['spreadsheet']} / {result['tab']}")
        if result.get("row_number"):
            print(f"  Zeile {result['row_number']} · Schlüssel {result['row_key']!r} "
                  f"in Spalte {result['key_column']!r}")
        for column, change in (result.get("changes") or {}).items():
            print(f"  {column} [{change['cell']}]: {change['old']!r} -> {change['new']!r}")
        if result.get("values"):
            print("  neue Zeile: " + ", ".join(
                f"{column}={value!r}" for column, value in result["values"].items()
            ))
        if result.get("appended_range"):
            print(f"  Bereich: {result['appended_range']}")
        if result.get("dry_run"):
            return 0
        status = "verifiziert" if result.get("verified") else "NICHT verifiziert"
        print(f"  {status}" + ("" if result.get("resynced") else " · Index nicht aktualisiert"))
        if result.get("warning"):
            print(f"  ! {result['warning']}", file=sys.stderr)
        return 0 if result.get("verified") else 1

    conn = open_index(root)

    # Vor Status/Brief/Lint den Google-Stand nachziehen (5-Minuten-Cache).
    if args.command in ("status", "brief", "lint"):
        from . import gdocs

        gdocs.auto_sync(root, conn)

    if args.command == "index":
        _emit(json.dumps(refresh(conn, root, force=args.force), ensure_ascii=False), as_json)
    elif args.command == "init":
        from . import bootstrap, lint as lint_mod

        report = bootstrap.init_project(root, out=args.out, force=args.force,
                                        dry_run=args.dry_run)
        if args.dry_run:
            # Nichts geschrieben, also nichts zu indizieren oder zu linten.
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        indexed = refresh(conn, root, force=True)
        # Nach dem Schreiben von managed.json mit Scope linten: sonst bricht
        # `init` in jedem Repo mit Alt-Doku an fremden Formbefunden ab.
        lint_result = json.loads(lint_mod.lint(conn, managed=bootstrap.load_managed(root)))
        report["indexed"] = indexed.get("sections", indexed)
        report["lint"] = {
            "sections": lint_result.get("sections"),
            "issues": lint_result.get("managed_issues", 0),
            "discovered": lint_result.get("discovered_issues", 0),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # 0 nur, wenn die atlas-eigenen Dokumente sauber sind.
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

        # Ohne managed.json ist alles verwaltet (Altprojekte, Google Docs) —
        # mit managed.json entscheidet nur atlas-eigene Doku über den Exit-Code.
        managed = bootstrap.load_managed(root)
        raw = lint_mod.lint(conn, managed=managed)
        data = json.loads(raw)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0 if data.get("ok") else 1

        print(f"Struktur-Check: {data['sections']} Abschnitte")
        if managed is not None:
            print(f"  verwaltet: {data['managed_issues']} Befunde · "
                  f"Altbestand: {data['discovered_issues']}")
        if not data["counts"]:
            print("Keine Befunde.")
            return 0

        for kind, count in data["counts"].items():
            print(f"  {kind:<22} {count}")
        print()
        for issue in data["issues"][:15]:
            where = f"{issue['path']}:{issue['line']}" if issue["path"] else "—"
            scope = "" if issue.get("scope") == "managed" else " (Altbestand)"
            print(f"[{issue['severity']:<7}] {issue['type']:<18} {where}{scope}")
            print(f"          § {issue['heading']}")
            print(f"          {issue['detail']}")
        if len(data["issues"]) > 15:
            print(f"\n… {len(data['issues']) - 15} weitere (--json für alle)")

        if data["discovered_issues"] and not data["managed_issues"]:
            print("\nDie Befunde betreffen nur fremde Alt-Doku — atlas-Dokumente "
                  "sind sauber.")
        return 0 if data.get("ok") else 1
    elif args.command == "doctor":
        from . import status as status_mod, verify

        payload = stats(conn)
        payload["active_root"] = str(root)
        # Regressions-Wächter: Symbole erledigter Work Orders gegen den Code.
        # Was hier auftaucht, ist ein ✅, das die Gegenwart nicht mehr deckt.
        try:
            items = json.loads(status_mod.status(conn))["work_orders"]
            mapping = verify.load_wo_symbols(root)
            payload["attachment_regressions"] = verify.regressions(root, items)
            payload["wo_symbols"] = mapping
            # Erledigte WOs ohne hinterlegte Symbole sind nicht überwacht —
            # eine Regression dort bliebe unbemerkt. Sichtbar machen, nicht ahnden.
            payload["work_orders_without_symbols"] = [
                item["wo"] for item in items
                if str(item.get("status", "")).lower() in {"done", "erledigt"}
                and item["wo"] not in mapping
            ]
        except Exception as error:  # Index leer/None — Diagnose darf nie abstürzen
            payload["attachment_regressions"] = []
            payload["verify_error"] = str(error)
        _emit(json.dumps(payload, ensure_ascii=False), as_json)
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
