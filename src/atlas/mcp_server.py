"""MCP-Server (stdio) für den GDD-Index."""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import search as search_mod
from .config import find_root
from .indexer import ensure_fresh, open_index

INSTRUCTIONS = (
    "Dieser Server indiziert das Game Design Document des Projekts. "
    "LESEN: Für JEDE Frage zum Game Design zuerst atlas_where oder atlas_search benutzen — "
    "niemals das GDD-Markdown oder .atlas/cache direkt mit Read/Grep öffnen. Die Dateien "
    "sind groß, der Index ist schneller und liefert nur den relevanten Abschnitt. "
    "Ablauf: atlas_where('Thema') -> atlas_read(anchor) für den Volltext. "
    "SCHREIBEN: atlas_set_status und atlas_publish erst nach grünen Tests aufrufen — "
    "Abhaken ist der letzte Schritt, nie der erste. Design-Prosa, Formeln und "
    "Balancing-Zahlen sind read-only; autonom erlaubt sind nur Status-Marker, "
    "Implementierungs-Spalten und Dateipfade. "
    "Balance-Werte NIEMALS als Zellkoordinate (C14) schreiben: atlas_sheet_update "
    "nimmt row_key + Spaltenname und rechnet die Zelle selbst aus. "
    # --- Direktheit: verhindert 15-Call-Ketten für eine Frage ---
    "DIREKTHEIT: Ein passendes Tool beantwortet die Frage in EINEM Aufruf. "
    "Work-Order-Status = atlas_status. Offene Tasks = atlas_tasks. Symbol/Datei = atlas_code_find. "
    "Rufe NICHT zusätzlich `list tools`, die CLI, die MCP-Quelldateien oder den Index auf, "
    "um ein Ergebnis zu bestätigen: Die Werkzeuge lesen dieselbe Quelle und lügen nicht. "
    "Ein zweiter Call ist nur berechtigt, wenn der erste eine Fehlermeldung enthielt. "
    "Status und Diagnose sind getrennt: atlas_status liefert die Work Orders, "
    "`atlas doctor` (nur CLI, kein MCP-Tool) prüft die Index-Gesundheit."
)

mcp = FastMCP("atlas", instructions=INSTRUCTIONS)

_root: Path | None = None
_conn = None


