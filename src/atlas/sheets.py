"""Deterministische Sheet-Mutationen: Zeilen-Schlüssel + Spaltenname -> A1-Zelle.

Der Agent nennt `row_key="monster_goblin"` und `{"HP": 45}`. Er nennt **nie**
`C14`. Koordinaten (Zeile, Spalte, Buchstabe) rechnet ausschließlich Python aus.

Warum: LLMs verzählen sich in Tabellen systematisch — Header, Trennlinie,
0- vs. 1-basiert. Ein verschobener Schreibvorgang überschreibt den Nachbareintrag
und fällt niemandem auf. Deshalb gibt es hier keine Koordinaten-Eingabe.

Drei Regeln, die das Modul durchsetzt:
1. **Whitelist** — nur Spreadsheets, die als Quelle in `sync_state` stehen.
2. **Namensauflösung** — Spalten werden über den Header gesucht, nie über einen
   Index. Unbekannte Spalte -> Fehler *mit Liste der erlaubten Header*, damit der
   Agent im zweiten Versuch selbst korrigiert.
3. **Rücklesen** — nach dem Schreiben werden genau die geänderten Zellen erneut
   gelesen. `verified` ist der entscheidende Rückgabewert, nicht "hat nicht geworfen".
"""

from __future__ import annotations

import re
from typing import Any

from . import audit, gdocs

SHEET_MIME = gdocs.SHEET_MIME


class SheetError(ValueError):
    """Fehler mit Handlungsanweisung — landet direkt im Tool-Output."""


# --------------------------------------------------------------------------
# A1-Notation
# --------------------------------------------------------------------------

def col_letter(index: int) -> str:
    """0-basierter Spaltenindex -> A1-Buchstabe (0 -> A, 25 -> Z, 26 -> AA)."""
    if index < 0:
        raise ValueError(f"Spaltenindex darf nicht negativ sein: {index}")
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def col_index(letters: str) -> int:
    """Umkehrung von col_letter — für Tests und Rückübersetzung."""
    value = 0
    for char in letters.strip().upper():
        if not char.isalpha():
            raise ValueError(f"Kein Spaltenbuchstabe: {letters!r}")
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def quote_tab(tab: str) -> str:
    """Tab-Name für A1-Ranges: 'Mein Blatt' -> 'Mein Blatt' (Apostroph verdoppelt)."""
    return "'" + str(tab).replace("'", "''") + "'"


# --------------------------------------------------------------------------
# Werte-Normalisierung
# --------------------------------------------------------------------------

def norm_key(value: Any) -> str:
    """Schlüssel-Vergleich: Groß/Klein und Leerraum egal."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _norm_value(value: Any) -> str:
    """Wert-Vergleich für die Verifikation.

    Google formatiert bei USER_ENTERED um: "45" -> 45, "1.50" -> 1.5,
    Checkbox -> TRUE. Deshalb wird vor dem Vergleich normalisiert.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    text = str(value if value is not None else "").strip()
    if text.startswith("="):
        return text  # Formel bleibt Formel
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text.casefold()
    return f"{number:g}"


def same_value(left: Any, right: Any) -> bool:
    return _norm_value(left) == _norm_value(right)


def parse_scalar(raw: str) -> Any:
    """CLI-Wert -> int/float/bool/str. Führende Nullen bleiben Text ('007')."""
    text = str(raw).strip()
    if text.lower() in ("true", "wahr"):
        return True
    if text.lower() in ("false", "falsch"):
        return False
    if re.fullmatch(r"-?(0|[1-9]\d*)", text):
        return int(text)
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return float(text.replace(",", "."))
    return text


# --------------------------------------------------------------------------
# Quelle auflösen
# --------------------------------------------------------------------------

def known_spreadsheets(conn) -> list[dict]:
    return [s for s in gdocs.known_sources(conn) if s["mimeType"] == SHEET_MIME]


def resolve_spreadsheet(conn, hint: str | None = None) -> dict:
    """Findet das Spreadsheet: per ID, per Namens-Teilstring oder als einziges."""
    sources = known_spreadsheets(conn)
    if not sources:
        raise SheetError(
            "Kein Spreadsheet als Quelle bekannt. Einmal `atlas sync --query <Name>` "
            "ausführen, dann steht es im Index."
        )

    if hint:
        wanted = str(hint).strip()
        for source in sources:
            if wanted == source["id"]:
                return source
        matches = [s for s in sources if norm_key(wanted) in norm_key(s["name"])]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            names = ", ".join(f"'{s['name']}'" for s in sources)
            raise SheetError(f"Kein Spreadsheet passt zu '{hint}'. Bekannt: {names}")
        names = ", ".join(f"'{s['name']}'" for s in matches)
        raise SheetError(f"Mehrdeutig: {names}. Vollen Namen oder die ID angeben.")

    if len(sources) == 1:
        return sources[0]
    names = ", ".join(f"'{s['name']}'" for s in sources)
    raise SheetError(f"Mehrere Spreadsheets bekannt — 'sheet' angeben: {names}")


