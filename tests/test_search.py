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
