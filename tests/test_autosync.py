"""Auto-Sync darf nie den Tool-Call blockieren oder sprengen."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from atlas import gdocs
from atlas.indexer import open_index

FIXTURES = Path(__file__).parent / "fixtures" / "gdd"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "proj"
    docs = target / "gdd"
    docs.mkdir(parents=True)
    for source in FIXTURES.glob("*.md"):
        shutil.copy(source, docs / source.name)
    return target


def test_without_token_does_nothing(project: Path, monkeypatch):
    monkeypatch.setattr(gdocs, "TOKEN_PATH", project / "gibt-es-nicht.json")
    conn = open_index(project)
    assert gdocs.auto_sync(project, conn) is None


def test_without_known_sources_does_nothing(project: Path, monkeypatch):
    token = project / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gdocs, "TOKEN_PATH", token)
    conn = open_index(project)
    assert gdocs.auto_sync(project, conn) is None


def test_disabled_by_env(project: Path, monkeypatch):
    monkeypatch.setenv("GDD_AUTO_SYNC", "0")
    conn = open_index(project)
    assert gdocs.auto_sync(project, conn) is None


def test_failure_is_swallowed_and_recorded(project: Path, monkeypatch):
    token = project / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gdocs, "TOKEN_PATH", token)
    conn = open_index(project)
    conn.execute(
        "INSERT INTO sync_state(drive_id, name, mime) VALUES ('abc', 'Doc', 'application/vnd.google-apps.document')"
    )
    conn.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("kein Netz")

    monkeypatch.setattr(gdocs, "sync", boom)
    assert gdocs.auto_sync(project, conn) is None

    row = conn.execute("SELECT value FROM meta WHERE key='last_sync_error'").fetchone()
    assert row is not None and "kein Netz" in row["value"]


def test_cache_prevents_second_call(project: Path, monkeypatch):
    token = project / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gdocs, "TOKEN_PATH", token)
    conn = open_index(project)
    conn.execute(
        "INSERT INTO sync_state(drive_id, name, mime) VALUES ('abc', 'Doc', 'application/vnd.google-apps.document')"
    )
    conn.commit()

    calls = {"n": 0}

    def fake_sync(*args, **kwargs):
        calls["n"] += 1
        return {"written": [], "skipped": [], "errors": []}

    monkeypatch.setattr(gdocs, "sync", fake_sync)
    gdocs.auto_sync(project, conn, max_age_seconds=300)
    gdocs.auto_sync(project, conn, max_age_seconds=300)
    assert calls["n"] == 1