def list_tabs(service, spreadsheet_id: str) -> list[str]:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, includeGridData=False
    ).execute()
    return [
        sheet.get("properties", {}).get("title", "")
        for sheet in meta.get("sheets", [])
    ]


# --------------------------------------------------------------------------
# Blatt lesen und Zeile finden
# --------------------------------------------------------------------------

def read_tab(service, spreadsheet_id: str, tab: str) -> list[list[str]]:
    try:
        response = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=quote_tab(tab)
        ).execute()
    except Exception as error:
        raise SheetError(gdocs._explain(error)) from error
    return response.get("values", []) or []


def headers_of(rows: list[list[str]]) -> list[str]:
    return [str(cell).strip() for cell in rows[0]] if rows else []


def resolve_column(headers: list[str], name: str) -> int:
    """Spaltenname -> Index. Unbekannt: Fehler mit der Liste der erlaubten Spalten."""
    cleaned = str(name).strip()
    if cleaned in headers:
        index = headers.index(cleaned)
    else:
        lowered = [header.casefold() for header in headers]
        matches = [i for i, header in enumerate(lowered) if header == cleaned.casefold()]
        if not matches:
            allowed = ", ".join(repr(header) for header in headers if header)
            raise SheetError(
                f"Unbekannte Spalte {cleaned!r}. Erlaubte Spalten: [{allowed}]"
            )
        if len(matches) > 1:
            raise SheetError(
                f"Spalte {cleaned!r} kommt {len(matches)}x im Header vor — "
                "nicht eindeutig, bitte im Blatt umbenennen."
            )
        index = matches[0]

    if headers.count(headers[index]) > 1:
        raise SheetError(
            f"Spaltenname {headers[index]!r} ist mehrfach im Header vorhanden. "
            "Eine Zelle lässt sich so nicht eindeutig adressieren."
        )
    return index


def find_row(
    rows: list[list[str]], row_key: Any, key_column: str | None = None
) -> tuple[int, list[str], list[str], int]:
    """Sucht die Zeile über den Schlüsselwert.

    Rückgabe: (Zeilennummer 1-basiert, Header, Zeilendaten, Key-Spaltenindex).
    `rows[1:]` beginnt bei Zeile 2 — die einsame Stelle, an der gezählt wird.
    """
    if not rows:
        raise SheetError("Das Blatt ist leer — es gibt keine Header-Zeile.")

    headers = headers_of(rows)
    if key_column:
        key_index = resolve_column(headers, key_column)
    else:
        key_index = 0

    wanted = norm_key(row_key)
    if not wanted:
        raise SheetError("row_key ist leer.")

    for offset, row in enumerate(rows[1:]):
        cell = row[key_index] if len(row) > key_index else ""
        if norm_key(cell) == wanted:
            return offset + 2, headers, list(row), key_index

    column_name = headers[key_index] if len(headers) > key_index else "Spalte 1"
    sample = [
        str(row[key_index]) if len(row) > key_index else ""
        for row in rows[1:6]
    ]
    sample = [value for value in sample if value]
    hint = f" Vorhandene Schlüssel (Auszug): {', '.join(sample)}" if sample else ""
    raise SheetError(
        f"Kein Eintrag {row_key!r} in Spalte {column_name!r} des Blatts gefunden.{hint}"
    )


# --------------------------------------------------------------------------
# Änderungen planen
# --------------------------------------------------------------------------

def plan_changes(
    headers: list[str], row: list[str], updates: dict[str, Any], row_number: int
) -> list[dict]:
    """Mappt {Spaltenname: neuer Wert} auf konkrete Zellen mit Vorher/Nachher."""
    if not updates:
        raise SheetError("Keine Änderungen angegeben (updates ist leer).")

    changes: list[dict] = []
    seen: set[int] = set()
    for name, new_value in updates.items():
        index = resolve_column(headers, name)
        if index in seen:
            raise SheetError(f"Spalte {headers[index]!r} wurde doppelt angegeben.")
        seen.add(index)
        old_value = row[index] if len(row) > index else ""
        changes.append({
            "column": headers[index],
            "cell": f"{col_letter(index)}{row_number}",
            "old": old_value,
            "new": new_value,
            "index": index,
        })
    return changes


