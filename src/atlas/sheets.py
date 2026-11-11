"""Deterministic spreadsheet mutations: row key + column name -> A1 cell.

The agent passes `row_key="monster_goblin"` and `{"HP": 45}`. It **never** passes
`C14`. Cell coordinates (row, column, letter) are computed exclusively by Python.

Why: LLMs systematically miscount rows and columns in tables — header rows,
separator lines, 0- vs. 1-based indexing. An off-by-one write overwrites a
neighboring entry silently. Therefore raw cell coordinates are not accepted.

Three rules enforced by this module:
1. **Whitelist** — only spreadsheets registered in `sync_state` are writable.
2. **Header resolution** — columns are looked up by header name, never by index.
   Unknown column -> error *with the list of valid headers*, so the agent can
   self-correct on the next call.
3. **Read-back** — after writing, the exact modified cells are read back.
   `verified` is the authoritative status, not "did not raise".
"""

from __future__ import annotations

import re
from typing import Any

from . import audit, gdocs

SHEET_MIME = gdocs.SHEET_MIME


class SheetError(ValueError):
    """Error with an actionable instruction — returned directly in tool output."""


# --------------------------------------------------------------------------
# A1 notation
# --------------------------------------------------------------------------

def col_letter(index: int) -> str:
    """Convert a 0-based column index to A1 letters (0 -> A, 25 -> Z, 26 -> AA)."""
    if index < 0:
        raise ValueError(f"Column index must not be negative: {index}")
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def col_index(letters: str) -> int:
    """Inverse of col_letter — used in tests and reverse lookups."""
    value = 0
    for char in letters.strip().upper():
        if not char.isalpha():
            raise ValueError(f"Not a column letter: {letters!r}")
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def quote_tab(tab: str) -> str:
    """Quote a tab name for A1 ranges: 'My Sheet' -> \"'My Sheet'\" (apostrophes doubled)."""
    return "'" + str(tab).replace("'", "''") + "'"


# --------------------------------------------------------------------------
# Value normalization
# --------------------------------------------------------------------------

def norm_key(value: Any) -> str:
    """Normalize a key for case- and whitespace-insensitive comparison."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _norm_value(value: Any) -> str:
    """Normalize a cell value for read-back verification.

    Google reformats values under USER_ENTERED: "45" -> 45, "1.50" -> 1.5,
    checkbox -> TRUE. Normalize before comparing.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    text = str(value if value is not None else "").strip()
    if text.startswith("="):
        return text  # formula stays a formula
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text.casefold()
    return f"{number:g}"


def same_value(left: Any, right: Any) -> bool:
    return _norm_value(left) == _norm_value(right)


def parse_scalar(raw: str) -> Any:
    """Parse a CLI scalar -> int/float/bool/str. Leading zeros stay strings ('007')."""
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
# Source resolution
# --------------------------------------------------------------------------

def known_spreadsheets(conn) -> list[dict]:
    return [s for s in gdocs.known_sources(conn) if s["mimeType"] == SHEET_MIME]


def resolve_spreadsheet(conn, hint: str | None = None) -> dict:
    """Resolve the target spreadsheet: by ID, by name substring, or as the sole source."""
    sources = known_spreadsheets(conn)
    if not sources:
        raise SheetError(
            "No spreadsheet source known. Run `atlas sync --query <Name>` once "
            "to add it to the index."
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
            raise SheetError(f"No spreadsheet matches '{hint}'. Known: {names}")
        names = ", ".join(f"'{s['name']}'" for s in matches)
        raise SheetError(f"Ambiguous: {names}. Provide the full name or ID.")

    if len(sources) == 1:
        return sources[0]
    names = ", ".join(f"'{s['name']}'" for s in sources)
    raise SheetError(f"Multiple spreadsheets known — specify 'sheet': {names}")


def list_tabs(service, spreadsheet_id: str) -> list[str]:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, includeGridData=False
    ).execute()
    return [
        sheet.get("properties", {}).get("title", "")
        for sheet in meta.get("sheets", [])
    ]


# --------------------------------------------------------------------------
# Read tab and locate row
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
    """Resolve column name -> 0-based index. Raises with allowed headers if unknown."""
    cleaned = str(name).strip()
    if cleaned in headers:
        index = headers.index(cleaned)
    else:
        lowered = [header.casefold() for header in headers]
        matches = [i for i, header in enumerate(lowered) if header == cleaned.casefold()]
        if not matches:
            allowed = ", ".join(repr(header) for header in headers if header)
            raise SheetError(
                f"Unknown column {cleaned!r}. Allowed columns: [{allowed}]"
            )
        if len(matches) > 1:
            raise SheetError(
                f"Column {cleaned!r} appears {len(matches)}x in the header — "
                "ambiguous, please rename it in the sheet."
            )
        index = matches[0]

    if headers.count(headers[index]) > 1:
        raise SheetError(
            f"Column name {headers[index]!r} appears multiple times in the header. "
            "A cell cannot be addressed unambiguously this way."
        )
    return index


