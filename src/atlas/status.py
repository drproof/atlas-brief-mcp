"""Progress overview: extract work order status from documentation text.

No new format required — agents already write "Work Order N — ✅ DONE".
This module turns those lines into a compact status summary for planning chats.
"""

from __future__ import annotations

import json
import re
import sqlite3

from datetime import datetime, timezone
from pathlib import Path

WO_RE = re.compile(r"Work\s*Order\s*(\d+)", re.IGNORECASE)
DONE_RE = re.compile(r"✅|☑|ERLEDIGT|ABGESCHLOSSEN|\bDONE\b", re.IGNORECASE)
OPEN_RE = re.compile(r"⬜|☐|🚧|OFFEN|OPEN|AUSSTEHEND|\bTODO\b|\bIN[\s_-]+PROGRESS\b|\[ \]", re.IGNORECASE)
SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})(?:\(\))?`")
SPEC_ANCHOR_RE = re.compile(r"`([a-z0-9][a-z0-9_-]*#[a-z0-9][a-z0-9_-]*)`", re.IGNORECASE)
STEP_LINE_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+\[(?P<mark>[ xX])\][ \t]+(?P<text>.+)$")


def _title(line: str, pos: int) -> str:
    rest = line[pos:]
    rest = DONE_RE.sub("", rest)
    rest = OPEN_RE.sub("", rest)
    rest = re.sub(r"^[\s*—–\-:]+", "", rest)
    match = re.search(r"\((.+?)\)", rest)
    if match:
        return match.group(1).strip()[:90]
    return rest.split("**")[0].strip(" *:")[:90]


_BOILERPLATE_WO1_TITLES = {
    "complete the architecture doc",
    "architekturdokument vervollständigen",
}


def _is_boilerplate_wo(entry: dict) -> bool:
    return (
        entry.get("wo") == 1
        and (entry.get("title") or "").strip().casefold() in _BOILERPLATE_WO1_TITLES
    )


def extract_work_orders(conn: sqlite3.Connection) -> list[dict]:
    """Find 'Work Order N' entries along with their status, spec anchor, steps, and symbols.

    Only lines with a status marker immediately after the number count — prose
    mentions like "API from Work Order 1" are not status declarations. Searches
    across all parts because long sections are split into parts 1..n.
    """
    rows = conn.execute(
        "SELECT anchor, heading, body FROM sections ORDER BY rowid"
    ).fetchall()

    items: dict[int, dict] = {}
    for row in rows:
        heading_line = (row["heading"] or "").strip()
        lines = ([heading_line] if heading_line else []) + row["body"].splitlines()
        for index, line in enumerate(lines):
            matches = list(WO_RE.finditer(line))
            if not matches:
                continue

            for m_idx, match in enumerate(matches):
                number = int(match.group(1))
                seg_end = matches[m_idx + 1].start() if m_idx + 1 < len(matches) else len(line)
                segment = line[match.start(): seg_end]
                rel_end = match.end() - match.start()

                # Status marker must immediately follow the work order number (or appear
                # before the colon `:` when a longer title precedes the marker).
                window = segment[rel_end: rel_end + 40]
                done = bool(DONE_RE.search(window))
                open_ = bool(OPEN_RE.search(window))
                if not (done or open_):
                    leading = line[: match.start()] if m_idx == 0 else ""
                    if m_idx > 0 or re.match(r"^[\s#*>\-_0-9.)]*$", leading):
                        pre_colon = segment[rel_end:].split(":", 1)[0][:120]
                        done = bool(DONE_RE.search(pre_colon))
                        open_ = bool(OPEN_RE.search(pre_colon))
                if not (done or open_):
                    continue

                status = "done" if done and not open_ else ("open" if open_ else "unknown")
                candidate_title = _title(segment, rel_end)

                # Collect lines belonging to this Work Order until the next Work Order or heading.
                block_lines: list[str] = [segment]
                steps: list[dict] = []
                if m_idx == len(matches) - 1:
                    for follow in lines[index + 1:]:
                        stripped = follow.strip()
                        if stripped.startswith("#") or WO_RE.search(follow):
                            break
                        block_lines.append(follow)
                        step_match = STEP_LINE_RE.match(follow)
                        if step_match:
                            steps.append({
                                "text": step_match.group("text").strip(),
                                "done": step_match.group("mark").lower() == "x",
                            })

                context = "\n".join(block_lines[:12])
                spec_match = SPEC_ANCHOR_RE.search(context)
                spec_anchor = spec_match.group(1) if spec_match else ""

                symbols: list[str] = []
                for symbol in SYMBOL_RE.findall(context):
                    if symbol not in symbols:
                        symbols.append(symbol)

                if number in items:
                    existing = items[number]
                    # A real Work Order 1 (e.g. from a synced Google Doc) always
                    # overrides the untouched `atlas init` skeleton placeholder.
                    if _is_boilerplate_wo(existing) and candidate_title.strip().casefold() not in _BOILERPLATE_WO1_TITLES:
                        pass
                    else:
                        # Merge checklist step completion across local docs/ARCHITECTURE.md
                        # and synced Google Docs so checking off a step locally is
                        # immediately reflected even before Drive export catches up.
                        if (
                            status == "done"
                            and (existing.get("title") or "").strip().casefold()
                            == candidate_title.strip().casefold()
                        ):
                            existing["status"] = "done"
                        if steps:
                            existing_steps = existing.get("steps") or []
                            if not existing_steps:
                                existing["steps"] = steps
                            else:
                                done_by_text = {
                                    s["text"].strip().casefold()
                                    for s in steps
                                    if s.get("done")
                                }
                                for es in existing_steps:
                                    if es["text"].strip().casefold() in done_by_text:
                                        es["done"] = True
                            merged_steps = existing.get("steps") or []
                            if merged_steps:
                                existing["steps_done"] = sum(1 for s in merged_steps if s["done"])
                                existing["steps_total"] = len(merged_steps)
                        if not existing.get("spec_anchor") and spec_anchor:
                            existing["spec_anchor"] = spec_anchor
                        continue

                entry: dict = {
                    "wo": number,
                    "status": status,
                    "title": candidate_title,
                    "symbols": symbols[:6],
                    "anchor": row["anchor"],
                    "heading": row["heading"],
                }
                if spec_anchor:
                    entry["spec_anchor"] = spec_anchor
                if steps:
                    entry["steps"] = steps
                    entry["steps_done"] = sum(1 for s in steps if s["done"])
                    entry["steps_total"] = len(steps)
                items[number] = entry
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
        try:
            from . import gdocs
            if gdocs.recover_sources_from_cache(conn):
                latest = conn.execute("SELECT MAX(synced_at) FROM sync_state").fetchone()
                last = (latest[0] if latest else "") or ""
        except Exception:
            pass
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
        steps = wo.get("steps") or []
        steps_done = wo.get("steps_done", 0)
        steps_total = wo.get("steps_total", 0)
        mark = marks[wo["status"]]
        if wo["status"] == "open" and 0 < steps_done < steps_total:
            mark = "🚧"
        extras: list[str] = []
        if wo.get("spec_anchor"):
            extras.append(f"spec: {wo['spec_anchor']}")
        if steps_total:
            extras.append(f"{steps_done}/{steps_total} steps")
        if wo.get("ready_to_close"):
            extras.append("💡 ready to close: all symbols wired")
        suffix = f"  [{', '.join(extras)}]" if extras else ""
        lines.append(f"  {mark} WO {wo['wo']:>2}  {wo['title']}{suffix}")
        for step in steps:
            box = "☑" if step["done"] else "☐"
            lines.append(f"        {box} {step['text']}")
    if payload.get("truncated"):
        lines.append(f"  … truncated ({payload.get('returned')} of "
                     f"{counts['total']}) — `--json` returns all.")
    unindexed = payload.get("unindexed_work_orders") or []
    if unindexed:
        lines.append("")
        lines.append("Recorded in audit log / wo-symbols.yml (not yet in document):")
        for uwo in unindexed:
            syms = ", ".join(uwo.get("symbols") or [])
            sym_note = f"  [symbols: {syms}]" if syms else ""
            title = uwo.get("title") or "(title not recorded in log)"
            lines.append(f"  ⚠️ WO {uwo['wo']:>2}  {title} ({uwo.get('status', 'done')}){sym_note}")
    lines.append("")
    lines.append(f"Open checkbox tasks: {payload['open_tasks']} · "
                 f"Documents: {payload['docs']}")
    if payload.get("freshness"):
        lines.append(payload["freshness"])
    return "\n".join(lines)


def _infer_root(conn: sqlite3.Connection) -> Path | None:
    """Resolve the project root from the SQLite connection path (`.atlas/index.sqlite`)."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            db_file = row[2] if len(row) > 2 else ""
            if db_file and db_file != ":memory:":
                p = Path(db_file)
                if p.parent.name in {".atlas", ".gdd"} and p.parent.parent.exists():
                    return p.parent.parent
    except Exception:
        pass
    return None


def _recover_unindexed_work_orders(proj_root: Path, indexed_wos: set[int]) -> list[dict]:
    """Find Work Orders recorded in `mutations.log` or `wo-symbols.yml` that are missing from the document index."""
    from . import audit, verify

    sym_map = verify.load_wo_symbols(proj_root)
    log_entries = audit.tail(proj_root, limit=0)

    by_wo: dict[int, dict] = {}
    for entry in log_entries:
        if entry.get("refused") or entry.get("verified") is False:
            continue
        raw_wo = entry.get("wo")
        if raw_wo is None:
            continue
        try:
            wo_num = int(raw_wo)
        except (TypeError, ValueError):
            continue
        if wo_num in indexed_wos:
            continue
        rec = by_wo.setdefault(wo_num, {"wo": wo_num, "status": "done", "title": "", "symbols": []})
        if entry.get("status"):
            rec["status"] = str(entry["status"]).lower()
        if entry.get("title"):
            rec["title"] = str(entry["title"]).strip()
        elif entry.get("line"):
            m = WO_RE.search(str(entry["line"]))
            if m:
                rec["title"] = _title(str(entry["line"]), m.end())

    for wo_num, syms in sym_map.items():
        if wo_num in indexed_wos:
            continue
        rec = by_wo.setdefault(wo_num, {"wo": wo_num, "status": "done", "title": "", "symbols": []})
        rec["symbols"] = list(dict.fromkeys(rec["symbols"] + syms))

    return [by_wo[k] for k in sorted(by_wo)]


def status(
    conn: sqlite3.Connection,
    limit: int = 30,
    max_chars: int = 8000,
    root: Path | None = None,
) -> str:
    work_orders = extract_work_orders(conn)
    proj_root = root or _infer_root(conn)
    unindexed: list[dict] = []
    if proj_root is not None:
        from . import verify

        sym_map = verify.load_wo_symbols(proj_root)
        for item in work_orders:
            if item["status"] != "open":
                continue
            recorded = sym_map.get(int(item["wo"]), [])
            if not recorded:
                continue
            steps_ok = item.get("steps_done", 0) == item.get("steps_total", 0)
            if steps_ok and verify.check(proj_root, recorded).get("ok"):
                item["ready_to_close"] = True
        unindexed = _recover_unindexed_work_orders(
            proj_root, {int(item["wo"]) for item in work_orders}
        )

    counts = {"done": 0, "open": 0, "unknown": 0}
    for item in work_orders:
        counts[item["status"]] += 1

    open_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE done = 0").fetchone()[0]
    fresh_note = freshness(conn)
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
        "freshness": fresh_note,
    }
    if unindexed:
        payload["unindexed_work_orders"] = unindexed

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
