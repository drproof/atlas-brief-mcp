"""Health warnings: silent when everything is fine, loud when an invariant breaks."""

from __future__ import annotations

import json

from atlas import gdocs, health


def _log(root, entries: list[dict]) -> None:
    atlas_dir = root / ".atlas"
    atlas_dir.mkdir(parents=True, exist_ok=True)
    with (atlas_dir / "mutations.log").open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _token(tmp_path, scopes: list[str], monkeypatch) -> None:
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"scopes": scopes}), encoding="utf-8")
    monkeypatch.setattr(health, "_token_path", lambda: path)


def test_silent_when_nothing_is_wrong(tmp_path, monkeypatch):
    _log(tmp_path, [{"tool": "publish", "wo": 13, "verified": True}])
    # Use the actual scope list rather than guessing: it includes read scopes too.
    _token(tmp_path, sorted(gdocs.WRITE_SCOPES), monkeypatch)

    assert health.warnings(tmp_path) == []
    assert health.header(tmp_path) == ""


def test_loud_when_write_scopes_were_lost(tmp_path, monkeypatch):
    """The 2026-11-04 bug: a read call dropped write permissions during token refresh."""
    _log(tmp_path, [{"tool": "publish", "wo": 6, "verified": True}])
    _token(tmp_path, ["https://www.googleapis.com/auth/drive.readonly"], monkeypatch)

    found = health.warnings(tmp_path)

    assert any("write permissions" in text for text in found)
    assert health.header(tmp_path).startswith("⚠️ [GDD-HEALTH]")


def test_read_only_token_without_history_is_not_an_error(tmp_path, monkeypatch):
    """Someone who never wrote anything has not *lost* write permissions."""
    _log(tmp_path, [])
    _token(tmp_path, ["https://www.googleapis.com/auth/drive.readonly"], monkeypatch)

    assert health.warnings(tmp_path) == []


def test_unconfirmed_write_is_reported_once(tmp_path, monkeypatch):
    _log(tmp_path, [
        {"tool": "publish", "wo": 13, "written": True, "verified": False},
        {"tool": "publish", "wo": 13, "written": True, "verified": False},
    ])

    found = health.warnings(tmp_path)

    assert len(found) == 1
    assert "WO 13" in found[0]


def test_later_success_resolves_the_warning(tmp_path, monkeypatch):
    _log(tmp_path, [
        {"tool": "publish", "wo": 13, "written": True, "verified": False},
        {"tool": "publish", "wo": 13, "written": True, "verified": True},
    ])

    assert health.warnings(tmp_path) == []


def test_refusal_is_not_a_warning(tmp_path, monkeypatch):
    """A write blocked by the gate is expected behavior, not a failure."""
    _log(tmp_path, [
        {"tool": "set_status_refused", "wo": 8, "refused": True,
         "written": False, "verified": False},
    ])

    assert health.warnings(tmp_path) == []


def test_legacy_refusal_line_is_understood(tmp_path, monkeypatch):
    _log(tmp_path, [
        {"tool": "publish_refused", "wo": 13, "verified": False},
        {"tool": "publish", "wo": 13, "written": True, "verified": True},
    ])

    assert health.warnings(tmp_path) == []


def test_header_never_raises(tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(health, "warnings", boom)
    assert health.header(tmp_path) == ""


def test_local_write_does_not_demand_google_write_scopes(tmp_path, monkeypatch):
    """A local Markdown write does not require a Google write token."""
    _log(tmp_path, [{"tool": "set_status", "wo": 1, "source": "local", "verified": True}])
    _token(tmp_path, ["https://www.googleapis.com/auth/drive.readonly"], monkeypatch)

    assert health.warnings(tmp_path) == []


def test_sheet_write_only_requires_sheet_scopes_not_docs_write_scope(tmp_path, monkeypatch):
    """Default `atlas auth` grants Docs read-only + Sheets write (`SHEET_WRITE_SCOPES`).
    A `sheet_update` must NOT trigger a false warning about missing `documents` write scope."""
    _log(tmp_path, [{"tool": "sheet_update", "tab": "Units", "key": "goblin", "verified": True}])
    _token(tmp_path, sorted(gdocs.SHEET_WRITE_SCOPES), monkeypatch)
    assert health.warnings(tmp_path) == []

    # If spreadsheets write scope itself is lost, warn and point to `atlas auth` (without --docs-write)
    _token(tmp_path, sorted(gdocs.READ_SCOPES), monkeypatch)
    warns = health.warnings(tmp_path)
    assert len(warns) == 1
    assert "spreadsheets" in warns[0]
    assert "`atlas auth`" in warns[0]
