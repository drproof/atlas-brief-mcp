"""Schema-Migration und Task-Lebenszyklus."""

from __future__ import annotations

import sqlite3

from atlas import indexer


def _project(tmp_path, name: str, text: str):
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / name).write_text(text, encoding="utf-8")
    return root


def test_schema_version_mismatch_rebuilds_cache(tmp_path):
    """Ein alter Index darf nach einer Schema-Aenderung nicht scheitern."""
    path = tmp_path / "index.sqlite"
    conn = indexer.connect(path)
    # Stand von Version 1 simulieren: tasks ohne path-Spalte
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE docs (id TEXT PRIMARY KEY, path TEXT);
        CREATE TABLE sections (id INTEGER PRIMARY KEY, doc_id TEXT, path TEXT, heading TEXT);
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, anchor TEXT, text TEXT, done INTEGER,
                            owner TEXT, tag TEXT, line INTEGER);
        """
    )
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
    conn.execute("INSERT INTO docs(id, path) VALUES('alt', 'docs/alt.md')")
    conn.commit()

    indexer.init(conn)

    version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert version == indexer.SCHEMA_VERSION
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "path" in columns, "tasks braucht die neue Spalte"
    # Cache ist geleert, Markdown bleibt die Wahrheit
    assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0


def test_deleted_file_leaves_no_ghost_tasks(tmp_path):
    """Beim Loeschen einer Datei verschwinden auch ihre Tasks."""
    root = _project(tmp_path, "01.md", "# Plan\n\n## Aufgaben\n\n- [ ] Erster Punkt\n")
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    (root / "docs" / "01.md").unlink()
    indexer.refresh(conn, root, force=True)

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0] == 0


def test_changed_frontmatter_id_clears_old_tasks(tmp_path):
    """Aendert sich die `id:`, darf der alte Task nicht stehenbleiben."""
    root = _project(tmp_path, "01.md",
                    "---\nid: erster\n---\n\n# Plan\n\n## Aufgaben\n\n- [ ] Punkt\n")
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    (root / "docs" / "01.md").write_text(
        "---\nid: zweiter\n---\n\n# Plan\n\n## Aufgaben\n\n- [ ] Punkt\n",
        encoding="utf-8",
    )
    indexer.refresh(conn, root, force=True)

    rows = conn.execute("SELECT anchor, path FROM tasks").fetchall()
    assert len(rows) == 1
    assert rows[0]["anchor"].startswith("zweiter#")
