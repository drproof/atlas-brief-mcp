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
SEPARATORS = ("—", "–", "-", ":")
# Both spellings: German (existing documents) and English (new).
_OLD_MARK_RE = re.compile(
    r"^(?:✅\s*(?:ERLEDIGT|DONE)|⬜\s*(?:OFFEN|OPEN)|☑\s*(?:ERLEDIGT|DONE)"
    r"|☐\s*(?:OFFEN|OPEN))\s*",
    re.IGNORECASE,
)
_TASK_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:[-*+]|\d+\.)[ \t]+)\[(?P<mark>[ xX])\](?P<rest>[ \t]+.*)$",
    re.MULTILINE,
)


def _line_re(wo: int) -> re.Pattern:
    separators = "|".join(re.escape(item) for item in SEPARATORS)
    return re.compile(
        rf"^(?P<prefix>[ \t>*_-]*Work Order\s*{int(wo)}\s*(?:{separators})\s*)(?P<rest>.*)$",
        re.MULTILINE | re.IGNORECASE,
    )


def targets(root: Path) -> list[Path]:
    """Return local documentation files that may contain work orders.

    Order: files managed by `atlas init` first, followed by all other Markdown
    files in the project. The list is kept small — only files that actually
    contain a work order line are modified.
    """
    from . import bootstrap
    from .config import discover

    seen: list[Path] = []
    managed = bootstrap.load_managed(root) or set()
    for rel in sorted(managed):
        path = root / rel
        if path.exists():
            seen.append(path)
    for path in discover(root):
        if path not in seen:
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

    mark = STATUS_MARK.get(status.lower())
    if mark is None:
        raise ValueError(f"Unknown status '{status}' (allowed: done, open)")

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
            "status": status.lower(),
            "source": "local",
            "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
        }

    payload = {
        "written": not dry_run,
        "changed": not dry_run,
        "dry_run": dry_run,
        "wo": int(wo),
        "status": status.lower(),
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

    audit.log(root, "set_status", wo=wo, status=status, source="local",
              path=payload["path"], verified=verified)
    return payload


def find_task_line(root: Path, task_query: str) -> tuple[Path, str, re.Match] | None:
    """Return the file, full text, and regex match for a Markdown checkbox task."""
    needle = (task_query or "").strip().casefold()
    if not needle:
        return None
    for path in targets(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _TASK_RE.finditer(text):
            if needle in match.group("rest").casefold():
                return path, text, match
    return None


def set_task_status(root: Path, conn, task: str, status: str,
                    dry_run: bool = False) -> dict:
    """Toggle a Markdown checkbox task (`- [ ]` <-> `- [x]`) in local documentation."""
    from . import audit, indexer

    norm = status.lower()
    if norm not in STATUS_MARK:
        raise ValueError(f"Unknown status '{status}' (allowed: done, open)")
    target_char = "x" if norm == "done" else " "

    found = find_task_line(root, task)
    if found is None:
        return {
            "written": False,
            "changed": False,
            "verified": False,
            "reason": f"Checkbox task matching '{task}' is not in any local documentation file.",
            "source": "local",
        }

    path, text, match = found
    rel_path = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    old_line = match.group(0)
    new_line = f"{match.group('prefix')}[{target_char}]{match.group('rest')}"
    if match.group("mark").lower() == target_char:
        return {
            "written": False,
            "changed": False,
            "unchanged": True,
            "verified": True,
            "task": task,
            "status": norm,
            "source": "local",
            "path": rel_path,
        }

    payload = {
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
    if dry_run:
        return payload

    _write_atomic(path, text[: match.start()] + new_line + text[match.end():])

    confirmed = find_task_line(root, task)
    verified = bool(confirmed and confirmed[2].group("mark").lower() == target_char)
    payload["verified"] = verified
    if not verified:
        payload["warning"] = "Checkbox was written, but could not be read back from the file."

    indexer.index_file(conn, root, path)
    conn.commit()

    audit.log(root, "set_task_status", task=task, status=status, source="local",
              path=rel_path, verified=verified)
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
    audit.log(root, "publish", wo=wo, source="local",
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
