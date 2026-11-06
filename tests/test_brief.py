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