def _batch_update(service, spreadsheet_id: str, tab: str, changes: list[dict],
                  value_input: str) -> None:
    data = [
        {
            "range": f"{quote_tab(tab)}!{change['cell']}",
            "values": [[change["new"]]],
        }
        for change in changes
    ]
    try:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": value_input, "data": data},
        ).execute()
    except Exception as error:
        raise SheetError(gdocs._explain(error)) from error


def read_back(service, spreadsheet_id: str, tab: str, cells: list[str]) -> dict[str, str]:
    """Liest genau die geänderten Zellen zurück — FORMULA, damit Formeln sichtbar bleiben."""
    if not cells:
        return {}
    ranges = [f"{quote_tab(tab)}!{cell}" for cell in cells]
    response = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id, ranges=ranges, valueRenderOption="FORMULA"
    ).execute()

    values: dict[str, str] = {}
    for cell, value_range in zip(cells, response.get("valueRanges", [])):
        rows = value_range.get("values") or [[""]]
        values[cell] = str(rows[0][0]) if rows and rows[0] else ""
    return values


# --------------------------------------------------------------------------
# Öffentliche Operationen
# --------------------------------------------------------------------------

def _service(client_secret: str | None = None):
    from googleapiclient.discovery import build

    creds = gdocs.load_credentials(
        interactive=False, client_secret=client_secret, scopes=gdocs.WRITE_SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def describe_change(change: dict) -> str:
    return (
        f"{change['column']} [{change['cell']}]: "
        f"{change['old']!r} -> {change['new']!r}"
    )


def update(
    root,
    conn,
    tab: str,
    row_key: Any,
    updates: dict[str, Any],
    sheet: str | None = None,
    key_column: str | None = None,
    dry_run: bool = False,
    value_input: str = "USER_ENTERED",
    client_secret: str | None = None,
) -> dict:
    """Aktualisiert benannte Spalten genau einer Zeile, dann Re-Index."""
    source = resolve_spreadsheet(conn, sheet)
    gdocs.assert_writable(conn, source["id"])

    service = _service(client_secret)
    tabs = list_tabs(service, source["id"])
    if tab not in tabs:
        allowed = ", ".join(repr(name) for name in tabs)
        raise SheetError(f"Tab {tab!r} existiert nicht. Vorhandene Tabs: [{allowed}]")

    rows = read_tab(service, source["id"], tab)
    row_number, headers, row, key_index = find_row(rows, row_key, key_column)
    changes = plan_changes(headers, row, updates, row_number)

    payload: dict[str, Any] = {
        "spreadsheet": source["name"],
        "spreadsheet_id": source["id"],
        "tab": tab,
        "row_key": row_key,
        "key_column": headers[key_index] if len(headers) > key_index else "",
        "row_number": row_number,
        "changes": {
            change["column"]: {
                "cell": change["cell"],
                "old": change["old"],
                "new": change["new"],
            }
            for change in changes
        },
        "dry_run": dry_run,
        "value_input": value_input,
    }
    if dry_run:
        return payload

    _batch_update(service, source["id"], tab, changes, value_input)

    # --- Selbstverifikation: geänderte Zellen UND die Schlüsselzelle ---------
    # Nur die Zielzelle zu prüfen reicht nicht. Wird die Zeile zwischen Lesen und
    # Schreiben im Browser sortiert oder gelöscht, landet der Wert an der falschen
    # Zeile — das Rücklesen dieser Koordinate meldet dann trotzdem den geschriebenen
    # Wert. Die Schlüsselzelle derselben Zeile verrät die Verschiebung.
    cells = [change["cell"] for change in changes]
    key_cell = f"{col_letter(key_index)}{row_number}"
    try:
        readback = read_back(service, source["id"], tab, cells + [key_cell])
    except Exception as error:  # Rücklesen darf den Erfolg nicht verschlucken
        readback = {}
        payload["readback_error"] = gdocs._explain(error)

    mismatched = [
        change for change in changes
        if change["cell"] in readback
        and not same_value(readback[change["cell"]], change["new"])
    ]
    key_intact = key_cell in readback and same_value(readback.get(key_cell), row_key)
    payload.update({
        "written": True,
        "readback": readback,
        "verified": bool(readback) and not mismatched and key_intact,
    })
    audit.log(root, "sheet_update", sheet=source["name"], tab=tab,
              row=row_number, key=row_key,
              changes={c["column"]: [c["old"], c["new"]] for c in changes},
              verified=payload["verified"])
    if readback and not key_intact:
        payload["row_moved"] = True
        payload["warning"] = (
            f"Zeile verschoben: in {key_cell} steht jetzt "
            f"{readback.get(key_cell)!r} statt {row_key!r}. Der Wert ist an der "
            "falschen Zeile gelandet — im Sheet prüfen und von Hand korrigieren."
        )
    if mismatched:
        payload["warning"] = (
            "Zellen geschrieben, aber der Rücklesewert weicht ab: "
            + "; ".join(
                f"{c['cell']} erwartet {c['new']!r}, gelesen {readback.get(c['cell'])!r}"
                for c in mismatched
            )
            + ". Google kann Werte umformatieren (Checkbox -> TRUE, Formel -> Ergebnis)."
        )

    # --- Write-Through: Cache + FTS-Index sofort aktualisieren ---------------
    try:
        gdocs.sync(root, sources=[source], force=True, conn=conn)
        payload["resynced"] = True
    except Exception as error:
        payload["resynced"] = False
        payload["sync_error"] = str(error)

    return payload


def append(
    root,
    conn,
    tab: str,
    values: dict[str, Any],
    sheet: str | None = None,
    dry_run: bool = False,
    value_input: str = "USER_ENTERED",
    client_secret: str | None = None,
) -> dict:
    """Hängt eine neue Zeile an — Spalten über den Header, nie über Positionen."""
    source = resolve_spreadsheet(conn, sheet)
    gdocs.assert_writable(conn, source["id"])

    service = _service(client_secret)
    tabs = list_tabs(service, source["id"])
    if tab not in tabs:
        allowed = ", ".join(repr(name) for name in tabs)
        raise SheetError(f"Tab {tab!r} existiert nicht. Vorhandene Tabs: [{allowed}]")

    header_rows = service.spreadsheets().values().get(
        spreadsheetId=source["id"], range=f"{quote_tab(tab)}!1:1"
    ).execute().get("values", [])
    headers = headers_of(header_rows)
    if not headers:
        raise SheetError(f"Keine Header-Zeile in {tab!r} gefunden.")

    unknown = [name for name in values if not any(
        str(name).strip().casefold() == header.casefold() for header in headers
    )]
    if unknown:
        allowed = ", ".join(repr(header) for header in headers if header)
        raise SheetError(
            f"Unbekannte Spalten: {unknown}. Erlaubte Spalten: [{allowed}]"
        )

    canonical: dict[int, Any] = {}
    for name, value in values.items():
        canonical[resolve_column(headers, name)] = value
    ordered = [canonical.get(index, "") for index in range(len(headers))]

    payload: dict[str, Any] = {
        "spreadsheet": source["name"],
        "spreadsheet_id": source["id"],
        "tab": tab,
        "values": {headers[i]: v for i, v in canonical.items()},
        "dry_run": dry_run,
        "value_input": value_input,
    }
    if dry_run:
        return payload

    try:
        response = service.spreadsheets().values().append(
            spreadsheetId=source["id"],
            range=f"{quote_tab(tab)}!A1",
            valueInputOption=value_input,
            insertDataOption="INSERT_ROWS",
            body={"values": [ordered]},
        ).execute()
    except Exception as error:
        raise SheetError(gdocs._explain(error)) from error

    updated_range = (response.get("updates") or {}).get("updatedRange", "")
    payload["appended_range"] = updated_range

    cells = [
        f"{col_letter(index)}{_row_of(updated_range)}"
        for index in canonical
    ]
    try:
        readback = read_back(service, source["id"], tab, cells)
    except Exception as error:
        readback = {}
        payload["readback_error"] = gdocs._explain(error)
    payload["readback"] = readback
    payload["verified"] = bool(readback) and all(
        same_value(readback.get(cell), canonical[index])
        for index, cell in zip(canonical, cells)
    )
    audit.log(root, "sheet_append", sheet=source["name"], tab=tab,
              row=_row_of(updated_range), values=payload["values"],
              verified=payload["verified"])

    try:
        gdocs.sync(root, sources=[source], force=True, conn=conn)
        payload["resynced"] = True
    except Exception as error:
        payload["resynced"] = False
        payload["sync_error"] = str(error)

    return payload


def _row_of(a1_range: str) -> int:
    """Zeilennummer aus "'Tab'!A13:F13" ziehen."""
    match = re.search(r"[!:](\d+)", a1_range or "")
    return int(match.group(1)) if match else 0
