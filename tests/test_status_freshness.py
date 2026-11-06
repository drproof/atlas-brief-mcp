"""Status output: readable, complete, and with verifiable freshness."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from atlas import status as status_mod


def _conn(synced_at: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sync_state (drive_id TEXT PRIMARY KEY, name TEXT, mime TEXT,"
        " modified TEXT, synced_at TEXT, chars INTEGER, cache_path TEXT)"
    )
    conn.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE tasks (done INTEGER)")
    if synced_at:
        conn.execute(
            "INSERT INTO sync_state (drive_id, synced_at) VALUES ('d1', ?)", (synced_at,)
        )
    return conn


def test_freshness_without_google_source():
    text = status_mod.freshness(_conn())
    assert "local files only" in text


def test_freshness_just_now():
    now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
    conn = _conn((now - timedelta(seconds=20)).isoformat())
    text = status_mod.freshness(conn, now=now)

    assert "just now" in text
    assert "anything newer" in text


def test_freshness_minutes_and_hours():
    now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
    conn = _conn((now - timedelta(minutes=12)).isoformat())
    assert "12 min ago" in status_mod.freshness(conn, now=now)

    conn = _conn((now - timedelta(hours=3)).isoformat())
    assert "3 h ago" in status_mod.freshness(conn, now=now)


def test_status_text_is_readable_and_complete():
    conn = _conn(datetime.now(timezone.utc).isoformat())
    payload = {
        "work_orders": [
            {"wo": 14, "status": "done", "title": "SkillExecutor"},
            {"wo": 15, "status": "open", "title": "MonsterAI"},
        ],
        "counts": {"total": 2, "done": 1, "open": 1, "unknown": 0},
        "open_tasks": 0,
        "docs": 4,
        "last_sync": "",
        "freshness": status_mod.freshness(conn),
    }
    text = status_mod.status_text(payload)

    assert "Work Orders: 2 — 1 done, 1 open" in text
    assert "✅ WO 14" in text
    assert "⬜ WO 15" in text
    assert "Documents: 4" in text
    assert "As of:" in text
    # No JSON wrapper: the agent should not have to reformat the response.
    assert "{" not in text


def test_truncation_is_announced():
    payload = {
        "work_orders": [{"wo": 1, "status": "done", "title": "X"}],
        "counts": {"total": 60, "done": 60, "open": 0, "unknown": 0},
        "open_tasks": 0,
        "docs": 1,
        "truncated": True,
        "returned": 1,
    }
    text = status_mod.status_text(payload)

    assert "truncated" in text
    assert "1 of 60" in text
