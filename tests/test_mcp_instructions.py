"""Verify that MCP server instructions and tool schemas include all required guardrails and metadata."""

from __future__ import annotations

import asyncio
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
        assert tool in _instructions(), f"{tool} missing from instructions"


def test_forbids_cell_coordinates():
    text = _instructions()
    assert "row_key" in text
    assert "c14" in text.lower()


def test_only_names_tools_that_exist():
    """Catches invented tool names in the server instructions."""
    existing = {name for name in dir(mcp_server) if name.startswith("atlas_")}
    mentioned = set(re.findall(r"\batlas_[a-z_]+\b", _instructions()))
    unknown = mentioned - existing
    assert not unknown, f"Instructions reference non-existent tools: {sorted(unknown)}"


def test_all_tools_have_annotations_and_parameter_descriptions():
    """Every tool must expose MCP ToolAnnotations and descriptions for all parameters (TDQS)."""
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert len(tools) == 16
    for tool in tools:
        assert tool.annotations is not None, f"{tool.name} is missing ToolAnnotations"
        assert tool.annotations.title, f"{tool.name} is missing annotations.title"
        assert tool.annotations.readOnlyHint is not None, f"{tool.name} missing readOnlyHint"
        assert tool.annotations.destructiveHint is not None, f"{tool.name} missing destructiveHint"
        assert tool.annotations.idempotentHint is not None, f"{tool.name} missing idempotentHint"
        assert tool.annotations.openWorldHint is not None, f"{tool.name} missing openWorldHint"

        props = (tool.inputSchema or {}).get("properties", {})
        for param_name, param_schema in props.items():
            desc = param_schema.get("description", "")
            assert desc and len(desc) >= 20, (
                f"{tool.name}.{param_name} is missing a detailed description in inputSchema"
            )


def test_all_prompts_registered_and_callable():
    """Verify the 3 workflow MCP prompts are registered on the FastMCP server."""
    prompts = asyncio.run(mcp_server.mcp.list_prompts())
    names = {p.name for p in prompts}
    assert names == {"atlas_onboard", "atlas_task_brief", "atlas_pre_commit_audit"}
    for p in prompts:
        assert p.description and len(p.description) >= 20
        assert p.title

