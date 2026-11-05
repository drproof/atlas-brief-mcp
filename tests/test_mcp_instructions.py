"""Die MCP-Instructions sind die einzige Regel, die ein frisch gestarteter Client
ohne geladene AGENTS.md sieht. Sie müssen die Schreib-Guardrails enthalten — und
dürfen keine Tools nennen, die es nicht gibt.

Letzteres ist kein theoretischer Fall: Ein Regelvorschlag aus der Praxis nannte
`atlas_check_task`, während das Tool `atlas_set_status` heißt. Ein Agent, der die
Regel wörtlich nimmt, ruft ein nicht existierendes Tool auf und improvisiert.
"""

from __future__ import annotations

import re

from atlas import mcp_server


def _instructions() -> str:
    return mcp_server.INSTRUCTIONS


def test_forbids_direct_cache_reads():
    assert ".atlas/cache" in _instructions()


def test_checkoff_only_after_green_tests():
    """The instruction that matters most: done is the last step, not the first."""
    text = _instructions().lower()
    assert "after tests are green" in text
    assert "last step" in text


def test_spec_is_declared_read_only():
    assert "read-only" in _instructions()


def test_names_all_write_tools():
    for tool in ("atlas_set_status", "atlas_publish", "atlas_sheet_update"):
        assert tool in _instructions(), f"{tool} fehlt in den Instructions"


def test_forbids_cell_coordinates():
    text = _instructions()
    assert "row_key" in text
    assert "c14" in text.lower()


def test_only_names_tools_that_exist():
    """Fängt erfundene Tool-Namen in den Instructions."""
    existing = {name for name in dir(mcp_server) if name.startswith("atlas_")}
    mentioned = set(re.findall(r"\batlas_[a-z_]+\b", _instructions()))
    unknown = mentioned - existing
    assert not unknown, f"Instructions nennen nicht existierende Tools: {sorted(unknown)}"
