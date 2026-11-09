"""SQLite index: schema, incremental build, and staleness checks."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .chunker import chunk
from .config import CACHE_DIR, discover, index_path
from .slug import slugify

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS docs (
  id         TEXT PRIMARY KEY,
  path       TEXT NOT NULL UNIQUE,
  title      TEXT,
  status     TEXT,
  tags       TEXT,
  implements TEXT,
  mtime      REAL,
  size       INTEGER,
  sha1       TEXT,
  indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS sections (
  rowid      INTEGER PRIMARY KEY,
  anchor     TEXT NOT NULL UNIQUE,
  doc_id     TEXT NOT NULL,
  path       TEXT NOT NULL,
  heading    TEXT NOT NULL,
  level      INTEGER NOT NULL,
  breadcrumb TEXT,
  line_start INTEGER,
  line_end   INTEGER,
  body       TEXT NOT NULL,
  chars      INTEGER,
  part       INTEGER DEFAULT 1,
  parts      INTEGER DEFAULT 1,
  tags       TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
  heading, breadcrumb, body, tags,
  content='sections', content_rowid='rowid',
  tokenize="unicode61 remove_diacritics 2",
  prefix='2 3'
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_vocab USING fts5vocab(sections_fts, 'row');

CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
  INSERT INTO sections_fts(rowid, heading, breadcrumb, body, tags)
  VALUES (new.rowid, new.heading, new.breadcrumb, new.body, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
  INSERT INTO sections_fts(sections_fts, rowid, heading, breadcrumb, body, tags)
  VALUES ('delete', old.rowid, old.heading, old.breadcrumb, old.body, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS sections_au AFTER UPDATE ON sections BEGIN
  INSERT INTO sections_fts(sections_fts, rowid, heading, breadcrumb, body, tags)
  VALUES ('delete', old.rowid, old.heading, old.breadcrumb, old.body, old.tags);
  INSERT INTO sections_fts(rowid, heading, breadcrumb, body, tags)
  VALUES (new.rowid, new.heading, new.breadcrumb, new.body, new.tags);
END;

CREATE TABLE IF NOT EXISTS tasks (
  id     INTEGER PRIMARY KEY,
  anchor TEXT NOT NULL,
  path   TEXT,
  text   TEXT NOT NULL,
  done   INTEGER DEFAULT 0,
  owner  TEXT,
  tag    TEXT,
  line   INTEGER
);

CREATE TABLE IF NOT EXISTS sync_state (
  drive_id   TEXT PRIMARY KEY,
  name       TEXT,
  mime       TEXT,
  modified   TEXT,
  synced_at  TEXT,
  chars      INTEGER,
  cache_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_sections_doc ON sections(doc_id);
CREATE INDEX IF NOT EXISTS idx_sections_path ON sections(path);
CREATE INDEX IF NOT EXISTS idx_tasks_anchor ON tasks(anchor);
CREATE INDEX IF NOT EXISTS idx_tasks_path ON tasks(path);
"""

SCHEMA_VERSION = "3"

# Everything regenerable from the source files. Dropped and rebuilt on a
# schema version bump — Markdown remains the source of truth. `sync_state` is
# preserved so bound Google Docs/Sheets are not forgotten across rebuilds.
CACHE_TABLES = ("sections_fts", "sections", "tasks", "docs")


def _rebuild_cache(conn: sqlite3.Connection) -> None:
    """Drop and recreate the index when the schema changes.

    `CREATE TABLE IF NOT EXISTS` does not migrate anything: a new column is
    missing in an existing table, and the first write fails with
    `OperationalError`. Because the index is purely a cache, rebuilding is
    cheaper and cleaner than a migration chain.
    """
    for name in CACHE_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.executescript(SCHEMA)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    # Check the version first, then apply the schema: SCHEMA includes an index
    # on `tasks.path`, and `CREATE INDEX` fails on an old table lacking that
    # column. `CREATE TABLE IF NOT EXISTS` does not migrate existing tables.
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()

    if row is None:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,)
        )
    elif str(row["value"]) != SCHEMA_VERSION:
        # Schema changed: drop and rebuild cache. The next indexing pass
        # repopulates it — Markdown is the source of truth.
        _rebuild_cache(conn)
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,))
    else:
        conn.executescript(SCHEMA)
    conn.commit()


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_doc_id(conn: sqlite3.Connection, wanted: str, rel: str) -> str:
    base = slugify(wanted)
    candidate = base
    counter = 2
    while True:
        row = conn.execute("SELECT path FROM docs WHERE id=?", (candidate,)).fetchone()
        if row is None or row["path"] == rel:
            return candidate
        candidate = f"{base}-{counter}"
        counter += 1


