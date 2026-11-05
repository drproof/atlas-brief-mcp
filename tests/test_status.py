import sqlite3

from atlas.status import extract_work_orders


def _conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sections(anchor TEXT, heading TEXT, body TEXT)")
    conn.executemany("INSERT INTO sections VALUES (?, ?, ?)", rows)
    return conn


def test_done_and_open_are_distinguished():
    conn = _conn([(
        "doc#plan", "Plan",
        "* **Work Order 1 — ✅ DONE (Core Unit, AP):** Text\n"
        "* **Work Order 2 — ⬜ OPEN (Dynamische Türen):** Text",
    )])
    items = extract_work_orders(conn)
    assert [item["wo"] for item in items] == [1, 2]
    assert items[0]["status"] == "done"
    assert items[0]["title"].startswith("Core Unit")
    assert items[1]["status"] == "open"


def test_mentions_without_status_are_ignored():
    conn = _conn([(
        "doc#api", "API",
        "Verbindliche API aus Work Order 3 (unit.gd, gore_manager.gd):",
    )])
    assert extract_work_orders(conn) == []


def test_split_sections_are_searched():
    """Lange Abschnitte liegen als part 1..n vor — Status kann in jedem Teil stehen."""
    conn = _conn([
        ("doc#a", "A", "Vorspann ohne Status"),
        ("doc#b", "B", "* **Work Order 7 — ✅ DONE (Späte Order):**"),
    ])
    items = extract_work_orders(conn)
    assert [item["wo"] for item in items] == [7]
