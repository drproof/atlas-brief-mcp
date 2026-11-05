"""Schreiben in lokale Markdown-Doku — ohne Google-Konto.

`atlas init` legt eine `docs/ARCHITECTURE.md` mit `Work Order 1 — ⬜ OFFEN` an
und schreibt eine `AGENTS.md`, die den Agenten verpflichtet, den Status per
Tool-Call zu setzen. Ohne Google-Token lief genau dieser Pfad bisher in eine
Sackgasse („Statuszeile in keinem Dokument gefunden") — der erste Eindruck
eines neuen Nutzers war ein Fehler.

Dieses Modul schließt den Kreis: derselbe Vertrag, dieselbe Verifikation,
dasselbe Audit-Log — nur gegen die Datei statt gegen die Docs API.
"""

from __future__ import annotations

import re
from pathlib import Path

STATUS_MARK = {"done": "✅ ERLEDIGT", "open": "⬜ OFFEN"}
SEPARATORS = ("—", "–", "-", ":")
# Alte Marker, die beim Statuswechsel ersetzt werden.
_OLD_MARK_RE = re.compile(r"^(?:✅\s*ERLEDIGT|⬜\s*OFFEN|☑\s*ERLEDIGT|☐\s*OFFEN)\s*",
                          re.IGNORECASE)


def _line_re(wo: int) -> re.Pattern:
    separators = "|".join(re.escape(item) for item in SEPARATORS)
    return re.compile(
        rf"^(?P<prefix>[ \t>*_-]*Work Order\s*{int(wo)}\s*(?:{separators})\s*)(?P<rest>.*)$",
        re.MULTILINE | re.IGNORECASE,
    )


def targets(root: Path) -> list[Path]:
    """Lokale Doku-Dateien, in denen Work Orders stehen können.

    Reihenfolge: was `atlas init` verwaltet, dann alles andere Markdown im
    Projekt. Die Liste ist bewusst klein — gesucht wird nur in Dateien, die
    tatsächlich eine Work-Order-Zeile enthalten.
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
    """Die Datei und Zeile, die diese Work Order dokumentiert."""
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
    """Tauscht nur den Statusmarker, nicht den Titel."""
    if _OLD_MARK_RE.match(rest):
        return _OLD_MARK_RE.sub(f"{mark} ", rest, count=1).rstrip()
    return f"{mark} {rest}".strip() if rest else mark


def _write_atomic(path: Path, text: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def set_status(root: Path, conn, wo: int, status: str,
               dry_run: bool = False) -> dict:
    """Ändert den Status einer Work Order in der lokalen Doku."""
    from . import audit, indexer

    mark = STATUS_MARK.get(status.lower())
    if mark is None:
        raise ValueError(f"Unbekannter Status '{status}' (erlaubt: done, open)")

    found = find_line(root, wo)
    if found is None:
        return {
            "written": False,
            "verified": False,
            "reason": f"Work Order {wo} steht in keiner lokalen Doku-Datei. "
                      "Erst `atlas publish` oder die Zeile manuell anlegen.",
            "source": "local",
        }

    path, text, match = found
    old_line = match.group(0)
    new_line = match.group("prefix") + _replace_mark(match.group("rest"), mark)
    if new_line == old_line:
        return {
            "written": False,
            "unchanged": True,
            "verified": True,
            "wo": int(wo),
            "status": status.lower(),
            "source": "local",
            "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
        }

    payload = {
        "written": not dry_run,
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

    # Selbstverifikation: aus der Datei zurücklesen, nicht aus dem Speicher.
    confirmed = find_line(root, wo)
    verified = bool(confirmed and mark in confirmed[2].group(0))
    payload["verified"] = verified
    if not verified:
        payload["warning"] = "Zeile wurde geschrieben, steht aber nicht so in der Datei."

    indexer.index_file(conn, root, path)
    conn.commit()

    audit.log(root, "set_status", wo=wo, status=status, source="local",
              path=payload["path"], verified=verified)
    return payload


def publish(root: Path, conn, wo: int, line: str, section: str,
            dry_run: bool = False) -> dict:
    """Legt eine Work-Order-Zeile in der lokalen Doku an."""
    from . import audit, indexer

    if find_line(root, wo) is not None:
        return {
            "written": False,
            "unchanged": True,
            "reason": f"Work Order {wo} existiert bereits — Status ändern statt neu anlegen.",
            "source": "local",
        }

    candidates = [path for path in targets(root)
                  if path.name.lower().startswith("architecture")
                  or "work order" in path.read_text(encoding="utf-8", errors="replace").lower()]
    if not candidates:
        candidates = targets(root)[:1]
    if not candidates:
        return {"written": False, "verified": False, "source": "local",
                "reason": "Keine lokale Doku-Datei gefunden — `atlas init` ausführen."}

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
    verified = bool(confirmed and line.strip() in confirmed[2].group(0))
    payload["verified"] = verified
    if not verified:
        payload["warning"] = "Zeile wurde geschrieben, steht aber nicht so in der Datei."

    indexer.index_file(conn, root, path)
    conn.commit()
    audit.log(root, "publish", wo=wo, source="local",
              path=payload["path"], verified=verified)
    return payload


def _insert_into_section(text: str, section: str, line: str) -> tuple[str, int]:
    """Hängt die Zeile ans Ende des Abschnitts (sonst ans Dateiende)."""
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
        # Kein Abschnitt: ans Ende, mit Leerzeile davor.
        body = text.rstrip("\n")
        return f"{body}\n\n{line}\n", len(body) + 2

    # Ende des Abschnitts: nächste Überschrift gleicher oder höherer Ebene.
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped.startswith("#"):
            continue
        if len(stripped) - len(stripped.lstrip("#")) <= level:
            end = index
            break

    # Leerzeilen am Abschnittsende nicht mitzählen, aber eine behalten.
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)
    return "\n".join(lines) + "\n", insert_at