def index_file(conn: sqlite3.Connection, root: Path, path: Path) -> int:
    """Re-index a single file (idempotent). Returns the number of sections."""
    rel = path.relative_to(root).as_posix()
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    stat = path.stat()

    meta_probe = chunk(raw, slugify(path.stem), rel)
    wanted = str(meta_probe.meta.get("id") or path.stem)
    doc_id = _unique_doc_id(conn, wanted, rel)

    if doc_id != meta_probe.doc_id:
        meta_probe = chunk(raw, doc_id, rel)
    doc = meta_probe

    conn.execute("DELETE FROM sections WHERE path=?", (rel,))
    # Delete by path rather than by doc_id anchor: if `id:` in frontmatter
    # changes, the old anchor would otherwise remain orphaned forever.
    conn.execute("DELETE FROM tasks WHERE path=?", (rel,))
    conn.execute("DELETE FROM docs WHERE path=?", (rel,))
    conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))

    meta = doc.meta
    tags = meta.get("tags") or []
    implements = meta.get("implements") or []
    conn.execute(
        """
        INSERT INTO docs(id, path, title, status, tags, implements, mtime, size, sha1, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            rel,
            doc.title,
            str(meta.get("status") or ""),
            str(tags),
            str(implements),
            stat.st_mtime,
            stat.st_size,
            _sha1(path),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )

    for section in doc.sections:
        conn.execute(
            """
            INSERT INTO sections(anchor, doc_id, path, heading, level, breadcrumb,
                                 line_start, line_end, body, chars, part, parts, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                section.anchor, section.doc_id, section.path, section.heading,
                section.level, section.breadcrumb, section.line_start, section.line_end,
                section.body, len(section.body), section.part, section.parts,
                section.tags,
            ),
        )
        for line_no, text, done, owner, tag in _extract_tasks(section.body):
            conn.execute(
                "INSERT INTO tasks(anchor, path, text, done, owner, tag, line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (section.anchor, rel, text, done, owner, tag,
                 section.line_start + line_no),
            )
    return len(doc.sections)


def _extract_tasks(body: str):
    """Parse '- [ ] Text @owner #tag' -> (line_offset, text, done, owner, tag)."""
    import re

    pattern = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.+)$")
    owner_re = re.compile(r"@([A-Za-z0-9_.-]+)")
    tag_re = re.compile(r"(?<!\w)#([A-Za-z0-9_-]+)")
    for idx, line in enumerate(body.splitlines()):
        match = pattern.match(line)
        if not match:
            continue
        done = 1 if match.group(1).lower() == "x" else 0
        text = match.group(2).strip()
        owner_match = owner_re.search(text)
        tag_match = tag_re.search(text)
        yield (
            idx,
            owner_re.sub("", tag_re.sub("", text)).strip(),
            done,
            owner_match.group(1) if owner_match else "",
            tag_match.group(1) if tag_match else "",
        )


