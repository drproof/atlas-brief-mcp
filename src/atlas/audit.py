"""Append-only Protokoll aller Schreibzugriffe auf die Quelle.

Der Agent bekommt sein `verified: true` im Tool-Output — der Mensch will später
wissen, was über den Tag verteilt in den Google-Dokumenten passiert ist, ohne
sich durch die Google-Versionshistorie zu klicken.

Bewusst eine simple JSON-Zeile pro Vorgang: lesbar mit `grep`, `jq` oder
`atlas log`, und niemals von der Anwendung selbst verändert.
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
    """Hängt einen Vorgang an. Darf den Schreibvorgang nie scheitern lassen."""
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
        pass  # ein fehlendes Protokoll darf keinen Write verhindern
    return path


def tail(root, limit: int = 20) -> list[dict]:
    path = log_path(root)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in lines[-limit:]:
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
            # `publish` schreibt `document`, nicht `document_name` — sonst
            # zeigt `atlas log` bei Work-Order-Writes nie den Dokumentnamen.
            or entry.get("document_name")
            or entry.get("document")
            or ""
        )
        detail = entry.get("wo") or entry.get("row") or ""
        note = f"{target} {detail}".strip()
        rows.append(f"{stamp}  {tool:<14} {verdict:<18} {note[:70]}")
    return "\n".join(rows)
