import json

from atlas import search as search_mod


def test_where_finds_combat_section(conn):
    payload = json.loads(search_mod.where(conn, "kampfsystem"))
    assert payload["found"] is True
    assert payload["path"].endswith("01-kampfsystem.md")
    assert payload["anchor"].startswith("kampfsystem#")


def test_where_ignores_filler_words(conn):
    payload = json.loads(search_mod.where(conn, "Wo finde ich das Dialogtool?"))
    assert payload["found"] is True
    assert "dialog" in payload["anchor"].lower()


def test_where_returns_base_anchor_for_split_sections(conn):
    payload = json.loads(search_mod.where(conn, "reaktor energie hydraulik"))
    assert payload["found"] is True
    assert "~" not in payload["anchor"]


def test_where_reports_missing_topic(conn):
    payload = json.loads(search_mod.where(conn, "quantenphysik"))
    assert payload["found"] is False
    assert "hint" in payload


def test_search_respects_budget(conn):
    raw = search_mod.search(conn, "schaden deckung reaktor", k=10, max_chars=300)
    assert len(raw) <= 300
    assert json.loads(raw)["hits"]


def test_read_concatenates_all_parts(conn):
    found = json.loads(search_mod.where(conn, "reaktor energie"))
    payload = json.loads(search_mod.read(conn, found["anchor"], max_chars=500))
    assert payload["total_chars"] > 1500
    assert payload["truncated"] is True
    assert payload["next_offset"] == 500


def test_tasks_extracted_from_checkboxes(conn):
    payload = json.loads(search_mod.tasks(conn, status="open"))
    texts = [item["text"] for item in payload["tasks"]]
    assert any("Schadensformel" in text for text in texts)
    assert all(item["done"] is False for item in payload["tasks"])
    owner = [item for item in payload["tasks"] if "Schadensformel" in item["text"]][0]
    assert owner["owner"] == "design"
    assert owner["tag"] == "balance"


def test_outline_lists_documents(conn):
    payload = json.loads(search_mod.outline(conn))
    assert "01-kampfsystem.md" in payload["outline"]
    assert payload["docs"] >= 3


def test_links_returns_implements(conn):
    found = json.loads(search_mod.where(conn, "kampfsystem"))
    payload = json.loads(search_mod.links(conn, found["anchor"]))
    assert "combat_system.gd" in payload["implements"]


def _log_project(tmp_path, heading: str, count: int):
    """A document whose section contains N Work Order lines."""
    from atlas import indexer

    body = "\n".join(
        f"Work Order {i} — ✅ DONE (Eintrag {i} im Logbuch, Testfall)"
        for i in range(1, count + 1)
    )
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "05.md").write_text(
        f"# Kampagne\n\n## {heading}\n\n{body}\n", encoding="utf-8")
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)
    return conn


def _penalty_for(conn, heading: str) -> float:
    from atlas.search import _log_penalty

    rows = [
        {"doc_id": row[0], "heading": row[1]}
        for row in conn.execute(
            "SELECT DISTINCT doc_id, heading FROM sections WHERE heading = ?", (heading,)
        ).fetchall()
    ]
    assert rows, f"Abschnitt {heading!r} nicht im Index"
    return _log_penalty(conn, rows).get((rows[0]["doc_id"], heading), 1.0)


def test_five_work_orders_are_not_a_logbook(tmp_path):
    """A prototype plan with five entries must not be penalized."""
    conn = _log_project(tmp_path, "3. Prototyp-Meilenstein-Plan", 5)

    assert _penalty_for(conn, "3. Prototyp-Meilenstein-Plan") == 1.0


def test_twenty_work_orders_are_a_logbook(tmp_path):
    """At 16 or more entries it is a logbook — full penalty applies."""
    conn = _log_project(tmp_path, "Nachtrag", 20)

    assert _penalty_for(conn, "Nachtrag") == 0.25


def test_ten_work_orders_get_half_penalty(tmp_path):
    """Between 8 and 15 entries: half penalty, not full."""
    conn = _log_project(tmp_path, "Nachtrag", 10)

    assert _penalty_for(conn, "Nachtrag") == 0.5


def test_a_section_named_work_orders_is_still_a_logbook(tmp_path):
    """Regression: "Work Orders" is the typical heading name of a logbook.

    A heading boost for "work orders" would override the count rule —
    letting the logbook win every system query because the score penalty
    never takes effect.
    """
    conn = _log_project(tmp_path, "Work Orders", 20)

    assert _penalty_for(conn, "Work Orders") == 0.25


def test_a_section_named_status_is_still_a_logbook(tmp_path):
    conn = _log_project(tmp_path, "Status", 20)

    assert _penalty_for(conn, "Status") == 0.25
