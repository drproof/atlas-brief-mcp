"""Write to local Markdown documentation — without a Google account.

`atlas init` creates `docs/ARCHITECTURE.md` with `Work Order 1 — ⬜ OPEN`
and writes an `AGENTS.md` instructing the agent to update status via tool
calls. Without a Google token, that path previously hit a dead end ("status
line not found in any document") — making a new user's first impression an
error.

This module closes the loop: the same contract, the same verification, and
the same audit log — backed by local files instead of the Docs API.
"""

from __future__ import annotations

import re
from pathlib import Path

STATUS_MARK = {"done": "✅ DONE", "open": "⬜ OPEN"}
_STATUS_ALIASES = {
    "done": "done",
    "erledigt": "done",
    "abgeschlossen": "done",
    "open": "open",
    "offen": "open",
    "ausstehend": "open",
    "todo": "open",
    "in_progress": "open",
    "in progress": "open",
}
SEPARATORS = ("—", "–", "-", ":")
# Both spellings: German (existing documents) and English (new).
_OLD_MARK_RE = re.compile(
    r"^(?:(?:✅|☑)\s*(?:ERLEDIGT|ABGESCHLOSSEN|DONE)"
    r"|(?:⬜|☐)\s*(?:OFFEN|AUSSTEHEND|OPEN)"
    r"|🚧\s*(?:IN\s+PROGRESS|OFFEN|OPEN)?"
    r"|(?:✅|☑|⬜|☐|🚧))\s*",
    re.IGNORECASE,
)
_TASK_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:[-*+]|\d+\.)[ \t]+)\[(?P<mark>[ xX])\](?P<rest>[ \t]+.*)$",
    re.MULTILINE,
)
_ANY_WO_HEADER_RE = re.compile(
    r"^[ \t>*#_-]*Work\s*Order\s*(?P<wo>\d+)\s*(?:—|–|-|:)\s*(?P<tail>.*)$",
    re.IGNORECASE,
)


def _norm_status(status: str) -> str:
    norm = _STATUS_ALIASES.get((status or "").strip().lower(), "")
    if not norm:
        raise ValueError(f"Unknown status '{status}' (allowed: done, open)")
    return norm


def _line_re(wo: int) -> re.Pattern:
    separators = "|".join(re.escape(item) for item in SEPARATORS)
    return re.compile(
        rf"^(?P<prefix>[ \t>*_-]*Work Order\s*{int(wo)}\s*(?:{separators})\s*)(?P<rest>.*)$",
        re.MULTILINE | re.IGNORECASE,
    )


def _is_sync_cache_file(path: Path) -> bool:
    """Return True if `path` lives inside `.atlas/cache/` or `.gdd/cache/`."""
    from .config import CACHE_DIR, LEGACY_CACHE_DIR

    return (
        path.parent.name == "cache"
        and path.parent.parent.name in {CACHE_DIR, LEGACY_CACHE_DIR}
    )


def targets(root: Path) -> list[Path]:
    """Return local documentation files that may contain work orders.

    Order: files managed by `atlas init` first, followed by all other local
    Markdown files in the project (excluding `.atlas/cache/` and `.gdd/cache/`,
    which are overwritten on Google Docs sync).
    """
    from . import bootstrap
    from .config import discover

    seen: list[Path] = []
    managed = bootstrap.load_managed(root) or set()
    for rel in sorted(managed):
        path = root / rel
        if path.exists() and not _is_sync_cache_file(path):
            seen.append(path)
    for path in discover(root):
        if path not in seen and not _is_sync_cache_file(path):
            seen.append(path)
    return seen