def find_row(
    rows: list[list[str]], row_key: Any, key_column: str | None = None
) -> tuple[int, list[str], list[str], int]:
    """Locate a row by its key value.

    Returns: (1-based row number, headers, row values, key column index).
    `rows[1:]` starts at row 2 — the single place where row numbers are computed.
    """
    if not rows:
        raise SheetError("The sheet is empty — there is no header row.")

    headers = headers_of(rows)
    if key_column:
        key_index = resolve_column(headers, key_column)
    else:
        key_index = 0

    wanted = norm_key(row_key)
    if not wanted:
        raise SheetError("row_key is empty.")

    for offset, row in enumerate(rows[1:]):
        cell = row[key_index] if len(row) > key_index else ""
        if norm_key(cell) == wanted:
            return offset + 2, headers, list(row), key_index

    column_name = headers[key_index] if len(headers) > key_index else "column 1"
    sample = [
        str(row[key_index]) if len(row) > key_index else ""
        for row in rows[1:6]
    ]
    sample = [value for value in sample if value]
    hint = f" Existing keys (sample): {', '.join(sample)}" if sample else ""
    raise SheetError(
        f"No entry {row_key!r} found in column {column_name!r} of the sheet.{hint}"
    )


# --------------------------------------------------------------------------
# Plan changes
# --------------------------------------------------------------------------

def plan_changes(
    headers: list[str], row: list[str], updates: dict[str, Any], row_number: int
) -> list[dict]:
    """Map {column_name: new_value} to concrete cells with old/new values."""
    if not updates:
        raise SheetError("No changes provided (updates is empty).")

    changes: list[dict] = []
    seen: set[int] = set()
    for name, new_value in updates.items():
        index = resolve_column(headers, name)
        if index in seen:
            raise SheetError(f"Column {headers[index]!r} was specified twice.")
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
    """Read back the exact modified cells — using FORMULA render mode so formulas remain visible."""
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
# Public operations
# --------------------------------------------------------------------------

def _service(client_secret: str | None = None, write: bool = True):
    from googleapiclient.discovery import build

    wanted = gdocs.SHEET_WRITE_SCOPES if write else gdocs.READ_SCOPES
    creds = gdocs.load_credentials(
        interactive=False, client_secret=client_secret, scopes=wanted
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
    """Update named columns of a single row, then re-index."""
    source = resolve_spreadsheet(conn, sheet)
    gdocs.assert_writable(conn, source["id"])

    service = _service(client_secret)
    tabs = list_tabs(service, source["id"])
    if tab not in tabs:
        allowed = ", ".join(repr(name) for name in tabs)
        raise SheetError(f"Tab {tab!r} does not exist. Available tabs: [{allowed}]")

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

    # --- Self-verification: modified cells AND the key cell -----------------
    # Checking only the target cell is not enough. If the sheet is sorted or a
    # row is deleted in the browser between read and write, the value lands on
    # the wrong row — and reading back that coordinate still returns the written
    # value. Checking the key cell of the same row catches the shift.
    cells = [change["cell"] for change in changes]
    key_cell = f"{col_letter(key_index)}{row_number}"
    try:
        readback = read_back(service, source["id"], tab, cells + [key_cell])
    except Exception as error:  # read-back failure must not mask the write result
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
            f"Row moved: {key_cell} now contains "
            f"{readback.get(key_cell)!r} instead of {row_key!r}. The value landed "
            "on the wrong row — check the sheet and fix it manually."
        )
    if mismatched:
        payload["warning"] = (
            "Cells written, but readback value differs: "
            + "; ".join(
                f"{c['cell']} expected {c['new']!r}, read {readback.get(c['cell'])!r}"
                for c in mismatched
            )
            + ". Google may reformat values (checkbox -> TRUE, formula -> result)."
        )

    # --- Write-through: refresh cache and FTS index immediately -------------
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
    """Append a new row — mapping columns by header name, never by position."""
    source = resolve_spreadsheet(conn, sheet)
    gdocs.assert_writable(conn, source["id"])

    service = _service(client_secret)
    tabs = list_tabs(service, source["id"])
    if tab not in tabs:
        allowed = ", ".join(repr(name) for name in tabs)
        raise SheetError(f"Tab {tab!r} does not exist. Available tabs: [{allowed}]")

    header_rows = service.spreadsheets().values().get(
        spreadsheetId=source["id"], range=f"{quote_tab(tab)}!1:1"
    ).execute().get("values", [])
    headers = headers_of(header_rows)
    if not headers:
        raise SheetError(f"No header row found in {tab!r}.")

    unknown = [name for name in values if not any(
        str(name).strip().casefold() == header.casefold() for header in headers
    )]
    if unknown:
        allowed = ", ".join(repr(header) for header in headers if header)
        raise SheetError(
            f"Unknown columns: {unknown}. Allowed columns: [{allowed}]"
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
    """Extract the row number from an A1 range like \"'Tab'!A13:F13\"."""
    match = re.search(r"[!:](\d+)", a1_range or "")
    return int(match.group(1)) if match else 0
