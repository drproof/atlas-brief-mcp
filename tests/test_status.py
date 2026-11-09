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


def test_long_single_newline_work_order_list_does_not_slice_wo_18_or_25():
    """Regression test: a 27-entry Work Order list separated by single newlines
    exceeds MAX_CHARS=1500 around WO 18 and 3000 around WO 25. `_split_body` must
    split on newlines rather than slicing a Work Order status line on a space."""
    from atlas.chunker import chunk

    lines = ["# Milestone Plan", ""]
    for i in range(1, 28):
        lines.append(
            f"* **Work Order {i} — ✅ ERLEDIGT (Feature Module Number {i} With Description):** "
            f"Implemented system component {i} with full test coverage."
        )
    doc = chunk("\n".join(lines), "gdd", "gdd.md")
    assert len(doc.sections) >= 2

    conn = _conn([(s.anchor, s.heading, s.body) for s in doc.sections])
    items = extract_work_orders(conn)
    found_numbers = [item["wo"] for item in items]
    assert found_numbers == list(range(1, 28))


def test_real_wo_1_overrides_architecture_boilerplate_wo_1():
    """`Work Order 1 — ⬜ OPEN (Complete the architecture doc)` from `atlas init`
    must never shadow the real Work Order 1 from a synced GDD."""
    conn = _conn([
        (
            "architecture#3-work-orders",
            "3. Work Orders",
            "Work Order 1 — ⬜ OPEN (Complete the architecture doc): fill in the TODO blocks.",
        ),
        (
            "gdd#work-orders",
            "Work Orders",
            "Work Order 1 — ✅ DONE (Core Unit, Tri-Color AP, Vor-HP-Rüstung & Fokus): Implemented.",
        ),
    ])
    items = extract_work_orders(conn)
    assert len(items) == 1
    assert items[0]["wo"] == 1
    assert items[0]["status"] == "done"
    assert items[0]["title"] == "Core Unit, Tri-Color AP, Vor-HP-Rüstung & Fokus"


def test_localwrite_targets_excludes_sync_cache_and_rescues_cached_wos(tmp_path):
    from atlas import gdocs, localwrite

    (tmp_path / "docs").mkdir()
    arch = tmp_path / "docs" / "ARCHITECTURE.md"
    arch.write_text("# Arch\n\n## 3. Work Orders\n", encoding="utf-8")

    cache_dir = tmp_path / ".atlas" / "cache"
    cache_dir.mkdir(parents=True)
    cached_file = cache_dir / "master-gdd.md"
    cached_file.write_text(
        "# GDD\n\nWork Order 19 — ✅ DONE (Heldenquests): Done.\n"
        "Work Order 20 — ✅ DONE (Camp-Dialoge): Done.\n",
        encoding="utf-8",
    )

    target_paths = localwrite.targets(tmp_path)
    assert cached_file not in target_paths
    assert arch in target_paths

    merged = gdocs._rescue_cached_work_orders(
        tmp_path,
        "master-gdd.md",
        "# GDD\n\nWork Order 1 — ✅ DONE (Core Unit): Done.\n",
    )
    assert "Work Order 19 — ✅ DONE (Heldenquests)" in merged
    assert "Work Order 20 — ✅ DONE (Camp-Dialoge)" in merged


def test_glued_work_orders_on_same_line_are_both_extracted():
    """If an earlier insert glued two Work Orders onto the same paragraph line,
    both must be extracted with their own number, status, and title."""
    conn = _conn([(
        "gdd#plan",
        "Prototyp-Meilenstein-Plan",
        "* **Work Order 17 — ✅ ERLEDIGT (Kampf-KI):** Details.Work Order 18 — ✅ ERLEDIGT (Glued Order 18): More details.",
    )])
    items = extract_work_orders(conn)
    assert [item["wo"] for item in items] == [17, 18]
    assert items[0]["title"] == "Kampf-KI"
    assert items[1]["title"] == "Glued Order 18"


def test_status_surfaces_unindexed_work_orders_from_audit_and_symbols(tmp_path):
    import json
    from atlas import audit, verify
    from atlas.indexer import open_index
    from atlas.status import status, status_text

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "SPEC.md").write_text(
        "# Plan\n\nWork Order 1 — ✅ DONE (Core Unit): Done.\n",
        encoding="utf-8",
    )
    conn = open_index(tmp_path)

    audit.log(tmp_path, "publish", wo=18, status="done", title="Recovered Title 18", verified=True)
    verify.record_wo_symbols(tmp_path, 18, ["some_symbol_18"])
    verify.record_wo_symbols(tmp_path, 25, ["some_symbol_25"])

    payload = json.loads(status(conn, root=tmp_path))
    unindexed = payload.get("unindexed_work_orders") or []
    assert [u["wo"] for u in unindexed] == [18, 25]
    assert unindexed[0]["title"] == "Recovered Title 18"
    assert unindexed[0]["symbols"] == ["some_symbol_18"]

    rendered = status_text(payload)
    assert "WO 18  Recovered Title 18" in rendered
    assert "some_symbol_18" in rendered
    assert "WO 25" in rendered



