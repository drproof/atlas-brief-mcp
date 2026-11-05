"""Tests für sheets.py — Primärschlüssel + Spaltenname statt Koordinaten.

Der Fake-Service bildet die Sheets-API so weit nach, dass die Logik echt läuft:
Lesen, Schreiben, Rücklesen, Anhängen. Kein Netz, keine Attrappen auf Logikebene.
"""

from __future__ import annotations

import json

import pytest

from atlas import gdocs, sheets


# --------------------------------------------------------------------------
# Fake-Sheets-API
# --------------------------------------------------------------------------

class _Request:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def _split(a1: str) -> tuple[str, str]:
    if "!" in a1:
        tab, cells = a1.split("!", 1)
    else:
        tab, cells = a1, ""
    return tab.strip("'").replace("''", "'"), cells


class FakeValues:
    def __init__(self, tabs: dict[str, list[list]]):
        self.tabs = tabs
        self.writes: list[tuple[str, str, object]] = []
        self.appends: list[tuple[str, list]] = []

    # -- lesen -------------------------------------------------------------
    def get(self, spreadsheetId=None, range=None, **kwargs):
        tab, cells = _split(range or "")
        rows = self.tabs.get(tab, [])
        if not cells:
            return _Request({"values": [list(row) for row in rows]})
        if ":" in cells and cells.replace(":", "").isdigit():
            first = int(cells.split(":")[0])
            row = rows[first - 1] if len(rows) >= first else []
            return _Request({"values": [list(row)] if row else []})
        column = "".join(ch for ch in cells if ch.isalpha())
        number = int("".join(ch for ch in cells if ch.isdigit()))
        index = sheets.col_index(column)
        row = rows[number - 1] if len(rows) >= number else []
        value = row[index] if len(row) > index else ""
        return _Request({"values": [[value]] if value != "" else []})

    def batchGet(self, spreadsheetId=None, ranges=None, valueRenderOption=None, **kwargs):
        return _Request({"valueRanges": [self.get(range=r).execute() for r in ranges or []]})

    # -- schreiben ---------------------------------------------------------
    def batchUpdate(self, spreadsheetId=None, body=None):
        for entry in (body or {}).get("data", []):
            tab, cells = _split(entry["range"])
            column = "".join(ch for ch in cells if ch.isalpha())
            number = int("".join(ch for ch in cells if ch.isdigit()))
            index = sheets.col_index(column)
            value = entry["values"][0][0]
            self.writes.append((tab, cells, value))
            rows = self.tabs.setdefault(tab, [])
            while len(rows) < number:
                rows.append([])
            row = rows[number - 1]
            while len(row) <= index:
                row.append("")
            row[index] = value
        return _Request({"responses": []})

    def append(self, spreadsheetId=None, range=None, valueInputOption=None,
               insertDataOption=None, body=None):
        tab, _ = _split(range or "")
        rows = self.tabs.setdefault(tab, [])
        values = (body or {}).get("values", [[]])[0]
        rows.append(list(values))
        self.appends.append((tab, list(values)))
        number = len(rows)
        last = sheets.col_letter(max(len(values) - 1, 0))
        return _Request({"updates": {"updatedRange": f"'{tab}'!A{number}:{last}{number}"}})


class FakeSpreadsheets:
    def __init__(self, values: FakeValues):
        self._values = values

    def get(self, spreadsheetId=None, includeGridData=None, **kwargs):
        tabs = [{"properties": {"title": title}} for title in self._values.tabs]
        return _Request({"sheets": tabs})

    def values(self):
        return self._values


class FakeService:
    def __init__(self, tabs: dict[str, list[list]]):
        self.values_api = FakeValues(tabs)

    def spreadsheets(self):
        return FakeSpreadsheets(self.values_api)


TABS = {
    "Units": [
        ["ID", "Name", "HP", "Armor", "Status"],
        ["monster_goblin", "Goblin", "100", "2", "⬜"],
        ["monster_orc", "Orc", "140", "4", "⬜"],
    ]
}


@pytest.fixture()
def service():
    return FakeService({tab: [list(row) for row in rows] for tab, rows in TABS.items()})


@pytest.fixture()
def wired(monkeypatch, service):
    """Verdrahtet sheets.update/append ohne Netz: bekannte Quelle, Fake-Service."""
    monkeypatch.setattr(gdocs, "known_sources", lambda conn: [
        {"id": "sheet-1", "name": "GOREQUEST Balancing", "mimeType": gdocs.SHEET_MIME}
    ])
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(sheets, "_service", lambda *args, **kwargs: service)
    return service