def find_line(root: Path, wo: int) -> tuple[Path, str, re.Match] | None:
    """Return the file and regex match documenting this work order."""
    pattern = _line_re(wo)
    for path in targets(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = pattern.search(text)
        if match:
            return path, text, match
    return None


def _replace_mark(rest: str, mark: str) -> str:
    """Replace only the status marker, preserving the title."""
    if _OLD_MARK_RE.match(rest):
        return _OLD_MARK_RE.sub(f"{mark} ", rest, count=1).rstrip()
    return f"{mark} {rest}".strip() if rest else mark


def _write_atomic(path: Path, text: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def set_status(root: Path, conn, wo: int, status: str,
               dry_run: bool = False) -> dict:
    """Update the status of a work order in local documentation."""
    from . import audit, indexer

    norm = _norm_status(status)
    mark = STATUS_MARK[norm]

    found = find_line(root, wo)
    if found is None:
        return {
            "written": False,
            "changed": False,
            "verified": False,
            "reason": f"Work Order {wo} is not in any local documentation file. "
                      "Run `atlas publish` first or add the line manually.",
            "source": "local",
        }

    path, text, match = found
    old_line = match.group(0)
    new_line = match.group("prefix") + _replace_mark(match.group("rest"), mark)
    if new_line == old_line:
        return {
            "written": False,
            "changed": False,
            "unchanged": True,
            "verified": True,
            "wo": int(wo),
            "status": norm,
            "source": "local",
            "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
        }

    payload = {
        "written": not dry_run,
        "changed": not dry_run,
        "dry_run": dry_run,
        "wo": int(wo),
        "status": norm,
        "source": "local",
        "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
        "before": old_line.strip()[:120],
        "after": new_line.strip()[:120],
    }
    if dry_run:
        return payload

    _write_atomic(path, text[: match.start()] + new_line + text[match.end():])

    # Self-verification: read back from disk, not from memory.
    confirmed = find_line(root, wo)
    verified = bool(confirmed and mark in confirmed[2].group(0))
    payload["verified"] = verified
    if not verified:
        payload["warning"] = "Line was written, but could not be read back from the file."

    indexer.index_file(conn, root, path)
    conn.commit()

    audit.log(root, "set_status", wo=wo, status=norm, source="local",
              path=payload["path"], verified=verified)
    return payload


def _wo_context_map(text: str) -> list[tuple[int, int, int | None, bool]]:
    """Return `(start_offset, end_offset, wo_num, is_open_or_standalone)` line spans."""
    spans: list[tuple[int, int, int | None, bool]] = []
    current_wo: int | None = None
    current_open = True
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line_no_nl = raw_line.rstrip("\r\n")
        wm = _ANY_WO_HEADER_RE.match(line_no_nl)
        if wm:
            current_wo = int(wm.group("wo"))
            tail = wm.group("tail")[:36]
            is_done = bool(re.search(r"✅|☑|\bERLEDIGT\b|\bABGESCHLOSSEN\b|\bDONE\b", tail, re.IGNORECASE))
            current_open = not is_done
        elif line_no_nl.lstrip().startswith("#"):
            current_wo = None
            current_open = True
        end_offset = offset + len(raw_line)
        spans.append((offset, end_offset, current_wo, current_open))
        offset = end_offset
    return spans


def _enclosing_wo_for_offset(spans: list[tuple[int, int, int | None, bool]], pos: int) -> tuple[int | None, bool]:
    for start, end, wo_num, is_open in spans:
        if start <= pos < end:
            return wo_num, is_open
    return None, True


def _collect_open_task_hints(root: Path, wo: int | None = None, limit: int = 5) -> list[str]:
    hints: list[str] = []
    for path in targets(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        spans = _wo_context_map(text)
        for match in _TASK_RE.finditer(text):
            if match.group("mark").lower() == "x":
                continue
            enc_wo, enc_open = _enclosing_wo_for_offset(spans, match.start())
            if wo is not None and enc_wo != int(wo):
                continue
            if wo is None and not enc_open:
                continue
            step_txt = match.group("rest").strip()
            if step_txt and step_txt not in hints:
                hints.append(step_txt)
                if len(hints) >= limit:
                    return hints
    return hints


def find_task_line(
    root: Path,
    task_query: str,
    wo: int | None = None,
    target_mark: str | None = None,
) -> tuple[Path, str, re.Match] | None:
    """Return the file, full text, and regex match for a Markdown checkbox task.

    When `wo` is provided, restricts matching to steps inside Work Order `wo`.
    When `target_mark` is provided ('x' or ' '), prefers an untoggled match inside
    an open Work Order so duplicate steps like `- [ ] Add unit tests` never get
    stuck on an earlier completed Work Order.
    """
    needle = (task_query or "").strip().casefold()
    if not needle:
        return None
    candidates: list[tuple[int, int, Path, str, re.Match]] = []
    seq = 0
    for path in targets(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        spans = _wo_context_map(text)
        for match in _TASK_RE.finditer(text):
            if needle not in match.group("rest").casefold():
                continue
            enc_wo, enc_open = _enclosing_wo_for_offset(spans, match.start())
            if wo is not None and enc_wo != int(wo):
                continue
            cur_mark = match.group("mark").lower()
            if target_mark is None:
                return path, text, match
            needs_change = cur_mark != target_mark.lower()
            if needs_change and enc_open:
                prio = 0
            elif needs_change:
                prio = 1
            elif enc_open:
                prio = 2
            else:
                prio = 3
            candidates.append((prio, seq, path, text, match))
            seq += 1
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    _, _, best_path, best_text, best_match = candidates[0]
    return best_path, best_text, best_match


def set_task_status(root: Path, conn, task: str, status: str,
                    dry_run: bool = False, wo: int | None = None) -> dict:
    """Toggle a Markdown checkbox task (`- [ ]` <-> `- [x]`) in local documentation."""
    from . import audit, indexer

    norm = _norm_status(status)
    target_char = "x" if norm == "done" else " "

    found = find_task_line(root, task, wo=wo, target_mark=target_char)
    if found is None:
        hints = _collect_open_task_hints(root, wo=wo)
        scope_note = f" under Work Order {int(wo)}" if wo is not None else ""
        hint_note = (
            f" Open steps{scope_note}: " + "; ".join(f"'{h}'" for h in hints)
            if hints
            else ""
        )
        res_missing: dict = {
            "written": False,
            "changed": False,
            "verified": False,
            "reason": f"Checkbox task matching '{task}'{scope_note} is not in any local documentation file.{hint_note}",
            "source": "local",
        }
        if hints:
            res_missing["open_tasks_hint"] = hints
        if wo is not None:
            res_missing["wo"] = int(wo)
        return res_missing

    path, text, match = found
    effective_wo = (
        int(wo)
        if wo is not None
        else _enclosing_wo_for_offset(_wo_context_map(text), match.start())[0]
    )
    rel_path = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    old_line = match.group(0)
    new_line = f"{match.group('prefix')}[{target_char}]{match.group('rest')}"
    if match.group("mark").lower() == target_char:
        unchanged_res: dict = {
            "written": False,
            "changed": False,
            "unchanged": True,
            "verified": True,
            "task": task,
            "status": norm,
            "source": "local",
            "path": rel_path,
        }
        if effective_wo is not None:
            unchanged_res["wo"] = int(effective_wo)
        return unchanged_res

    payload: dict = {
        "written": not dry_run,
        "changed": not dry_run,
        "dry_run": dry_run,
        "task": task,
        "status": norm,
        "source": "local",
        "path": rel_path,
        "before": old_line.strip()[:120],
        "after": new_line.strip()[:120],
    }
    if effective_wo is not None:
        payload["wo"] = int(effective_wo)
    if dry_run:
        return payload

    _write_atomic(path, text[: match.start()] + new_line + text[match.end():])

    try:
        disk_text = path.read_text(encoding="utf-8")
        verified = disk_text[match.start(): match.start() + len(new_line)] == new_line
    except OSError:
        verified = False
    payload["verified"] = verified
    if not verified:
        payload["warning"] = "Checkbox was written, but could not be read back from the file."

    indexer.index_file(conn, root, path)
    conn.commit()

    log_kwargs: dict = {"task": task, "status": norm, "source": "local", "path": rel_path, "verified": verified}
    if effective_wo is not None:
        log_kwargs["wo"] = int(effective_wo)
    audit.log(root, "set_task_status", **log_kwargs)
    return payload


def publish(root: Path, conn, wo: int, line: str, section: str,
            dry_run: bool = False) -> dict:
    """Append a work order line to local documentation."""
    from . import audit, indexer

    if find_line(root, wo) is not None:
        return {
            "written": False,
            "unchanged": True,
            "reason": f"Work Order {wo} already exists — change its status instead of creating a duplicate.",
            "source": "local",
        }

    candidates = [path for path in targets(root)
                  if path.name.lower().startswith("architecture")
                  or "work order" in path.read_text(encoding="utf-8", errors="replace").lower()]
    if not candidates:
        candidates = targets(root)[:1]
    if not candidates:
        return {"written": False, "verified": False, "source": "local",
                "reason": "No local documentation file found — run `atlas init` first."}

    path = candidates[0]
    text = path.read_text(encoding="utf-8")
    new_text, inserted_at = _insert_into_section(text, section, line)

    payload = {
        "written": not dry_run,
        "dry_run": dry_run,
        "wo": int(wo),
        "source": "local",
        "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
        "line": line,
        "index": inserted_at,
    }
    if dry_run:
        return payload

    _write_atomic(path, new_text)

    confirmed = find_line(root, wo)
    first_line = line.strip().splitlines()[0] if line.strip() else ""
    verified = bool(
        confirmed
        and first_line in confirmed[2].group(0)
        and line.strip() in confirmed[1]
    )
    payload["verified"] = verified
    if not verified:
        payload["warning"] = "Line was written, but could not be read back from the file."

    indexer.index_file(conn, root, path)
    conn.commit()
    audit.log(root, "publish", wo=wo, line=first_line, source="local",
              path=payload["path"], verified=verified)
    return payload


def _insert_into_section(text: str, section: str, line: str) -> tuple[str, int]:
    """Append the line to the end of the target section (or end of file)."""
    lines = text.splitlines()
    start = None
    needle = section.strip().casefold()
    for index, current in enumerate(lines):
        stripped = current.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip().casefold()
        if needle and (needle in title or title in needle):
            start = index
            break

    if start is None:
        # Section not found: append at the end, preceded by a blank line.
        body = text.rstrip("\n")
        return f"{body}\n\n{line}\n", len(body) + 2

    # End of section: next heading of the same or higher level.
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped.startswith("#"):
            continue
        if len(stripped) - len(stripped.lstrip("#")) <= level:
            end = index
            break

    # Skip trailing blank lines at the end of the section, keeping one after.
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)
    return "\n".join(lines) + "\n", insert_at
