"""Progress overview: extract work order status from documentation text.

No new format required — agents already write "Work Order N — ✅ DONE".
This module turns those lines into a compact status summary for planning chats.
"""

from __future__ import annotations

import json
import re
import sqlite3

from datetime import datetime, timezone

WO_RE = re.compile(r"Work\s*Order\s*(\d+)", re.IGNORECASE)
DONE_RE = re.compile(r"✅|☑|ERLEDIGT|ABGESCHLOSSEN|\bDONE\b", re.IGNORECASE)
OPEN_RE = re.compile(r"⬜|☐|OFFEN|OPEN|AUSSTEHEND|\bTODO\b|\[ \]", re.IGNORECASE)
SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})(?:\(\))?`")


def _title(line: str, pos: int) -> str:
    rest = line[pos:]
    rest = DONE_RE.sub("", rest)
    rest = OPEN_RE.sub("", rest)
    rest = re.sub(r"^[\s*—–\-:]+", "", rest)
    match = re.search(r"\((.+?)\)", rest)
    if match:
        return match.group(1).strip()[:90]
    return rest.split("**")[0].strip(" *:")[:90]


def extract_work_orders(conn: sqlite3.Connection) -> list[dict]:
    """Find 'Work Order N' entries along with their status and mentioned symbols.

    Only lines with a status marker immediately after the number count — prose
    mentions like "API from Work Order 1" are not status declarations. Searches
    across all parts because long sections are split into parts 1..n.
    """
    rows = conn.execute(
        "SELECT anchor, heading, body FROM sections ORDER BY rowid"
    ).fetchall()

    items: dict[int, dict] = {}
    for row in rows:
        lines = row["body"].splitlines()
        for index, line in enumerate(lines):
            match = WO_RE.search(line)
            if not match:
                continue
            number = int(match.group(1))
            if number in items:
                continue

            # Status marker must immediately follow the work order number.
            window = line[match.end(): match.end() + 40]
            done = bool(DONE_RE.search(window))
            open_ = bool(OPEN_RE.search(window))
            if not (done or open_):
                continue

            status = "done" if done and not open_ else ("open" if open_ else "unknown")

            # Symbols often appear in the lines immediately following the entry.
            context = "\n".join(lines[index: index + 8])
            symbols: list[str] = []
            for symbol in SYMBOL_RE.findall(context):
                if symbol not in symbols:
                    symbols.append(symbol)

            items[number] = {
                "wo": number,
                "status": status,
                "title": _title(line, match.end()),
                "symbols": symbols[:6],
                "anchor": row["anchor"],
                "heading": row["heading"],
            }
    return [items[key] for key in sorted(items)]


def freshness(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    """Return a one-line freshness note that makes redundant re-checking unnecessary.

    The most expensive failure mode in agent workflows is not a wrong answer,
    but a right answer that the agent does not trust: an agent that doubts
    `atlas_status` runs the CLI, inspects source files, and writes a custom
    script — 18 steps for 267 tokens. A timestamp line costs ten tokens.
    """
    latest = conn.execute("SELECT MAX(synced_at) FROM sync_state").fetchone()
    last = (latest[0] if latest else "") or ""
    if not last:
        return "As of: local files only (no Google source synced)."

    try:
        stamp = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return f"As of: last sync {last}."
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    age = max(0, int(((now or datetime.now(timezone.utc)) - stamp).total_seconds()))
    if age < 60:
        when = "just now"
    elif age < 3600:
        when = f"{age // 60} min ago"
    else:
        when = f"{age // 3600} h ago"

    return (
        f"As of: synced {when} ({last}). This is the source — a second "
        "call, the CLI, or inspecting the source code will not return anything newer."
    )


def status_text(payload: dict) -> str:
    """Render the status payload as readable text — what an agent would otherwise format itself."""
    counts = payload["counts"]
    marks = {"done": "✅", "open": "⬜", "unknown": "·"}
    lines = [
        f"Work Orders: {counts['total']} — {counts['done']} done, "
        f"{counts['open']} open, {counts['unknown']} unknown"
    ]
    for wo in payload["work_orders"]:
        lines.append(f"  {marks[wo['status']]} WO {wo['wo']:>2}  {wo['title']}")
    if payload.get("truncated"):
        lines.append(f"  … truncated ({payload.get('returned')} of "
                     f"{counts['total']}) — `--json` returns all.")
    lines.append("")
    lines.append(f"Open checkbox tasks: {payload['open_tasks']} · "
                 f"Documents: {payload['docs']}")
    if payload.get("freshness"):
        lines.append(payload["freshness"])
    return "\n".join(lines)


def status(conn: sqlite3.Connection, limit: int = 30, max_chars: int = 8000) -> str:
    work_orders = extract_work_orders(conn)
    counts = {"done": 0, "open": 0, "unknown": 0}
    for item in work_orders:
        counts[item["status"]] += 1

    open_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE done = 0").fetchone()[0]
    last_sync = conn.execute(
        "SELECT MAX(synced_at) FROM sync_state"
    ).fetchone()[0] or ""
    docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    payload = {
        "work_orders": work_orders[:limit],
        "counts": {"total": len(work_orders), **counts},
        "open_tasks": open_tasks,
        "docs": docs,
        "last_sync": last_sync,
        "freshness": freshness(conn),
    }

    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_chars:
        return text

    payload["work_orders"] = [
        {"wo": item["wo"], "status": item["status"], "title": item["title"]}
        for item in work_orders[:limit]
    ]
    payload["truncated"] = True
    payload["returned"] = len(payload["work_orders"])
    text = json.dumps(payload, ensure_ascii=False)
    while len(text) > max_chars and payload["work_orders"]:
        payload["work_orders"].pop()
        payload["returned"] = len(payload["work_orders"])
        text = json.dumps(payload, ensure_ascii=False)
    return text
