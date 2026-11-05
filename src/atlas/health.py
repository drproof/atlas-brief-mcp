"""Stille Degeneration laut machen — im Normalfall zum Preis von null Tokens.

Ein MCP-Server hat keinen Bildschirm für Warnungen. Wenn das Token beim Refresh
seine Schreib-Scopes verliert, merkt das niemand: `atlas where` antwortet weiter
korrekt, und erst der nächste `publish` scheitert — Stunden später, mitten in
einer Aufgabe, mit einer Fehlermeldung, die nach Formatfehler klingt.

Deshalb hängt sich an jede Tool-Antwort ein einzeiliger Kopf, **wenn** eine
Invariante verletzt ist. Ist alles in Ordnung, ist der Kopf leer und kostet
nichts. Geprüft wird nur lokal: Token-Datei und Audit-Log, kein Netz, < 5 ms.
"""

from __future__ import annotations

from .config import cache_dir

import json
import os
from pathlib import Path


def _token_path() -> Path:
    """Token-Datei: `~/.atlas/token.json`, bei Altzprojekten `~/.gdd/token.json`."""
    from . import gdocs

    return gdocs.global_dir() / "token.json"


def _token_scopes() -> set[str] | None:
    """Scopes, die im Token **gespeichert** sind. None = kein Token vorhanden."""
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
    """Die letzten Audit-Einträge — rückwärts gelesen, nicht die ganze Datei."""
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


def _entry_key(entry: dict) -> tuple:
    """Woran ein späterer Erfolg erkennt, dass er diesen Fehlschlag aufhebt.

    Work-Order-Writes haben eine Nummer. Sheet-Writes haben keine — sie wurden
    früher alle unter `wo=None` einsortiert, sodass ein beliebiger späterer
    Sheet-Write die Warnung für eine verschobene Zeile stumm löschte.
    """
    tool = str(entry.get("tool") or "write")
    if entry.get("wo") is not None:
        return ("wo", entry["wo"])
    return (
        tool,
        entry.get("sheet") or "",
        entry.get("tab") or "",
        entry.get("row") or entry.get("key") or "",
    )


def _entry_label(entry: dict) -> str:
    """Lesbare Bezeichnung — nie 'WO None'."""
    tool = entry.get("tool") or "write"
    if entry.get("wo") is not None:
        return f"{tool} WO {entry['wo']}"
    where = " · ".join(
        str(part) for part in (entry.get("tab"), entry.get("key") or entry.get("row"))
        if part
    )
    return f"{tool} {where}".strip()


def warnings(root) -> list[str]:
    """Verletzte Invarianten. Leere Liste = alles in Ordnung."""
    from . import gdocs

    found: list[str] = []
    recent = _recent(root)
    ever_wrote = any(entry.get("verified") is True and entry.get("source") != "local" for entry in recent)

    # 1. Schreib-Scopes verloren, obwohl schon einmal geschrieben wurde.
    if ever_wrote:
        scopes = _token_scopes()
        if scopes is not None:
            missing = [scope for scope in gdocs.WRITE_SCOPES if scope not in scopes]
            if missing:
                short = ", ".join(scope.rsplit("/", 1)[-1] for scope in missing)
                found.append(
                    f"Token hat keine Schreibrechte mehr ({short}) — "
                    "`atlas auth` neu ausführen, sonst scheitert jeder Write."
                )

    # 2. Ein Write blieb unbestätigt — und wurde nicht später bestätigt.
    #    Ein `refused` ist **kein** Fehler: Da hat das Gate korrekt geblockt.
    #    Gemeldet wird nur, was geschrieben wurde und trotzdem nicht im Dokument
    #    steht. Pro Work Order einmal, nicht bei jedem Tool-Aufruf neu.
    if recent:
        resolved: set = set()
        pending: list[dict] = []
        for entry in reversed(recent):
            key = _entry_key(entry)
            if entry.get("verified") is True:
                resolved.add(key)
                continue
            # Auch ältere Log-Zeilen erkennen: Refusals enden auf `_refused`,
            # bevor `refused=True` mitgeschrieben wurde.
            if entry.get("refused") or str(entry.get("tool", "")).endswith("_refused"):
                continue
            # Nur echte Fehlschläge: Einträge ohne `verified` (z. B. ein reiner
            # Lesevorgang) sind keine unbestätigten Writes.
            if entry.get("verified") is not False:
                continue
            if key in resolved:
                continue
            resolved.add(key)
            pending.append(entry)
        if pending:
            names = ", ".join(_entry_label(entry) for entry in reversed(pending))
            found.append(
                f"Unbestätigter Schreibvorgang: {names} — Zeile steht nicht im "
                "Dokument (Details: `atlas log`)."
            )

    return found


def header(root) -> str:
    """Einzeiliger Warnkopf oder '' — niemals eine Exception."""
    try:
        found = warnings(root)
    except Exception:  # Diagnose darf die Tool-Antwort nie zerstören
        return ""
    if not found:
        return ""
    return "⚠️ [GDD-HEALTH] " + " | ".join(found)