# --------------------------------------------------------------------------
# A1-Notation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("index,letters", [
    (0, "A"), (1, "B"), (25, "Z"), (26, "AA"), (27, "AB"), (51, "AZ"),
    (52, "BA"), (701, "ZZ"), (702, "AAA"),
])
def test_col_letter(index, letters):
    assert sheets.col_letter(index) == letters


def test_col_letter_roundtrip():
    for index in range(0, 800):
        assert sheets.col_index(sheets.col_letter(index)) == index


def test_quote_tab_escapes_apostrophe():
    assert sheets.quote_tab("Mein Blatt") == "'Mein Blatt'"
    assert sheets.quote_tab("Spieler's Daten") == "'Spieler''s Daten'"


# --------------------------------------------------------------------------
# Werte
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("45", 45), ("-3", -3), ("0", 0), ("007", "007"), ("1,5", 1.5),
    ("true", True), ("FALSE", False), ("✅", "✅"), ("Goblin", "Goblin"),
])
def test_parse_scalar(raw, expected):
    assert sheets.parse_scalar(raw) == expected


@pytest.mark.parametrize("left,right,equal", [
    (45, "45", True), ("1.50", 1.5, True), (True, "TRUE", True),
    ("✅", "✅", True), ("Goblin", "goblin", True), ("=SUM(A1)", "=SUM(A1)", True),
    (45, 46, False), ("TRUE", "✅", False),
])
def test_same_value(left, right, equal):
    assert sheets.same_value(left, right) is equal


# --------------------------------------------------------------------------
# Spalten und Zeilen finden
# --------------------------------------------------------------------------

def test_resolve_column_exact_and_casefold():
    headers = ["ID", "Name", "HP"]
    assert sheets.resolve_column(headers, "HP") == 2
    assert sheets.resolve_column(headers, "hp") == 2


def test_resolve_column_lists_allowed_headers():
    with pytest.raises(sheets.SheetError) as error:
        sheets.resolve_column(["ID", "Name", "HP"], "Health")
    message = str(error.value)
    assert "Erlaubte Spalten" in message
    assert "'HP'" in message
    assert "Health" in message


def test_resolve_column_rejects_duplicate_header():
    with pytest.raises(sheets.SheetError) as error:
        sheets.resolve_column(["ID", "HP", "HP"], "HP")
    assert "mehrfach" in str(error.value)


def test_find_row_returns_one_based_number():
    rows = TABS["Units"]
    number, headers, row, key_index = sheets.find_row(rows, "monster_goblin")
    assert number == 2          # Header ist Zeile 1
    assert headers[0] == "ID"
    assert row[1] == "Goblin"
    assert key_index == 0


def test_find_row_is_case_and_space_insensitive():
    number, _, _, _ = sheets.find_row(TABS["Units"], "  MONSTER_ORC ")
    assert number == 3


def test_find_row_uses_explicit_key_column():
    number, _, _, key_index = sheets.find_row(TABS["Units"], "Orc", key_column="Name")
    assert number == 3
    assert key_index == 1


def test_find_row_missing_key_shows_existing_keys():
    with pytest.raises(sheets.SheetError) as error:
        sheets.find_row(TABS["Units"], "monster_dragon")
    message = str(error.value)
    assert "monster_goblin" in message
    assert "monster_dragon" in message


def test_plan_changes_maps_to_correct_cell():
    rows = TABS["Units"]
    number, headers, row, _ = sheets.find_row(rows, "monster_goblin")
    changes = sheets.plan_changes(headers, row, {"HP": 45, "Status": "✅"}, number)

    assert changes[0]["cell"] == "C2"
    assert changes[0]["old"] == "100"
    assert changes[0]["new"] == 45
    assert changes[1]["cell"] == "E2"


# --------------------------------------------------------------------------
# update / append
# --------------------------------------------------------------------------

def test_update_writes_only_named_cells(wired, tmp_path):
    result = sheets.update(tmp_path, None, "Units", "monster_goblin",
                           {"HP": 45, "Status": "✅"})

    assert result["written"] is True
    assert result["verified"] is True
    assert result["resynced"] is True
    assert result["row_number"] == 2
    # genau zwei Zellen geschrieben, Nachbarzeile unberührt
    assert wired.values_api.writes == [("Units", "C2", 45), ("Units", "E2", "✅")]
    assert wired.values_api.tabs["Units"][2] == ["monster_orc", "Orc", "140", "4", "⬜"]


