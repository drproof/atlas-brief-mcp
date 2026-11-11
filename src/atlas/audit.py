"""Append-only log of all write operations against the source.

The agent gets its `verified: true` in the tool output — the human wants to know
later what happened in the Google documents over the course of the day without
clicking through Google's version history.

Deliberately a simple JSON line per operation: readable with `grep`, `jq`, or
`atlas log`, and never modified by the application itself.
"""

from __future__ import annotations

from .config import cache_dir

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_NAME = "mutations.log"


def log_path(root) -> Path:
    return cache_dir(root) / LOG_NAME


def log(root, tool: str, **fields) -> Path:
    """Append an operation entry. Must never cause the write operation to fail."""
    path = log_path(root)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "client": (
            os.environ.get("ATLAS_CLIENT")
            or os.environ.get("USERNAME")
            or os.environ.get("USER")
            or ""
        ),
    }
    entry.update(fields)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # a missing log must never block a write
    return path


def _tail_file_lines(path: Path, limit: int, only_wo: bool = False) -> list[str]:
    """Read up to the last `limit` lines of `path` without loading multi-MB logs into RAM."""
    if limit <= 0:
        out: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if only_wo and '"wo"' not in line:
                    continue
                stripped = line.rstrip("\r\n")
                if stripped:
                    out.append(stripped)
        return out
    size = path.stat().st_size
    if size <= 65536:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if only_wo:
            lines = [ln for ln in lines if '"wo"' in ln]
        return lines[-limit:]
    window = max(65536, limit * 1024)
    with path.open("rb") as fh:
        while True:
            offset = max(0, size - window)
            fh.seek(offset)
            chunk = fh.read(size - offset).decode("utf-8", errors="replace")
            lines = chunk.splitlines()
            if offset > 0 and lines:
                # Drop potentially truncated leading line at arbitrary byte boundary.
                lines = lines[1:]
            if only_wo:
                lines = [ln for ln in lines if '"wo"' in ln]
            if len(lines) >= limit or offset == 0:
                return lines[-limit:]
            window *= 2


def tail(root, limit: int = 20, only_wo: bool = False) -> list[dict]:
    from .config import CACHE_DIR, LEGACY_CACHE_DIR

    root_path = Path(root)
    paths = [
        p for p in (root_path / LEGACY_CACHE_DIR / LOG_NAME, root_path / CACHE_DIR / LOG_NAME)
        if p.exists()
    ]
    if not paths:
        return []
    raw_lines: list[str] = []
    for path in paths:
        try:
            raw_lines.extend(_tail_file_lines(path, limit, only_wo=only_wo))
        except OSError:
            continue
    entries: list[dict] = []
    for line in raw_lines[-limit:] if limit > 0 else raw_lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def render(root, limit: int = 20) -> str:
    entries = tail(root, limit)
    if not entries:
        return "_No write operations logged._"

    rows = []
    for entry in entries:
        stamp = str(entry.get("ts", ""))[:16].replace("T", " ")
        tool = str(entry.get("tool", "?"))
        verdict = "ok" if entry.get("verified") else "NOT VERIFIED"
        target = (
            entry.get("sheet")
            # `publish` writes `document`, not `document_name` — otherwise
            # `atlas log` never shows the document name for work order writes.
            or entry.get("document_name")
            or entry.get("document")
            or ""
        )
        detail = entry.get("wo") or entry.get("row") or ""
        note = f"{target} {detail}".strip()
        rows.append(f"{stamp}  {tool:<14} {verdict:<18} {note[:70]}")
    return "\n".join(rows)