def configure(root: str | Path | None = None) -> None:
    """Setzt die Projektwurzel (CLI nutzt das für --root)."""
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
    """Warnkopf bei verletzter Invariante (Token-Scopes, unbestätigter Write).

    Ist alles in Ordnung, ist der Kopf leer und die Antwort bleibt unverändert —
    der Preis für die Absicherung ist dann null Tokens.

    JSON-Antworten bleiben JSON: Die Warnung wird als Feld `_health` eingehängt,
    nicht davorgesetzt. Ein Präfix vor `{…}` würde jeden Agenten brechen, der
    die Antwort parst.
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

    # Projekt-Synonyme laden (.atlas/aliases.yml) — einmal, mtime-geprüft.
    normalize.load_aliases(root / ".atlas" / "aliases.yml")
    # Erst den Google-Stand nachziehen (mit 5-Minuten-Cache), dann indexieren.
    gdocs.auto_sync(root, conn)
    ensure_fresh(conn, root)
    return root, conn


@mcp.tool()
@_health
def atlas_where(topic: str) -> str:
    """Wo steht etwas im GDD? Liefert Datei, Zeile, Heading und Anker — ohne Volltext.

    ERSTE WAHL bei 'Wo finde ich X?', 'In welcher Datei steht Y?', 'Welcher Abschnitt
    beschreibt Z?'. Antwort ist winzig (~60 Tokens). Danach bei Bedarf atlas_read(anchor).
    """
    _, conn = _fresh()
    return search_mod.where(conn, topic)


@mcp.tool()
@_health
def atlas_search(query: str, k: int = 5, doc: str | None = None) -> str:
    """Volltextsuche über alle GDD-Abschnitte, mit Snippets und Ranking (BM25).

    Lexikalisch über SQLite-FTS5 — **keine** Embeddings, keine Synonyme. Es werden
    Wörter gebraucht, die im GDD stehen. Umlaute werden gefaltet ("Hoehe" findet
    "Höhe"), deutsche Komposita teilweise ("Kampfsystem" findet "Kampf-System").

    Für unscharfe Themen ('wie funktioniert Deckung?') oder wenn atlas_where nichts
    findet. k = Anzahl Treffer (Default 5). doc = optional auf eine Dokument-ID
    beschränken.
    """
    _, conn = _fresh()
    return search_mod.search(conn, query, k=k, doc=doc)


@mcp.tool()
@_health
def atlas_read(anchor: str, offset: int = 0, max_chars: int = 3000) -> str:
    """Liest genau EINEN GDD-Abschnitt über seinen Anker (z. B. 'kampfsystem#schadensmodell').

    Nutze den Anker aus atlas_where/atlas_search/atlas_outline. Bei truncated=true mit
    next_offset weiterlesen. Das ist der einzige Weg, GDD-Inhalt zu lesen.
    """
    _, conn = _fresh()
    return search_mod.read(conn, anchor, offset=offset, max_chars=max_chars)


@mcp.tool()
@_health
def atlas_outline(doc: str | None = None) -> str:
    """Inhaltsverzeichnis aller GDD-Dokumente (oder eines einzelnen via doc='id').

    Nutze das, wenn du die Struktur noch nicht kennst oder ein Thema nirgends
    gefunden wurde.
    """
    _, conn = _fresh()
    return search_mod.outline(conn, doc=doc)


@mcp.tool()
@_health
def atlas_tasks(status: str = "open", owner: str | None = None) -> str:
    """Offene Design-/Entwicklungs-Tasks aus den Checkboxen des GDD.

    status: 'open' | 'done' | 'all'. owner: optional '@name' filtern.
    """
    _, conn = _fresh()
    return search_mod.tasks(conn, status=status, owner=owner)


@mcp.tool()
@_health
def atlas_links(anchor: str) -> str:
    """Beziehungen eines Abschnitts: betroffene Code-Dateien (implements), Tags, Nachbarabschnitte.

    Für 'Was hängt damit zusammen?' oder 'Welcher Code setzt diesen Abschnitt um?'.
    """
    _, conn = _fresh()
    return search_mod.links(conn, anchor)


@mcp.tool()
@_health
def atlas_brief(goal: str, max_sections: int = 5, work_order: int | None = None,
              files: list[str] | None = None) -> str:
    """Baut einen fertigen Task-Brief für einen Subagenten: relevante GDD-Abschnitte,
    betroffene Code-Dateien, die Projekt-Leitplanken und den Abschluss-Schritt.

    Nutze das, wenn du eine Aufgabe delegieren willst, statt dem Subagenten die
    GDD-Datei zu geben. Der Brief ist eigenständig lesbar und enthält stabile Anker.

    Args:
        goal: Die Aufgabe in einem Satz, z. B. 'Zwergen-Perk Gewölbestürmer anbinden'.
        max_sections: Anzahl der GDD-Abschnitte (Default 5).
        work_order: Optional die Work-Order-Nummer — dann enthält der Brief den
            fertigen `atlas_publish`-Aufruf für den Abschluss.
        files: Optional die betroffenen Code-Dateien, z. B. ['scripts/unit.gd'].
    """
    root, conn = _fresh()
    from . import brief as brief_mod

    return brief_mod.build(conn, root, goal, max_sections=max_sections,
                           work_order=work_order, extra_files=files)


@mcp.tool()
@_health
def atlas_publish(wo: int, title: str, description: str = "", status: str = "done",
                verify_symbols: list[str] | None = None) -> str:
    """Trägt eine Work-Order-Statuszeile ins Master-Doc ein — append-only und verifiziert.

    Nutze das als LETZTEN Schritt nach einem Task, statt selbst ins Google Doc zu
    schreiben. Das Tool hängt die Zeile an den Abschnitt 'Prototyp-Meilenstein-Plan',
    prüft die Zeile direkt über die Docs API und meldet nur Erfolg
    (`verified: true`), wenn sie wirklich im Dokument steht.

    status: 'done' (✅ ERLEDIGT) oder 'open' (⬜ OFFEN).

    verify_symbols: Die Symbole, die diese Work Order gebaut hat. Bei 'done' wird
    VOR dem Schreiben geprüft, ob sie definiert UND benutzt sind. Ein definiertes,
    aber nirgends aufgerufenes Symbol lässt das Schreiben scheitern
    (`written: false, refused: true`) — genau der Fall, an dem WO 8 gescheitert ist.
    Für erledigte Orders zusätzlich in `.atlas/wo-symbols.yml` hinterlegen, damit
    spätere Regressionen auffallen.
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
    """Ändert den Status einer BEREITS dokumentierten Work Order (✅ ⇄ ⬜).

    Für offene Orders, die fertig werden: `atlas_set_status(8, "done")`.
    Nutzt replaceAllText auf dem eindeutigen Status-String — kein Index-Gefrickel.
    Zum NEUEN Eintragen einer Order `atlas_publish` verwenden, nicht dieses Tool.

    Bei 'done' gilt derselbe Anbindungs-Nachweis wie bei `atlas_publish`: Symbole aus
    verify_symbols und aus `.atlas/wo-symbols.yml` müssen definiert und benutzt sein,
    sonst wird der Status nicht gesetzt.
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
    """Ändert einzelne Zellen einer Tabellenzeile — über Schlüssel + Spaltenname.

    NIE Zellkoordinaten wie 'C14' verwenden. Der Zeilen-Schlüssel ist der Wert der
    ersten Spalte (z. B. 'monster_goblin'), die Spalten heißen wie im Header
    (z. B. {"HP": 45, "Status": "✅"}).

    Args:
        tab: Blattname exakt wie im Spreadsheet (z. B. 'Units').
        row_key: Wert der Schlüsselspalte, der die Zeile eindeutig identifiziert.
        updates: {Spaltenname: neuer Wert} — nur die zu ändernden Spalten.
        sheet: Spreadsheet-Name oder ID, nur nötig wenn mehrere Quellen bekannt sind.
        key_column: Name der Schlüsselspalte, falls nicht die erste Spalte.
        dry_run: true = nur den Diff zeigen, nichts schreiben.

    Bei einem falschen Spaltennamen kommt die Liste der erlaubten Header zurück —
    danach den Call korrigieren. Nach Erfolg ist der Index sofort aktualisiert.
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
    """Hängt eine neue Zeile an ein Blatt an — Spalten über den Header, nicht über Positionen.

    Beispiel: tab='Units', values={"ID": "monster_goblin", "Name": "Goblin",
    "HP": 100, "Armor": 2}. Fehlende Spalten bleiben leer.

    Args:
        tab: Blattname exakt wie im Spreadsheet.
        values: {Spaltenname: Wert} der neuen Zeile.
        sheet: Spreadsheet-Name oder ID, nur bei mehreren Quellen nötig.
        dry_run: true = nur zeigen, was angehängt würde.
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
    """Listet die bekannten Spreadsheets und ihre Tab-Namen.

    Nutze das, bevor du atlas_sheet_update/atlas_sheet_append aufrufst und den exakten
    Blattnamen nicht kennst. Liefert nur Namen, keine Zellinhalte.
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
    """Findet die Definition einer Funktion oder Klasse im Code — Datei, Zeile, Signatur.

    Nutze das, BEVOR du eine `.gd`-Datei liest: `turn_manager.gd` hat 424.944
    Zeichen (~125.000 Tokens), eine Signatur kostet ~30. Für 'wo ist X definiert
    und wie ist die Signatur?' ist das der billige Weg.

    Args:
        symbol: Funktions- oder Klassenname (Teilstring genügt).
        limit: Maximale Treffer (Default 10).
        usage: true = zusätzlich die Aufrufstellen zeigen. Damit erkennst du, ob
            eine Funktion überhaupt benutzt wird — eine Implementierung ohne
            Aufrufer ist toter Code, auch wenn sie vollständig aussieht.

    Gesucht wird in `func`- und `class_name`-Definitionen. Variablen und Signale
    findest du damit nicht — dafür gezielt `grep -n` nutzen.
    """
    root, _ = _fresh()
    from . import code as code_mod

    return code_mod.render(root, symbol, limit=limit, with_usage=usage)