def test_update_unknown_column_writes_nothing(wired, tmp_path):
    with pytest.raises(sheets.SheetError) as error:
        sheets.update(tmp_path, None, "Units", "monster_goblin", {"Health": 45})
    assert "Erlaubte Spalten" in str(error.value)
    assert wired.values_api.writes == []


def test_update_unknown_key_writes_nothing(wired, tmp_path):
    with pytest.raises(sheets.SheetError):
        sheets.update(tmp_path, None, "Units", "monster_dragon", {"HP": 1})
    assert wired.values_api.writes == []


def test_update_unknown_tab_lists_tabs(wired, tmp_path):
    with pytest.raises(sheets.SheetError) as error:
        sheets.update(tmp_path, None, "Units_Alt", "monster_goblin", {"HP": 1})
    assert "'Units'" in str(error.value)


def test_update_dry_run_does_not_write(wired, tmp_path):
    result = sheets.update(tmp_path, None, "Units", "monster_goblin",
                           {"HP": 45}, dry_run=True)
    assert result["dry_run"] is True
    assert result["changes"]["HP"]["cell"] == "C2"
    assert wired.values_api.writes == []


def test_append_orders_values_by_header(wired, tmp_path):
    result = sheets.append(tmp_path, None, "Units", {"Name": "Ogre", "HP": 200})

    assert result["verified"] is True
    tab, values = wired.values_api.appends[0]
    assert tab == "Units"
    assert values == ["", "Ogre", 200, "", ""]


def test_append_unknown_column_lists_allowed(wired, tmp_path):
    with pytest.raises(sheets.SheetError) as error:
        sheets.append(tmp_path, None, "Units", {"Lebenspunkte": 10})
    assert "Erlaubte Spalten" in str(error.value)
    assert wired.values_api.appends == []


def test_resolve_spreadsheet_by_partial_name(wired):
    source = sheets.resolve_spreadsheet(None, "balancing")
    assert source["id"] == "sheet-1"


def test_resolve_spreadsheet_unknown_name_lists_known(wired):
    with pytest.raises(sheets.SheetError) as error:
        sheets.resolve_spreadsheet(None, "Balancing Matrix 2024")
    assert "GOREQUEST Balancing" in str(error.value)


# --------------------------------------------------------------------------
# Race: Zeile verschiebt sich zwischen Lesen und Schreiben
# --------------------------------------------------------------------------

class RaceValues(FakeValues):
    """Simuliert einen Menschen, der im Browser sortiert, während wir schreiben."""

    def batchUpdate(self, spreadsheetId=None, body=None):
        # Goblin und Orc tauschen die Plätze — unsere Zielzeile 2 ist jetzt Orc.
        self.tabs["Units"][1], self.tabs["Units"][2] = (
            self.tabs["Units"][2], self.tabs["Units"][1]
        )
        return super().batchUpdate(spreadsheetId=spreadsheetId, body=body)


def test_update_detects_row_moved_between_read_and_write(monkeypatch, tmp_path):
    service = FakeService({tab: [list(row) for row in rows] for tab, rows in TABS.items()})
    service.values_api.__class__ = RaceValues

    monkeypatch.setattr(gdocs, "known_sources", lambda conn: [
        {"id": "sheet-1", "name": "GOREQUEST Balancing", "mimeType": gdocs.SHEET_MIME}
    ])
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {})
    monkeypatch.setattr(sheets, "_service", lambda *args, **kwargs: service)

    result = sheets.update(tmp_path, None, "Units", "monster_goblin", {"HP": 45})

    # Der Schreibvorgang hat Zeile 2 getroffen — dort steht jetzt aber der Orc.
    assert result["written"] is True
    assert result["verified"] is False, "verschobene Zeile muss als nicht verifiziert gelten"
    assert result["row_moved"] is True
    assert "Zeile verschoben" in result["warning"]
    assert result["readback"]["A2"] != "monster_goblin"


def test_update_verifies_when_row_stays_put(wired, tmp_path):
    result = sheets.update(tmp_path, None, "Units", "monster_goblin", {"HP": 45})
    assert result["verified"] is True
    assert result["readback"]["A2"] == "monster_goblin"
    assert "row_moved" not in result


def test_update_writes_audit_log(wired, tmp_path):
    sheets.update(tmp_path, None, "Units", "monster_goblin", {"HP": 45})

    log = tmp_path / ".atlas" / "mutations.log"
    assert log.exists(), "Schreibvorgang muss protokolliert werden"

    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["tool"] == "sheet_update"
    assert entry["key"] == "monster_goblin"
    assert entry["row"] == 2
    assert entry["changes"]["HP"] == ["100", 45]
    assert entry["verified"] is True
    assert entry["ts"]