def refresh(conn: sqlite3.Connection, root: Path, force: bool = False) -> dict:
    """Incrementally sync the filesystem into the SQLite index."""
    files = discover(root)
    known = {
        row["path"]: row
        for row in conn.execute("SELECT path, mtime, size, sha1 FROM docs").fetchall()
    }

    indexed = 0
    unchanged = 0
    removed = 0
    seen: set[str] = set()

    for path in files:
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        try:
            stat = path.stat()
        except OSError:
            continue
        prior = known.get(rel)
        if (
            not force
            and prior is not None
            and abs((prior["mtime"] or 0) - stat.st_mtime) < 1e-6
            and prior["size"] == stat.st_size
        ):
            unchanged += 1
            continue
        if not force and prior is not None and prior["sha1"] == _sha1(path):
            conn.execute(
                "UPDATE docs SET mtime=?, size=? WHERE path=?", (stat.st_mtime, stat.st_size, rel)
            )
            unchanged += 1
            continue
        index_file(conn, root, path)
        indexed += 1

    for rel in known.keys() - seen:
        # Also remove tasks: otherwise ghost tasks from deleted files remain
        # in `atlas tasks` and in the open task count.
        conn.execute("DELETE FROM tasks WHERE path=?", (rel,))
        conn.execute("DELETE FROM docs WHERE path=?", (rel,))
        conn.execute("DELETE FROM sections WHERE path=?", (rel,))
        removed += 1

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('last_index', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (stamp,),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('root', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(root),),
    )
    conn.commit()

    return {
        "root": str(root),
        "files": len(files),
        "indexed": indexed,
        "unchanged": unchanged,
        "removed": removed,
        "sections": conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0],
        "docs": conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
        "at": stamp,
    }


def open_index(root: Path, create: bool = True) -> sqlite3.Connection:
    conn = connect(index_path(root))
    init(conn)
    if create:
        refresh(conn, root)
    return conn


def ensure_fresh(conn: sqlite3.Connection, root: Path) -> None:
    """Cheap staleness check: re-index only when files have changed."""
    docs = conn.execute("SELECT path, mtime, size FROM docs").fetchall()
    known = {row["path"]: (row["mtime"], row["size"]) for row in docs}
    files = discover(root)
    current = {}
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        current[path.relative_to(root).as_posix()] = (stat.st_mtime, stat.st_size)
    if current.keys() != known.keys():
        refresh(conn, root)
        return
    for rel, (mtime, size) in current.items():
        prior = known.get(rel)
        if prior is None or abs((prior[0] or 0) - mtime) > 1e-6 or prior[1] != size:
            refresh(conn, root)
            return


def get_sync_state(conn: sqlite3.Connection, drive_id: str):
    return conn.execute(
        "SELECT * FROM sync_state WHERE drive_id=?", (drive_id,)
    ).fetchone()


def invalidate_sync_state(conn: sqlite3.Connection, drive_id: str) -> None:
    """Clear the cached `modified` timestamp of a document without unbinding it.

    Needed when the Drive export lags behind a write: `sync` wrote the stale
    text into the cache, but recorded the new `modifiedTime` in `sync_state`.
    Resetting `modified` to `''` forces the next `auto_sync` to re-export the
    document while keeping `drive_id` registered in `known_sources(conn)`.
    """
    conn.execute("UPDATE sync_state SET modified = '' WHERE drive_id = ?", (drive_id,))
    conn.commit()


def set_sync_state(
    conn: sqlite3.Connection,
    drive_id: str,
    name: str,
    mime: str,
    modified: str,
    synced_at: str,
    chars: int,
    cache_path: str,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_state(drive_id, name, mime, modified, synced_at, chars, cache_path)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(drive_id) DO UPDATE SET
          name=excluded.name, mime=excluded.mime, modified=excluded.modified,
          synced_at=excluded.synced_at, chars=excluded.chars, cache_path=excluded.cache_path
        """,
        (drive_id, name, mime, modified, synced_at, chars, cache_path),
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict:
    meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
    synced = conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0]
    last_sync = conn.execute("SELECT MAX(synced_at) FROM sync_state").fetchone()[0] or ""
    return {
        "root": meta.get("root", ""),
        "last_index": meta.get("last_index", ""),
        "docs": conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
        "sections": conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0],
        "tasks_open": conn.execute("SELECT COUNT(*) FROM tasks WHERE done=0").fetchone()[0],
        "synced_gdocs": synced,
        "last_sync": last_sync,
        "last_sync_error": meta.get("last_sync_error", ""),
        "index_file": str(index_path(Path(meta.get("root") or "."))),
    }


__all__ = [
    "CACHE_DIR", "SCHEMA", "connect", "init", "refresh", "index_file",
    "open_index", "ensure_fresh", "stats", "get_sync_state", "set_sync_state",
]
