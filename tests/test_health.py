"""Health-Warnungen: still wenn alles gut, laut wenn eine Invariante bricht."""

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
    # Nicht raten, sondern die echte Liste: sie enthält auch die Read-Scopes.
    _token(tmp_path, sorted(gdocs.WRITE_SCOPES), monkeypatch)

    assert health.warnings(tmp_path) == []
    assert health.header(tmp_path) == ""


def test_loud_when_write_scopes_were_lost(tmp_path, monkeypatch):
    """Der Bug vom 04.11.: ein Leseaufruf löschte beim Refresh die Schreibrechte."""
    _log(tmp_path, [{"tool": "publish", "wo": 6, "verified": True}])
    _token(tmp_path, ["https://www.googleapis.com/auth/drive.readonly"], monkeypatch)

    found = health.warnings(tmp_path)

    assert any("Schreibrechte" in text for text in found)
    assert health.header(tmp_path).startswith("⚠️ [GDD-HEALTH]")


def test_read_only_token_without_history_is_not_an_error(tmp_path, monkeypatch):
    """Wer nie geschrieben hat, hat auch keine Rechte *verloren*."""
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
    """Ein vom Gate geblockter Write ist korrektes Verhalten, kein Defekt."""
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
    """Ein lokaler Markdown-Write braucht kein Google-Schreibtoken."""
    _log(tmp_path, [{"tool": "set_status", "wo": 1, "source": "local", "verified": True}])
    _token(tmp_path, ["https://www.googleapis.com/auth/drive.readonly"], monkeypatch)

    assert health.warnings(tmp_path) == []
