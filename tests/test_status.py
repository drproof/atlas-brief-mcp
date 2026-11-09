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
    """Long sections are stored as part 1..n — status may appear in any part."""
    conn = _conn([
        ("doc#a", "A", "Vorspann ohne Status"),
        ("doc#b", "B", "* **Work Order 7 — ✅ DONE (Späte Order):**"),
    ])
    items = extract_work_orders(conn)
    assert [item["wo"] for item in items] == [7]


def test_heading_styled_and_long_title_work_orders_are_extracted():
    """Work Orders exported as headings (e.g. HEADING_2 in Google Docs) or with
    long titles preceding the status marker must also be detected."""
    conn = _conn([
        (
            "doc#wo-18",
            "Work Order 18 — ✅ DONE (Heading-Styled Order)",
            "Body text under heading-styled work order.\n- [x] Step 1",
        ),
        (
            "doc#plan",
            "Plan",
            "Work Order 25 (Very Long Title Before The Status Marker Over Forty Chars) — ✅ DONE: Details.",
        ),
    ])
    items = extract_work_orders(conn)
    assert [item["wo"] for item in items] == [18, 25]
    assert items[0]["status"] == "done"
    assert items[0]["title"] == "Heading-Styled Order"
    assert items[0]["steps_done"] == 1
    assert items[1]["status"] == "done"


def test_freshness_recovers_from_legacy_gdd_cache(tmp_path):
    from atlas.indexer import open_index
    from atlas.status import freshness

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "SPEC.md").write_text("# Spec\n\nText.\n", encoding="utf-8")
    conn = open_index(tmp_path)

    legacy_cache = tmp_path / ".gdd" / "cache"
    legacy_cache.mkdir(parents=True)
    (legacy_cache / "gdd.md").write_text(
        "---\n"
        "id: gdd\n"
        "title: Game GDD\n"
        "source: gdrive\n"
        "kind: doc\n"
        "drive_id: gdoc-legacy-99\n"
        "synced_at: 2026-11-09T16:00:00+00:00\n"
        "---\n\n# GDD\n\nBody.\n",
        encoding="utf-8",
    )

    note = freshness(conn)
    assert "no Google source synced" not in note
    assert "2026-11-09T16:00:00+00:00" in note

