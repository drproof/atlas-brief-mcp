"""Symbolsuche im Code — Datei, Zeile, Signatur statt 125k-Token-Datei.

An agent that reads a 9,000-line manager module end to end burns ~125,000 tokens.
For the question "where is `swap_positions_with_ally` defined?" a file, a line and
a signature are enough. This module does exactly that, without an index: one pass
over the sources, substring filter before the regex (that is the cheap part), then
only the hits.

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

# Eine Quelle der Wahrheit für "was ist eine Funktionsdefinition": `atlas init`
# und die Symbolsuche müssen dieselben Sprachen sehen. Sonst schreibt `init`
# eine AGENTS.md, die `atlas_code_find` empfiehlt, und das Gate findet im
# Python- oder TS-Projekt nichts.
from .bootstrap import DEF_PATTERNS, SKIP_KEYWORDS, iter_source_files

FUNC_RE = re.compile(
    # Ganze Signatur bis zum abschließenden ':' — inklusive 'static' und '-> Typ'.
    r"^[ \t]*(?P<signature>(?:static[ \t]+)?func[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"[ \t]*\((?P<args>[^)]*)\)[^\n:]*)",
    re.MULTILINE,
)
# GDScript `class_name Foo`, Python/TS/Java/C# `class Foo` / `export class Foo`.
CLASS_RE = re.compile(
    r"^[ \t]*(?:class_name[ \t]+"
    r"|(?:export[ \t]+)?(?:default[ \t]+)?(?:abstract[ \t]+)?class[ \t]+)"
    r"(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)
# Zeilen, die das Symbol *definieren* statt es zu benutzen.
DEF_LINE_RE = re.compile(r"^[ \t]*(?:static[ \t]+)?func[ \t]+(?P<name>[A-Za-z_]\w*)")
SIGNAL_DECL_RE = re.compile(r"^[ \t]*signal[ \t]+(?P<name>[A-Za-z_]\w*)")
# Szenen-Anbindung: [connection signal="pressed" from="X" to="." method="_on_pressed"]
TSCN_CONN_RE = re.compile(r'\[connection\b[^\]]*?method="(?P<name>[A-Za-z_]\w*)"')

SKIP_DIRS = {
    ".godot", ".git", ".venv", "__pycache__", "_archive", "_legacy_archive",
    "art_source", "node_modules", "addons",
}


def _iter_all_sources(root: Path):
    """Definitionen **und** Referenzen: alle Quellsprachen plus Godot-Szenen.

    `.tscn` enthält keinen Code, aber die Bindung — `[connection method=...]` ist
    die einzige Spur eines Signal-Handlers.
    """
    for path in iter_source_files(root):
        yield path
    for path in sorted(root.rglob("*.tscn")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find(root, symbol: str, limit: int = 20, exact: bool = False) -> list[dict]:
    """Definitionen, deren Name den Suchbegriff enthält (case-insensitiv).

    `exact=True` für das Anbindungs-Gate: Dort zählt nur die Definition mit
    genau diesem Namen. Ohne den Filter frisst `limit` die Substring-Treffer
    (`can_vault_a/b/c`) und die echte `func vault()` liegt hinter dem Abbruch.
    """
    needle = symbol.strip().lower()
    if not needle:
        return []

    hits: list[dict] = []
    for path in iter_source_files(root):
        text = _read(path)
        # Substring-Test ist um Größenordnungen billiger als zwei Regex-Läufe
        # über jede Datei.
        if needle not in text.lower():
            continue
        rel = path.relative_to(root).as_posix()
        for pattern in DEF_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group("name")
                if not name or name in SKIP_KEYWORDS:
                    continue
                if needle not in name.lower():
                    continue
                if exact and name != symbol:
                    continue
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.start())
                raw_line = text[line_start: line_end if line_end != -1 else len(text)]
                hits.append({
                    "path": rel,
                    "line": text.count("\n", 0, match.start()) + 1,
                    "kind": "func",
                    "symbol": name,
                    # Ohne abschließenden Doppelpunkt/Klammer: die Signatur,
                    # nicht die Zeile. Gilt für `func f():`, `def f():` und
                    # `function f() {`.
                    "signature": raw_line.strip().rstrip(":{").strip(),
                })
        for match in CLASS_RE.finditer(text):
            name = match.group("name")
            if needle not in name.lower():
                continue
            if exact and name != symbol:
                continue
            hits.append({
                "path": rel,
                "line": text.count("\n", 0, match.start()) + 1,
                "kind": "class",
                "symbol": name,
                "signature": text[text.rfind("\n", 0, match.start()) + 1:
                                  text.find("\n", match.start())].strip().rstrip(":{").strip(),
            })
        if len(hits) >= limit:
            break

    return hits[:limit]


def _is_definition_line(line: str, needle: str) -> bool:
    """Definiert diese Zeile das Symbol? Dann ist sie keine Benutzung."""
    for pattern in DEF_PATTERNS:
        match = pattern.match(line)
        if match and match.group("name") == needle:
            return True
    match = CLASS_RE.match(line)
    return bool(match and match.group("name") == needle)


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
            # Die Definitionszeile selbst ist keine Benutzung — in jeder
            # Sprache, nicht nur bei `func`.
            if _is_definition_line(line, needle):
                continue
            decl_match = SIGNAL_DECL_RE.match(line)
            if decl_match and decl_match.group("name") == needle:
                continue
            # `class_name Foo` deklariert die Klasse — es benutzt sie nicht.
            # Ohne diese Zeile besteht jede tote Klasse das Anbindungs-Gate.
            class_match = CLASS_RE.match(line)
            if class_match and class_match.group("name") == needle:
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
            f"No match for '{symbol}' — neither as a definition nor as "
            "a usage (including .tscn signal connections)."
        )

    lines: list[str] = []
    if hits:
        lines.append(f"{len(hits)} definition(s) for '{symbol}':")
        for hit in hits:
            lines.append(f"  {hit['path']}:{hit['line']}  {hit['signature']}")
    else:
        lines.append(f"No definition for '{symbol}' — used, but not defined.")

    # Immer: die Anbindung. Kostet zwei Zeilen und verhindert WO 8.
    lines.append(
        f"Wiring: {usage['call_count']} call(s), "
        f"{usage['ref_count']} reference(s) (signal/Callable/.tscn)"
    )

    if hits and total == 0:
        lines.append("")
        lines.append(
            f"⚠ '{symbol}' is defined, but NEVER called or bound — "
            "neither via `name(`, via signal Callable, via `call(\"...\")` nor in a .tscn."
        )
        lines.append("  An implementation without a caller is dead code (see WO 8).")
    elif usage["call_count"] == 0 and usage["ref_count"] > 0:
        lines.append(
            "  (no direct call, but bound — signals/Callables count as wiring)"
        )

    if with_usage and total:
        if usage["calls"]:
            lines.append("")
            lines.append(f"Calls ({usage['call_count']}):")
            for hit in usage["calls"]:
                lines.append(f"  {hit['path']}:{hit['line']}  {hit['text']}")
        if usage["refs"]:
            lines.append("")
            lines.append(f"References ({usage['ref_count']}):")
            for hit in usage["refs"]:
                lines.append(f"  {hit['path']}:{hit['line']}  {hit['text']}")

    lines.append("")
    lines.append("_Read signatures and match lines only, not full files._")
    return "\n".join(lines)
