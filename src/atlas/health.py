"""Surface silent degradation loudly — at zero token cost when healthy.

An MCP server has no screen for warnings. If the token loses its write scopes
on refresh, nobody notices: `atlas where` keeps answering normally, and only the
next `publish` fails — hours later, mid-task, with an error that sounds like a
formatting problem.

Therefore every tool response gets a single-line warning header **when** an
invariant is violated. When everything is fine, the header is empty and costs
nothing. Checks are strictly local: token file and audit log, no network, < 5 ms.
"""

from __future__ import annotations

from .config import cache_dir

import json
import os
from pathlib import Path


def _token_path() -> Path:
    """Return the token file: `~/.atlas/token.json`, or `~/.gdd/token.json` for legacy setups."""
    from . import gdocs

    return gdocs.global_dir() / "token.json"


def _token_scopes() -> set[str] | None:
    """Return scopes **stored** in the token file. None = no token present."""
    path = _token_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scopes = data.get("scopes") or []
    return {str(scope) for scope in scopes}


def _recent(root, limit: int = 20) -> list[dict]:
    """Return the latest audit entries — read from the tail without loading the full file."""
    path = cache_dir(root) / "mutations.log"
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = min(size, 8192)
            handle.seek(size - window)
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    lines = [line for line in chunk.splitlines() if line.strip().startswith("{")]
    entries: list[dict] = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _norm_wo(raw) -> int | str | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return str(raw)


def _entry_key(entry: dict) -> tuple:
    """Return the key by which a later success clears an earlier failure.

    Work order writes have a number; checklist step writes carry `task`; sheet
    writes carry sheet/tab/row keys so distinct targets never mask each other.
    """
    tool = str(entry.get("tool") or "write")
    wo_val = _norm_wo(entry.get("wo"))
    task_val = str(entry.get("task") or "").strip().lower()
    if task_val:
        return ("task", wo_val, task_val)
    if wo_val is not None:
        return ("wo", wo_val)
    return (
        tool,
        entry.get("sheet") or "",
        entry.get("tab") or "",
        entry.get("row") or entry.get("key") or "",
    )


def _entry_label(entry: dict) -> str:
    """Return a human-readable label — never 'WO None'."""
    tool = entry.get("tool") or "write"
    wo_val = _norm_wo(entry.get("wo"))
    task_raw = str(entry.get("task") or "").strip()
    if task_raw:
        short_task = task_raw[:40]
        if wo_val is not None:
            return f"{tool} WO {wo_val} · {short_task}"
        return f"{tool} {short_task}"
    if wo_val is not None:
        return f"{tool} WO {wo_val}"
    where = " · ".join(
        str(part) for part in (entry.get("tab"), entry.get("key") or entry.get("row"))
        if part
    )
    return f"{tool} {where}".strip()


def warnings(root) -> list[str]:
    """Return violated invariants. Empty list = all clear."""
    from . import gdocs

    found: list[str] = []
    recent = _recent(root)
    ever_wrote_sheets = any(
        entry.get("verified") is True
        and entry.get("source") != "local"
        and str(entry.get("tool") or "") in {"sheet_update", "sheet_append"}
        for entry in recent
    )
    ever_wrote_docs = any(
        entry.get("verified") is True
        and entry.get("source") != "local"
        and str(entry.get("tool") or "") not in {"sheet_update", "sheet_append"}
        for entry in recent
    )

    # 1. Write scopes lost even though a remote write succeeded previously.
    if ever_wrote_sheets or ever_wrote_docs:
        scopes = _token_scopes()
        if scopes is not None:
            required = (
                gdocs.WRITE_SCOPES
                if ever_wrote_docs
                else gdocs.SHEET_WRITE_SCOPES
            )
            missing = [scope for scope in required if scope not in scopes]
            if missing:
                short = ", ".join(scope.rsplit("/", 1)[-1] for scope in missing)
                auth_cmd = "atlas auth --docs-write" if ever_wrote_docs else "atlas auth"
                found.append(
                    f"Token has lost write permissions ({short}) — "
                    f"re-run `{auth_cmd}`, otherwise remote writes will fail."
                )

    # 2. A write remained unverified — and was not confirmed by a later retry.
    #    A `refused` entry is **not** an error: the gate blocked it as intended.
    #    Only report writes that were sent but not confirmed in the document.
    #    Once per work order, not repeated for every log line.
    if recent:
        resolved: set = set()
        pending: list[dict] = []
        for entry in reversed(recent):
            key = _entry_key(entry)
            if entry.get("verified") is True:
                resolved.add(key)
                continue
            # Also recognize older log entries: refusals ended with `_refused`
            # before `refused=True` was recorded explicitly.
            if entry.get("refused") or str(entry.get("tool", "")).endswith("_refused"):
                continue
            # Only real failures: entries without `verified` (e.g. a pure read)
            # are not unverified writes.
            if entry.get("verified") is not False:
                continue
            if key in resolved:
                continue
            resolved.add(key)
            pending.append(entry)
        if pending:
            names = ", ".join(_entry_label(entry) for entry in reversed(pending))
            found.append(
                f"Unverified write operation: {names} — line was not confirmed in the "
                "document (details: `atlas log`)."
            )

    return found


def header(root) -> str:
    """Return a single-line warning header or '' — never raises."""
    try:
        found = warnings(root)
    except Exception:  # diagnostics must never break a tool response
        return ""
    if not found:
        return ""
    return "⚠️ [GDD-HEALTH] " + " | ".join(found)
