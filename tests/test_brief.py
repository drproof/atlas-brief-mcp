"""Tests for task brief generation: rules source, truncation, and closing block."""

from __future__ import annotations

from atlas import brief


def test_trim_rules_cuts_at_first_numbered_section():
    text = (
        "# Regeln\n\n"
        "## 0. GDD-Zugriff\n"
        "Wichtig: GDD nur über MCP.\n\n"
        "## 1. Timeouts\n- 90 Sekunden\n- alter Kram\n"
    )
    trimmed = brief._trim_rules(text)
    assert "GDD nur über MCP" in trimmed
    assert "Timeouts" not in trimmed


def test_trim_rules_keeps_flat_file_intact():
    """Without section 0 there is no GDD part — do not strip everything."""
    text = "# Regeln\n\n## 1. Timeouts\n- 90 Sekunden\n"
    trimmed = brief._trim_rules(text)
    assert "90 Sekunden" in trimmed


def test_trim_rules_limits_long_text():
    text = "# Regeln\n" + ("- Zeile mit Inhalt\n" * 400)
    trimmed = brief._trim_rules(text)
    assert len(trimmed) <= brief.RULES_LIMIT + 60
    assert "see AGENTS.md for further rules" in trimmed


def test_load_rules_prefers_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# AGENTS\nGDD über MCP.\n## 1. Rest\n", encoding="utf-8")
    (tmp_path / "rules.md").write_text("# ALT\nVeraltete Regeln.\n", encoding="utf-8")

    rules = brief._load_rules(tmp_path)
    assert "GDD über MCP" in rules
    assert "Veraltete" not in rules


def test_load_rules_falls_back_to_rules_md(tmp_path):
    (tmp_path / "rules.md").write_text("# ALT\nNur hier.\n", encoding="utf-8")
    assert "Nur hier" in brief._load_rules(tmp_path)


def test_brief_contains_publish_command_for_work_order(conn, tmp_path):
    text = brief.build(conn, tmp_path, "Kampfsystem", work_order=12)
    assert "atlas publish --wo 12" in text
    assert "verified: true" in text
    assert "Closing" in text


def test_brief_without_work_order_mentions_placeholder(conn, tmp_path):
    text = brief.build(conn, tmp_path, "Kampfsystem")
    assert "atlas publish --wo <N>" in text


def test_brief_auto_selects_next_open_work_order_with_steps_and_symbols(tmp_path):
    """`atlas next` / `brief.build(conn, root)` without goal loads the next open WO, its spec, steps, and symbol status."""
    from atlas import bootstrap, indexer, publish as publish_mod

    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "engine.gd").write_text(
        "func apply_burn() -> int:\n\treturn 5\n", encoding="utf-8"
    )
    bootstrap.init_project(root)
    (root / "docs" / "02-combat.md").write_text(
        "---\nimplements: [core/engine.gd]\n---\n\n"
        "# Combat\n\n## Burn Mechanics\n\nBurn ticks every turn in `core/engine.gd`.\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)

    # Mark WO 1 done and plan WO 2 with 2 steps and 2 target symbols
    publish_mod.set_status(root, conn, wo=1, status="done")
    publish_mod.plan(
        root,
        conn,
        "Burn Mechanics",
        steps=["Define apply_burn in engine.gd", "Call apply_burn in turn loop"],
        verify_symbols=["apply_burn", "tick_burn_loop"],
    )
    publish_mod.set_status(root, conn, status="done", task="Define apply_burn")

    # Call `brief.build` with NO goal and NO work_order -> picks WO 2 (in-progress)
    output = brief.build(conn, root)
    assert "# Task brief: Burn Mechanics" in output
    assert "02-combat#burn-mechanics" in output
    assert "Work Order 2 Checklist (1/2 steps done)" in output
    assert "👉 **Next open step:** `Call apply_burn in turn loop`" in output
    assert "- [x] Define apply_burn in engine.gd" in output
    assert "- [ ] Call apply_burn in turn loop" in output
    assert "`apply_burn` — defined in `core/engine.gd:1` (⚠️ 0 callers (unwired))" in output
    assert "`tick_burn_loop` — **not defined yet**" in output
    assert "atlas set-status --wo 2 --status done --verify-symbols apply_burn,tick_burn_loop" in output

    # Plan a second open Work Order (WO 3) and test `atlas next+` (`all_open=True`)
    publish_mod.plan(
        root,
        conn,
        "Burn Mechanics",
        steps=["Add burn resistance check"],
        verify_symbols=["check_burn_resist"],
    )
    queue_output = brief.build(conn, root, all_open=True)
    assert "# Work Order Queue (`atlas next+`): 2 open (WO 2, WO 3)" in queue_output
    assert "## 1. Work Order 2 — 🚧 IN PROGRESS: Burn Mechanics" in queue_output
    assert "## 2. Work Order 3 — ⬜ OPEN: Burn Mechanics" in queue_output
    assert "atlas set-status --wo 2 --status done --verify-symbols apply_burn,tick_burn_loop" in queue_output
    assert "atlas set-status --wo 3 --status done --verify-symbols check_burn_resist" in queue_output


