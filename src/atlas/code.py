"""Symbolsuche im Code — Datei, Zeile, Signatur statt 125k-Token-Datei.

Ein Agent, der `turn_manager.gd` komplett liest, verbrennt 124.969 Tokens. Für die
Frage „wo ist `swap_positions_with_ally` definiert?" reichen Datei, Zeile und
Signatur. Dieses Modul macht genau das, ohne Index: einmal über die Quellen,
Substring-Filter vor dem Regex (das ist der billige Teil), dann nur die Treffer.

Bewusst kein Parser: GDScript-Signaturen sind mit zwei Regexen zuverlässig genug,
und ein Symbol-Index müsste bei jeder Code-Änderung nachziehen.

**Anbindung zählt, nicht Definition.** Seit WO 8 (1.070 Zeilen implementiert,
Aufrufer vergessen, vom nächsten Hotfix als „unused" gelöscht) liefert jede Suche
zusätzlich die Zahl der Aufrufe *und* der Referenzen. Referenzen sind der Teil,
den ein reiner `name(`-Scan übersieht:

    signal.connect(_on_skill)        → Callable ohne Klammer
    tween_method(_update_bar, ...)   → Callable ohne Klammer
    call("_dispatch_skill")          → String
    Callable(self, "_on_skill")      → String
    [connection ... method="_on_skill"]  in .tscn
"""

from __future__ import annotations

import re
from pathlib import Path

FUNC_RE = re.compile(
    # Ganze Signatur bis zum abschließenden ':' — inklusive 'static' und '-> Typ'.
    r"^[ \t]*(?P<signature>(?:static[ \t]+)?func[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"[ \t]*\((?P<args>[^)]*)\)[^\n:]*)",
    re.MULTILINE,
)
CLASS_RE = re.compile(r"^class_name[ \t]+(?P<name>[A-Za-z_]\w*)", re.MULTILINE)
# Zeilen, die das Symbol *definieren* statt es zu benutzen.
DEF_LINE_RE = re.compile(r"^[ \t]*(?:static[ \t]+)?func[ \t]+(?P<name>[A-Za-z_]\w*)")
SIGNAL_DECL_RE = re.compile(r"^[ \t]*signal[ \t]+(?P<name>[A-Za-z_]\w*)")
# Szenen-Anbindung: [connection signal="pressed" from="X" to="." method="_on_pressed"]
TSCN_CONN_RE = re.compile(r'\[connection\b[^\]]*?method="(?P<name>[A-Za-z_]\w*)"')

SKIP_DIRS = {
    ".godot", ".git", ".venv", "__pycache__", "_archive", "_legacy_archive",
    "art_source", "node_modules", "addons",
}


