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
DONE_RE = re.compile(r"✅|☑|\bERLEDIGT\b|\bABGESCHLOSSEN\b|\bDONE\b", re.IGNORECASE)
OPEN_RE = re.compile(
    r"⬜|☐|🚧|\bOFFEN\b|\bOPEN\b|\bAUSSTEHEND\b|\bTODO\b|\bIN[\s_-]+PROGRESS\b|\[ \]",
    re.IGNORECASE,
)
_LEADING_STATUS_STRIP_RE = re.compile(
    r"^[\s*—–\-:]*(?:(?:✅|☑|⬜|☐|🚧|\[[ xX]\])\s*)?"
    r"(?:\b(?:ERLEDIGT|ABGESCHLOSSEN|DONE|OFFEN|OPEN|AUSSTEHEND|TODO|IN[\s_-]+PROGRESS)\b)?"
    r"[\s*—–\-:]*",
    re.IGNORECASE,
)
_TRAILING_STATUS_STRIP_RE = re.compile(
    r"[\s*—–\-:]*(?:(?:✅|☑|⬜|☐|🚧|\[[ xX]\])\s*)?"
    r"(?:\b(?:ERLEDIGT|ABGESCHLOSSEN|DONE|OFFEN|OPEN|AUSSTEHEND|TODO|IN[\s_-]+PROGRESS)\b)"
    r".*$",
    re.IGNORECASE,
)
SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})(?:\(\))?`")
SPEC_ANCHOR_RE = re.compile(r"`([a-z0-9][a-z0-9_-]*#[a-z0-9][a-z0-9_-]*)`", re.IGNORECASE)
STEP_LINE_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+\[(?P<mark>[ xX])\][ \t]+(?P<text>.+)$")


def _status_from_text(text: str) -> tuple[bool, bool]:
    """Return `(done, open_)` for a status-marker snippet."""
    return bool(DONE_RE.search(text)), bool(OPEN_RE.search(text))


def _resolve_status(segment: str, rel_end: int, line: str, match_start: int, m_idx: int) -> str | None:
    """Extract `'done'` or `'open'` from a Work Order header without matching words inside titles."""
    after_num = segment[rel_end:]
    pre_colon = after_num.split(":", 1)[0]

    # 1. Check status prefix BEFORE any parenthesized `(Title)` so words like
    #    `(Open World)` or `(Todo Cleanup)` inside a title never flip `✅ DONE` to `open`.
    before_paren = pre_colon.split("(", 1)[0][:45]
    done, open_ = _status_from_text(before_paren)
    if done or open_:
        if done and open_:
            d_m = DONE_RE.search(before_paren)
            o_m = OPEN_RE.search(before_paren)
            return "done" if (d_m and o_m and d_m.start() < o_m.start()) else "open"
        return "done" if done else "open"

    # 2. Support `Work Order 25 (Long Title) — ✅ DONE:` where `(Title)` precedes the status marker.
    leading = line[:match_start] if m_idx == 0 else ""
    if m_idx > 0 or re.match(r"^[\s#*>\-_0-9.)]*$", leading):
        outside_parens = re.sub(r"\([^)]*\)", " ", pre_colon)[:120]
        done, open_ = _status_from_text(outside_parens)
        if done or open_:
            if done and open_:
                d_m = DONE_RE.search(outside_parens)
                o_m = OPEN_RE.search(outside_parens)
                return "done" if (d_m and o_m and d_m.start() < o_m.start()) else "open"
            return "done" if done else "open"

    # 3. Fallback within immediate 40-char window (e.g. status inside brackets).
    window = after_num[:40]
    done, open_ = _status_from_text(window)
    if not (done or open_):
        return None
    return "done" if done and not open_ else "open"


def _title(line: str, pos: int) -> str:
    rest = line[pos:]
    stripped = _LEADING_STATUS_STRIP_RE.sub("", rest, count=1)
    # Case 1: Title is enclosed in leading parentheses `(Title)`
    paren_match = re.match(r"^\((.+?)\)", stripped)
    if paren_match:
        return paren_match.group(1).strip()[:90]
    # Case 2: Unparenthesized title before `:` or `**`
    cleaned = _TRAILING_STATUS_STRIP_RE.sub("", stripped.split(":", 1)[0]).strip(" *:—–-")
    if cleaned:
        return cleaned.split("**")[0].strip(" *:")[:90]
    after_colon = stripped.split(":", 1)[1] if ":" in stripped else stripped
    return after_colon.split("**")[0].strip(" *:")[:90]


_BOILERPLATE_WO1_TITLES = {
    "complete the architecture doc",
    "architekturdokument vervollständigen",
}


def _is_boilerplate_wo(entry: dict) -> bool:
    return (
        entry.get("wo") == 1
        and (entry.get("title") or "").strip().casefold() in _BOILERPLATE_WO1_TITLES
    )


def _line_declares_wo(line: str) -> bool:
    """Return True only when `line` declares a Work Order status header (ignoring prose WO references)."""
    if STEP_LINE_RE.match(line):
        return False
    matches = list(WO_RE.finditer(line))
    for idx, m in enumerate(matches):
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
        seg = line[m.start(): seg_end]
        if _resolve_status(seg, m.end() - m.start(), line, m.start(), idx) is not None:
            return True
    return False


def extract_work_orders(conn: sqlite3.Connection) -> list[dict]:
    """Find 'Work Order N' entries along with their status, spec anchor, steps, and symbols.

    Only lines with a status marker immediately after the number count — prose
    mentions like "API from Work Order 1" are not status declarations. Searches
    across all parts because long sections are split into parts 1..n.
    """
    rows = conn.execute(
        "SELECT anchor, heading, body FROM sections ORDER BY rowid"
    ).fetchall()

    # Reassemble multi-part sections (`anchor` + `anchor~2`, or `anchor~1` + `anchor~2`)
    # before scanning so a 1,500-char chunk boundary never separates a Work Order header
    # from its `- [ ]` checklist steps.
    grouped_rows: list[dict] = []
    by_base: dict[str, dict] = {}
    for row in rows:
        raw_anchor = row["anchor"] or ""
        base_anchor = raw_anchor.split("~")[0]
        if base_anchor and base_anchor in by_base:
            by_base[base_anchor]["body"] += "\n" + (row["body"] or "")
            continue
        entry_row = {
            "anchor": base_anchor or raw_anchor,
            "heading": row["heading"],
            "body": row["body"] or "",
        }
        if base_anchor:
            by_base[base_anchor] = entry_row
        grouped_rows.append(entry_row)

    items: dict[int, dict] = {}
    for row in grouped_rows:
        heading_line = (row["heading"] or "").strip()
        lines = ([heading_line] if heading_line else []) + row["body"].splitlines()
        for index, line in enumerate(lines):
            if STEP_LINE_RE.match(line):
                continue
            matches = list(WO_RE.finditer(line))
            if not matches:
                continue

            for m_idx, match in enumerate(matches):
                number = int(match.group(1))
                seg_end = matches[m_idx + 1].start() if m_idx + 1 < len(matches) else len(line)
                segment = line[match.start(): seg_end]
                rel_end = match.end() - match.start()

                status = _resolve_status(segment, rel_end, line, match.start(), m_idx)
                if status is None:
                    continue

                candidate_title = _title(segment, rel_end)

                # Collect lines belonging to this Work Order until the next Work Order header or heading.
                block_lines: list[str] = [segment]
                steps: list[dict] = []
                if m_idx == len(matches) - 1:
                    for follow in lines[index + 1:]:
                        stripped = follow.strip()
                        if stripped.startswith("#"):
                            break
                        step_match = STEP_LINE_RE.match(follow)
                        if step_match:
                            block_lines.append(follow)
                            steps.append({
                                "text": step_match.group("text").strip(),
                                "done": step_match.group("mark").lower() == "x",
                            })
                            continue
                        if _line_declares_wo(follow):
                            break
                        block_lines.append(follow)

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
    reconciled = payload.get("reconciled")
    if reconciled and reconciled.get("total_reconciled"):
        restored = reconciled.get("restored_from_audit") or []
        synced_local = reconciled.get("synced_to_local") or []
        synced_gdocs = reconciled.get("synced_to_gdocs") or []
        lines.append("")
        lines.append(
            f"✓ Self-healed ({reconciled['total_reconciled']} action(s)): "
            f"restored {len(restored)} from audit log, "
            f"synced {len(synced_local)} to local docs, "
            f"mirrored {len(synced_gdocs)} to Google Docs."
        )
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


def _fetch_git_subjects(proj_root: Path) -> list[str]:
    """Read recent commit subjects once so recovering N untitled Work Orders spawns at most one git subprocess."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "log", "--all", "--format=%s", "-n", "100"],
            cwd=str(proj_root),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


def _infer_title_from_git_or_symbols(
    proj_root: Path,
    wo_num: int,
    symbols: list[str],
    git_subjects: list[str] | None = None,
) -> str:
    """Recover a missing Work Order title from git commit messages or fall back to its verified symbols."""
    subjects = _fetch_git_subjects(proj_root) if git_subjects is None else git_subjects
    if subjects:
        wo_pat = re.compile(
            rf"\b(?:Work\s*Order|WO)[-\s#]*0*{int(wo_num)}\b[:\s—–\-]*(?P<rest>.+)",
            re.IGNORECASE,
        )
        for subject in subjects:
            m = wo_pat.search(subject)
            if m:
                cand = m.group("rest").strip(" ():—–-")
                if len(cand) >= 4:
                    return cand[:90]

    if symbols:
        return f"Implemented {', '.join(symbols[:3])}"[:90]
    return ""


def _norm_audit_status(raw: object, fallback: str = "done") -> str:
    s = str(raw or "").strip().lower()
    if s in {"done", "erledigt", "abgeschlossen"}:
        return "done"
    if s in {"open", "offen", "ausstehend", "todo", "in_progress", "in progress"}:
        return "open"
    return fallback


def _recover_unindexed_work_orders(proj_root: Path, indexed_wos: set[int]) -> list[dict]:
    """Find Work Orders recorded in `mutations.log` or `wo-symbols.yml` that are missing from the document index."""
    from . import audit, verify

    sym_map = verify.load_wo_symbols(proj_root)
    log_entries = audit.tail(proj_root, limit=0, only_wo=True)

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
            rec["status"] = _norm_audit_status(entry["status"], fallback=rec["status"])
        elif entry.get("new"):
            new_str = str(entry["new"])
            if DONE_RE.search(new_str):
                rec["status"] = "done"
            elif OPEN_RE.search(new_str):
                rec["status"] = "open"
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

    need_git = any(not rec.get("title") for rec in by_wo.values())
    git_subjects = _fetch_git_subjects(proj_root) if need_git else []
    for wo_num, rec in by_wo.items():
        if not rec.get("title"):
            inferred = _infer_title_from_git_or_symbols(
                proj_root, wo_num, rec.get("symbols") or [], git_subjects=git_subjects
            )
            if inferred:
                rec["title"] = inferred
                rec["title_inferred"] = True

    return [by_wo[k] for k in sorted(by_wo)]


def reconcile_work_orders(
    root: Path,
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> dict:
    """Self-heal Work Order state across `mutations.log`/`wo-symbols.yml`, local `docs/ARCHITECTURE.md`, and bound Google Docs."""
    from . import localwrite, publish as publish_mod

    indexed = extract_work_orders(conn)
    indexed_nums = {int(item["wo"]) for item in indexed}
    unindexed = _recover_unindexed_work_orders(root, indexed_nums)

    restored_from_audit: list[int] = []
    synced_to_local: list[int] = []
    synced_to_gdocs: list[int] = []

    # 1. Restore orphaned Work Orders from audit log / wo-symbols.yml into local docs and Google Docs
    for uwo in unindexed:
        wo_num = int(uwo["wo"])
        st = _norm_audit_status(uwo.get("status"), fallback="done")
        syms = uwo.get("symbols") or []
        title = (
            uwo.get("title")
            or (f"Implemented {', '.join(syms[:3])}" if syms else f"Work Order {wo_num}")
        )
        desc = (
            "Symbols: " + ", ".join(f"`{s}`" for s in syms)
            if syms
            else "Restored from project audit log."
        )
        line = publish_mod.format_line(wo_num, st, title, desc)
        if localwrite.find_line(root, wo_num) is None:
            res = localwrite.publish(
                root, conn, wo_num, line, publish_mod.DEFAULT_SECTION, dry_run=dry_run
            )
            if res.get("written") or (dry_run and res.get("line")):
                restored_from_audit.append(wo_num)
        if not dry_run:
            mirror = publish_mod._mirror_to_gdocs(
                root, conn, wo_num, line, publish_mod.DEFAULT_SECTION
            )
            if mirror.get("gdocs_synced"):
                if not mirror.get("gdocs_existing"):
                    synced_to_gdocs.append(wo_num)
                else:
                    st_mirror = publish_mod._mirror_status_to_gdocs(
                        root, conn, wo_num, st
                    )
                    if st_mirror.get("gdocs_status_changed"):
                        synced_to_gdocs.append(wo_num)

    # 2. Cross-sync indexed Work Orders between local docs and bound Google Docs
    for item in extract_work_orders(conn):
        if _is_boilerplate_wo(item):
            continue
        wo_num = int(item["wo"])
        st = _norm_audit_status(item.get("status"), fallback="open")
        title = item.get("title") or f"Work Order {wo_num}"
        syms = item.get("symbols") or []
        desc_parts: list[str] = []
        if item.get("spec_anchor"):
            desc_parts.append(f"Spec `{item['spec_anchor']}`")
        if syms:
            desc_parts.append("Symbols: " + ", ".join(f"`{s}`" for s in syms))
        header = publish_mod.format_line(
            wo_num,
            st,
            title,
            " · ".join(desc_parts) if desc_parts else "Synchronized Work Order.",
        )
        steps_list = item.get("steps") or []
        step_lines = [
            f"- [{'x' if s.get('done') else ' '}] {s['text']}"
            for s in steps_list
        ]
        block = "\n".join([header, *step_lines])

        found_local = localwrite.find_line(root, wo_num)
        if found_local is None:
            res = localwrite.publish(
                root, conn, wo_num, block, publish_mod.DEFAULT_SECTION, dry_run=dry_run
            )
            if res.get("written") or (dry_run and res.get("line")):
                synced_to_local.append(wo_num)
        elif not dry_run:
            local_changed = False
            st_res = localwrite.set_status(root, conn, wo_num, st, dry_run=False)
            if st_res.get("changed"):
                local_changed = True
            for s in steps_list:
                if s.get("done") and (s.get("text") or "").strip():
                    task_res = localwrite.set_task_status(
                        root, conn, s["text"], "done", dry_run=False, wo=wo_num
                    )
                    if task_res.get("changed"):
                        local_changed = True
            if local_changed and wo_num not in synced_to_local:
                synced_to_local.append(wo_num)

        if not dry_run:
            mirror = publish_mod._mirror_to_gdocs(
                root, conn, wo_num, block, publish_mod.DEFAULT_SECTION
            )
            if mirror.get("gdocs_synced"):
                if not mirror.get("gdocs_existing"):
                    if wo_num not in synced_to_gdocs:
                        synced_to_gdocs.append(wo_num)
                else:
                    gdoc_changed = False
                    st_mirror = publish_mod._mirror_status_to_gdocs(
                        root, conn, wo_num, st
                    )
                    if st_mirror.get("gdocs_status_changed"):
                        gdoc_changed = True
                    for s in steps_list:
                        if s.get("done") and (s.get("text") or "").strip():
                            t_mirror = publish_mod._mirror_task_to_gdocs(
                                root, conn, s["text"], "done", wo=wo_num
                            )
                            if t_mirror.get("gdocs_changed"):
                                gdoc_changed = True
                    if gdoc_changed and wo_num not in synced_to_gdocs:
                        synced_to_gdocs.append(wo_num)

    return {
        "dry_run": dry_run,
        "restored_from_audit": restored_from_audit,
        "synced_to_local": synced_to_local,
        "synced_to_gdocs": synced_to_gdocs,
        "total_reconciled": (
            len(restored_from_audit) + len(synced_to_local) + len(synced_to_gdocs)
        ),
    }


def status(
    conn: sqlite3.Connection,
    limit: int = 30,
    max_chars: int = 8000,
    root: Path | None = None,
    reconcile: bool = False,
) -> str:
    proj_root = root or _infer_root(conn)
    reconcile_report: dict | None = None
    if reconcile and proj_root is not None:
        reconcile_report = reconcile_work_orders(proj_root, conn, dry_run=False)

    work_orders = extract_work_orders(conn)
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
    if reconcile_report is not None:
        payload["reconciled"] = reconcile_report

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