@mcp.tool()
@_health
def atlas_lint() -> str:
    """Prüft die GDD-Struktur und liefert die Liste der Formfehler, die den Index
    schwer lesbar machen (leere Überschriften, Abschnitte ohne Inhalt, zu lange
    Abschnitte, Work Orders ohne Status-Marker, Navigationszeilen).

    Nutze das, bevor du das GDD umstrukturierst — oder nach einem Agenten-Lauf.
    """
    from . import lint as lint_mod

    _, conn = _fresh()
    return lint_mod.lint(conn)


@mcp.tool()
@_health
def atlas_status() -> str:
    """Fortschritt des Projekts: welche Work Orders sind erledigt, welche offen.

    Nutze das, wenn du den aktuellen Stand brauchst — statt die GDD-Doku zu lesen.
    Liefert Nummer, Status und Titel als fertige Liste, plus eine Zeile, wie alt
    der Stand ist. Diese Antwort ist die Quelle: kein zweiter Aufruf, keine CLI,
    kein Blick in den Quellcode nötig.
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
    """Komplette GDD-Struktur als Text."""
    _, conn = _fresh()
    return search_mod.outline(conn, max_chars=8000)


@mcp.resource("atlas://section/{anchor}")
def resource_section(anchor: str) -> str:
    """Ein einzelner Abschnitt als Text."""
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