def _iter_sources(root: Path):
    """Definitionen: nur GDScript."""
    for path in sorted(root.rglob("*.gd")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _iter_all_sources(root: Path):
    """Referenzen: GDScript **und** Szenen — dort steht kein Code, aber die Bindung."""
    for pattern in ("*.gd", "*.tscn"):
        for path in sorted(root.rglob(pattern)):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find(root, symbol: str, limit: int = 20) -> list[dict]:
    """Definitionen, deren Name den Suchbegriff enthält (case-insensitiv)."""
    needle = symbol.strip().lower()
    if not needle:
        return []

    hits: list[dict] = []
    for path in _iter_sources(Path(root)):
        text = _read(path)
        # Substring-Test ist um Größenordnungen billiger als zwei Regex-Läufe
        # über jede Datei.
        if needle not in text.lower():
            continue

        rel = path.relative_to(root).as_posix()
        for match in FUNC_RE.finditer(text):
            if needle not in match.group("name").lower():
                continue
            hits.append({
                "path": rel,
                "line": text.count("\n", 0, match.start()) + 1,
                "kind": "func",
                "symbol": match.group("name"),
                "signature": match.group("signature").strip(),
            })
        for match in CLASS_RE.finditer(text):
            if needle not in match.group("name").lower():
                continue
            hits.append({
                "path": rel,
                "line": text.count("\n", 0, match.start()) + 1,
                "kind": "class",
                "symbol": match.group("name"),
                "signature": f"class_name {match.group('name')}",
            })
        if len(hits) >= limit:
            break

    return hits[:limit]


def references(root, symbol: str, limit: int = 20) -> dict:
    """Alle Stellen, die das Symbol *benutzen* — getrennt in Aufrufe und Referenzen.

    `calls`  = `name(` — direkter Aufruf
    `refs`   = `name` ohne Klammer: Signal-/Tween-Callable, `call("name")`,
               `Callable(self, "name")`, `.tscn`-`[connection method="name"]`

    Die Trennung ist wichtig: Ein Symbol mit 0 `calls`, aber 3 `refs` ist über
    Signale gebunden und lebt. Ein Symbol mit 0 `calls` **und** 0 `refs` ist tot.
    """
    needle = symbol.strip()
    result = {"calls": [], "refs": [], "call_count": 0, "ref_count": 0}
    if not needle:
        return result

    word = re.compile(rf"\b{re.escape(needle)}\b")
    call_re = re.compile(rf"\b{re.escape(needle)}\s*\(")

    for path in _iter_all_sources(Path(root)):
        text = _read(path)
        if needle not in text:
            continue

        rel = path.relative_to(root).as_posix()

        if path.suffix == ".tscn":
            # In Szenen ist jede Erwähnung eine Bindung, nie eine Definition.
            for number, line in enumerate(text.splitlines(), 1):
                match = TSCN_CONN_RE.search(line)
                if match and match.group("name") == needle:
                    result["refs"].append(
                        {"path": rel, "line": number, "text": line.strip()[:160]}
                    )
            continue

        for number, line in enumerate(text.splitlines(), 1):
            if not word.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Die Definitionszeile selbst ist keine Benutzung.
            def_match = DEF_LINE_RE.match(line)
            if def_match and def_match.group("name") == needle:
                continue
            decl_match = SIGNAL_DECL_RE.match(line)
            if decl_match and decl_match.group("name") == needle:
                continue

            entry = {"path": rel, "line": number, "text": stripped[:160]}
            if call_re.search(line):
                result["calls"].append(entry)
            else:
                result["refs"].append(entry)

    result["call_count"] = len(result["calls"])
    result["ref_count"] = len(result["refs"])
    result["calls"] = result["calls"][:limit]
    result["refs"] = result["refs"][:limit]
    return result


def find_usages(root, symbol: str, limit: int = 20) -> list[dict]:
    """Aufrufe **und** Referenzen als eine Liste — abwärtskompatibler Einstieg."""
    refs = references(root, symbol, limit=limit)
    return refs["calls"] + refs["refs"]


def render(root, symbol: str, limit: int = 20, with_usage: bool = False) -> str:
    hits = find(root, symbol, limit=limit)
    usage = references(root, symbol, limit=limit)
    total = usage["call_count"] + usage["ref_count"]

    if not hits and not total:
        return (
            f"Kein Treffer für '{symbol}' — weder als `func`/`class_name` noch als "
            "Benutzung (inkl. .tscn-Signalverbindungen)."
        )

    lines: list[str] = []
    if hits:
        lines.append(f"{len(hits)} Definition(en) für '{symbol}':")
        for hit in hits:
            lines.append(f"  {hit['path']}:{hit['line']}  {hit['signature']}")
    else:
        lines.append(f"Keine Definition für '{symbol}' — nur benutzt, nicht definiert.")

    # Immer: die Anbindung. Kostet zwei Zeilen und verhindert WO 8.
    lines.append(
        f"Anbindung: {usage['call_count']} Aufruf(e), "
        f"{usage['ref_count']} Referenz(en) (Signal/Callable/.tscn)"
    )

    if hits and total == 0:
        lines.append("")
        lines.append(
            f"⚠ '{symbol}' ist definiert, wird aber NIRGENDS aufgerufen oder gebunden — "
            "weder per `name(`, per Signal-Callable, per `call(\"...\")` noch in einer .tscn."
        )
        lines.append("  Eine Implementierung ohne Aufrufer ist toter Code (vgl. WO 8).")
    elif usage["call_count"] == 0 and usage["ref_count"] > 0:
        lines.append(
            "  (kein direkter Aufruf, aber gebunden — Signale/Callables zählen als Anbindung)"
        )

    if with_usage and total:
        if usage["calls"]:
            lines.append("")
            lines.append(f"Aufrufe ({usage['call_count']}):")
            for hit in usage["calls"]:
                lines.append(f"  {hit['path']}:{hit['line']}  {hit['text']}")
        if usage["refs"]:
            lines.append("")
            lines.append(f"Referenzen ({usage['ref_count']}):")
            for hit in usage["refs"]:
                lines.append(f"  {hit['path']}:{hit['line']}  {hit['text']}")

    lines.append("")
    lines.append("_Nur Signaturen und Fundzeilen gelesen, nicht die Dateien._")
    return "\n".join(lines)
