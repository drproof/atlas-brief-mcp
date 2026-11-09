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


def test_checkbox_task_toggle_and_wiring_gate(tmp_path):
    """Markdown `- [ ]` tasks can be toggled via `set_status(task=...)` and respect the wiring gate."""
    root, conn = _project(tmp_path)
    doc = root / "docs" / "ARCHITECTURE.md"
    doc.write_text(
        doc.read_text(encoding="utf-8")
        + "\n## Tasks\n\n- [ ] Wire damage calculator @dev #combat\n",
        encoding="utf-8",
    )
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "combat.py").write_text(
        "def dead_damage_calc(x: int) -> int:\n"
        "    return x * 2\n",
        encoding="utf-8",
    )
    indexer.index_file(conn, root, doc)
    conn.commit()

    # 1. Refused when verify_symbols has 0 callers
    refused = publish_mod.set_status(
        root, conn, status="done", task="Wire damage calculator",
        verify_symbols=["dead_damage_calc"],
    )
    assert refused["changed"] is False
    assert refused["refused"] is True
    assert "- [ ] Wire damage calculator" in doc.read_text(encoding="utf-8")

    # 2. Wire up the symbol, then check off the task
    (root / "src" / "combat.py").write_text(
        "def dead_damage_calc(x: int) -> int:\n"
        "    return x * 2\n\n"
        "def run_turn():\n"
        "    return dead_damage_calc(5)\n",
        encoding="utf-8",
    )
    done = publish_mod.set_status(
        root, conn, status="done", task="Wire damage calculator",
        verify_symbols=["dead_damage_calc"],
    )
    assert done["changed"] is True
    assert done["verified"] is True
    assert "- [x] Wire damage calculator" in doc.read_text(encoding="utf-8")

    # 3. Reopen the task (`- [x]` -> `- [ ]`)
    reopened = publish_mod.set_status(
        root, conn, status="open", task="Wire damage calculator",
    )
    assert reopened["changed"] is True
    assert reopened["verified"] is True
    assert "- [ ] Wire damage calculator" in doc.read_text(encoding="utf-8")


def test_plan_work_order_auto_links_spec_and_enforces_steps(tmp_path):
    """`atlas plan` auto-links the spec section via search, tracks steps in status, and blocks DONE while steps are open."""
    root, conn = _project(tmp_path)
    spec_doc = root / "docs" / "02-combat.md"
    spec_doc.write_text(
        "---\nimplements: [core/engine.gd]\n---\n\n"
        "# Combat System\n\n"
        "## Damage Over Time\n\n"
        "Burn stacks deal 5 fire damage at the end of each turn in `core/engine.gd`.\n",
        encoding="utf-8",
    )
    indexer.refresh(conn, root, force=True)

    # 1. Plan a new Work Order without specifying WO number or anchor manually
    planned = publish_mod.plan(
        root,
        conn,
        "Damage Over Time",
        steps=["Implement apply_burn in engine.gd", "Call apply_burn in start()"],
        verify_symbols=["apply_burn"],
    )
    assert planned["verified"] is True
    assert planned["wo"] == 2
    assert planned["spec_anchor"] == "02-combat#damage-over-time"
    assert "core/engine.gd" in planned["implements"]

    # 2. Check status output shows spec_anchor and 0/2 steps
    status_data = json.loads(status_mod.status(conn))
    wo2 = next(item for item in status_data["work_orders"] if item["wo"] == 2)
    assert wo2["spec_anchor"] == "02-combat#damage-over-time"
    assert wo2["steps_done"] == 0
    assert wo2["steps_total"] == 2

    # 3. Attempting to close WO 2 while steps are still open is refused
    (root / "core" / "engine.gd").write_text(
        "func apply_burn() -> int:\n\treturn 5\n\n"
        "func start() -> void:\n\tapply_burn()\n",
        encoding="utf-8",
    )
    refused = publish_mod.set_status(root, conn, wo=2, status="done")
    assert refused["changed"] is False
    assert refused["refused"] is True
    assert len(refused["open_steps"]) == 2

    # 4. Check off first step -> status_text shows 🚧 1/2 steps
    publish_mod.set_status(root, conn, status="done", task="Implement apply_burn")
    mid_status = json.loads(status_mod.status(conn))
    rendered = status_mod.status_text(mid_status)
    assert "🚧 WO  2" in rendered
    assert "1/2 steps" in rendered

    # 5. Check off second step -> before closing, status reports `ready_to_close: True`
    publish_mod.set_status(root, conn, status="done", task="Call apply_burn")
    ready_data = json.loads(status_mod.status(conn))
    wo2_ready = next(item for item in ready_data["work_orders"] if item["wo"] == 2)
    assert wo2_ready.get("ready_to_close") is True
    assert "ready to close: all symbols wired" in status_mod.status_text(ready_data)

    # 6. Close WO 2 -> succeeds!
    closed = publish_mod.set_status(root, conn, wo=2, status="done")
    assert closed["changed"] is True
    assert closed["verified"] is True


