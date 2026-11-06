"""Local write path: check off Work Orders without a Google account.

The workflow described by `atlas init` ("status is a tool call, not a text
edit") previously ended in "No valid Google token". These tests lock in the
closed loop: init -> status -> set_status -> status.
"""

from __future__ import annotations

import json

from atlas import bootstrap, indexer, localwrite
from atlas import publish as publish_mod
from atlas import status as status_mod


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "engine.gd").write_text(
        "func start() -> void:\n\tpass\n", encoding="utf-8")
    bootstrap.init_project(root)
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)
    return root, conn


def test_local_set_status_closes_the_loop(tmp_path):
    root, conn = _project(tmp_path)
    doc = root / "docs" / "ARCHITECTURE.md"
    assert "⬜ OPEN" in doc.read_text(encoding="utf-8")

    payload = publish_mod.set_status(root, conn, 1, "done")

    assert payload["verified"] is True
    assert payload["source"] == "local"
    # Check the status line itself, not the whole document: the explanatory
    # text below mentions "⬜ OPEN" as a format example.
    found = localwrite.find_line(root, 1)
    assert found is not None
    assert "✅ DONE" in found[2].group(0)
    assert "⬜ OPEN" not in found[2].group(0)


def test_local_set_status_survives_index_rebuild(tmp_path):
    """After writing, `status` must read the updated state from disk."""
    root, conn = _project(tmp_path)
    publish_mod.set_status(root, conn, 1, "done")

    data = json.loads(status_mod.status(conn))

    assert data["counts"]["done"] == 1
    assert data["counts"]["open"] == 0


def test_local_set_status_is_idempotent(tmp_path):
    root, conn = _project(tmp_path)
    publish_mod.set_status(root, conn, 1, "done")

    second = publish_mod.set_status(root, conn, 1, "done")

    # The index is up to date, so the pre-check answers immediately without
    # a second write to the file.
    assert second.get("changed") is False
    assert second.get("written") is not True


def test_local_set_status_keeps_the_title(tmp_path):
    """Only the status marker changes; the title remains intact."""
    root, conn = _project(tmp_path)
    before = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    publish_mod.set_status(root, conn, 1, "done")

    after = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Complete the architecture doc" in before
    assert "Complete the architecture doc" in after


def test_local_publish_appends_a_new_work_order(tmp_path):
    root, conn = _project(tmp_path)

    payload = localwrite.publish(
        root, conn, 2,
        "Work Order 2 — ⬜ OPEN (Zweiter Schritt): Test-Eintrag",
        "3. Work Orders",
    )

    assert payload["verified"] is True
    found = localwrite.find_line(root, 2)
    assert found is not None
    assert "Zweiter Schritt" in found[2].group(0)


def test_local_publish_refuses_duplicates(tmp_path):
    root, conn = _project(tmp_path)
    localwrite.publish(root, conn, 2, "Work Order 2 — ⬜ OPEN (A)", "3. Work Orders")

    second = localwrite.publish(root, conn, 2, "Work Order 2 — ⬜ OPEN (B)", "3. Work Orders")

    assert second["written"] is False
    assert "already exists" in second["reason"]


def test_unknown_work_order_explains_the_next_step(tmp_path):
    root, conn = _project(tmp_path)

    payload = localwrite.set_status(root, conn, 99, "done")

    assert payload["verified"] is False
    assert payload["changed"] is False
    assert "atlas publish" in payload["reason"]


def test_local_set_status_carries_changed_and_verify_gate(tmp_path):
    root, conn = _project(tmp_path)

    payload = publish_mod.set_status(root, conn, 1, "done")

    assert payload["changed"] is True
    assert payload["written"] is True
    assert "verify" in payload


def test_legacy_german_marker_is_still_swappable(tmp_path):
    """Existing documents use `⬜ OFFEN` — status switching must recognize it."""
    root, conn = _project(tmp_path)
    doc = root / "docs" / "ARCHITECTURE.md"
    doc.write_text(
        doc.read_text(encoding="utf-8").replace("⬜ OPEN", "⬜ OFFEN"),
        encoding="utf-8",
    )
    indexer.index_file(conn, root, doc)
    conn.commit()

    payload = localwrite.set_status(root, conn, 1, "done")

    assert payload["verified"] is True
    line = localwrite.find_line(root, 1)[2].group(0)
    assert "✅ DONE" in line
    assert "OFFEN" not in line


def test_legacy_marker_survives_status_back_and_forth(tmp_path):
    """OFFEN -> DONE -> OPEN without losing the title."""
    root, conn = _project(tmp_path)

    localwrite.set_status(root, conn, 1, "done")
    localwrite.set_status(root, conn, 1, "open")

    line = localwrite.find_line(root, 1)[2].group(0)
    assert "⬜ OPEN" in line
    assert "Complete the architecture doc" in line
