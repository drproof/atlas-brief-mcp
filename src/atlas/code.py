"""Symbol search in code — file, line, and signature instead of a 125k-token file.

An agent that reads a 9,000-line manager module end to end burns ~125,000 tokens.
For the question "where is `swap_positions_with_ally` defined?" a file, a line and
a signature are enough. This module does exactly that, without an index: one pass
over the sources, substring filter before the regex (that is the cheap part), then
only the hits.

Deliberately no parser: GDScript signatures are reliable enough with two regexes,
and a symbol index would have to be updated on every code change.

**Wiring counts, not definition.** Since WO 8 (1,070 lines implemented, caller
forgotten, deleted by the next hotfix as "unused"), every search also returns
the number of calls *and* references. References are the part a pure `name(` scan
misses:

    signal.connect(_on_skill)        → Callable without parentheses
    tween_method(_update_bar, ...)   → Callable without parentheses
    call("_dispatch_skill")          → String
    Callable(self, "_on_skill")      → String
    [connection ... method="_on_skill"]  in .tscn
"""

from __future__ import annotations

import re
from pathlib import Path

# Single source of truth for "what is a function definition": `atlas init`
# and symbol search must see the same languages. Otherwise `init` writes an
# AGENTS.md recommending `atlas_code_find`, while the gate finds nothing in
# a Python or TS project.
from .bootstrap import DEF_PATTERNS, SKIP_KEYWORDS, _func_body_end, iter_source_files

FUNC_RE = re.compile(
    # Full signature up to the trailing ':' — including 'static' and '-> Type'.
    r"^[ \t]*(?P<signature>(?:static[ \t]+)?func[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"[ \t]*\((?P<args>[^)]*)\)[^\n:]*)",
    re.MULTILINE,
)
# GDScript `class_name Foo`, Python/TS/Java/C#/Rust/Kotlin/Swift/Go type definitions.
CLASS_RE = re.compile(
    r"^[ \t]*(?:class_name[ \t]+(?P<gd_name>[A-Za-z_]\w*)"
    r"|(?:(?:export|default|public|private|protected|internal|fileprivate|open|pub(?:\([^)\n]+\))?"
    r"|abstract|sealed|final|static|partial|data|value)[ \t]+)*"
    r"(?:class|struct|interface|trait|enum|record)[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"|type[ \t]+(?P<go_name>[A-Za-z_]\w*)[ \t]+(?:struct|interface)\b)",
    re.MULTILINE,
)


def _class_match_name(match: re.Match) -> str:
    return (
        match.group("name")
        or match.group("gd_name")
        or match.group("go_name")
        or ""
    )
# Lines that *define* the symbol rather than using it.
DEF_LINE_RE = re.compile(r"^[ \t]*(?:static[ \t]+)?func[ \t]+(?P<name>[A-Za-z_]\w*)")
SIGNAL_DECL_RE = re.compile(r"^[ \t]*signal[ \t]+(?P<name>[A-Za-z_]\w*)")
# Scene binding: [connection signal="pressed" from="X" to="." method="_on_pressed"]
TSCN_CONN_RE = re.compile(r'\[connection\b[^\]]*?method="(?P<name>[A-Za-z_]\w*)"')

SKIP_DIRS = {
    ".godot", ".git", ".venv", "__pycache__", "_archive", "_legacy_archive",
    "art_source", "node_modules", "addons",
}


def _iter_all_sources(root: Path):
    """Yield definitions **and** references: all source languages plus Godot scenes.

    `.tscn` contains no code, but holds the binding — `[connection method=...]` is
    the only trace of a signal handler.
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


_HEADER_LINE_RE = re.compile(
    r"^(?:extends\s+\S+|class_name\s+\S+|import\s+.+|from\s+\S+\s+import\s+.+"
    r"|use\s+\S+|package\s+\S+|const\s+[A-Za-z_]\w*\s*[:=].+"
    r"|[A-Z][A-Z0-9_]{2,}\s*(?::\s*[\w\[\]]+\s*)?=\s*.+)$"
)


def _extract_file_surface(
    text: str,
    lines: list[str],
    def_idx: int,
    end_idx: int,
    symbol: str,
    max_body_lines: int = 80,
) -> dict:
    """Build the working surface around a definition: leading comments/decorators, numbered body, file header, enclosing class, and sibling signatures."""
    # 1. Include leading decorators and doc/invariant comments directly above the definition (up to 10 lines)
    context_start = def_idx
    while context_start > 0 and (def_idx - context_start) < 10:
        prev = lines[context_start - 1].strip()
        if prev.startswith(("@", "#", "//", "/*", "*", "*/")):
            context_start -= 1
        else:
            break

    slice_end = min(end_idx + 1, context_start + max_body_lines)
    body_lines = lines[context_start:slice_end]
    body_text = "\n".join(body_lines)
    numbered_lines = [
        f"{line_num:>5} | {content}"
        for line_num, content in enumerate(body_lines, start=context_start + 1)
    ]
    if end_idx + 1 > slice_end:
        tail = f"# … truncated ({end_idx - context_start + 1} lines total)"
        body_text += f"\n{tail}"
        numbered_lines.append(f"      | {tail}")

    # 2. Enclosing class/struct if this definition is indented
    enclosing_class = ""
    def_indent = len(lines[def_idx]) - len(lines[def_idx].lstrip()) if def_idx < len(lines) else 0
    if def_indent > 0:
        for idx in range(def_idx - 1, -1, -1):
            candidate = lines[idx]
            if not candidate.strip():
                continue
            cand_indent = len(candidate) - len(candidate.lstrip())
            if cand_indent < def_indent and CLASS_RE.match(candidate):
                enclosing_class = f"L{idx + 1}: {candidate.strip().rstrip(':{').strip()}"
                break

    # 3. File-level imports & constants at the top of the file (first 60 lines, up to 10 items)
    header_items: list[str] = []
    for raw in lines[:60]:
        stripped = raw.strip()
        if _HEADER_LINE_RE.match(stripped):
            header_items.append(stripped[:100])
            if len(header_items) >= 10:
                break

    # 4. Sibling function signatures in the same file (up to 15)
    siblings: list[str] = []
    seen_sibs: set[tuple[int, str]] = set()
    for pattern in DEF_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group("name")
            if not name or name in SKIP_KEYWORDS or name == symbol:
                continue
            s_line = text.count("\n", 0, match.start()) + 1
            if (s_line, name) in seen_sibs:
                continue
            seen_sibs.add((s_line, name))
            l_start = text.rfind("\n", 0, match.start()) + 1
            l_end = text.find("\n", match.start())
            sig = text[l_start: l_end if l_end != -1 else len(text)].strip().rstrip(":{").strip()
            siblings.append(f"L{s_line}: {sig}")
    siblings.sort(key=lambda item: int(item.split(":", 1)[0][1:]))

    return {
        "context_start_line": context_start + 1,
        "end_line": end_idx + 1,
        "body": body_text,
        "numbered_body": "\n".join(numbered_lines),
        "enclosing_class": enclosing_class,
        "file_header": header_items,
        "siblings": siblings[:15],
    }


def find(
    root,
    symbol: str,
    limit: int = 20,
    exact: bool = False,
    include_body: bool = False,
    max_body_lines: int = 80,
) -> list[dict]:
    """Find definitions whose name contains the search term (case-insensitive).

    `exact=True` for the wiring gate: there only the definition with this exact
    name counts. Without the filter, `limit` is consumed by substring matches
    (`can_vault_a/b/c`) and the real `func vault()` falls past the cutoff.
    """
    needle = symbol.strip().lower()
    if not needle:
        return []

    hits: list[dict] = []
    for path in iter_source_files(root):
        text = _read(path)
        # Substring check is orders of magnitude cheaper than two regex passes
        # across every file.
        if needle not in text.lower():
            continue
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines() if include_body else []
        seen_defs: set[tuple[int, str]] = set()
        for pattern in DEF_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group("name")
                if not name or name in SKIP_KEYWORDS:
                    continue
                if needle not in name.lower():
                    continue
                if exact and name != symbol:
                    continue
                line_no = text.count("\n", 0, match.start()) + 1
                if (line_no, name) in seen_defs:
                    continue
                seen_defs.add((line_no, name))
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.start())
                raw_line = text[line_start: line_end if line_end != -1 else len(text)]
                entry: dict = {
                    "path": rel,
                    "line": line_no,
                    "kind": "func",
                    "symbol": name,
                    # Without trailing colon/brace: the signature, not the line.
                    # Works for `func f():`, `def f():`, and `function f() {`.
                    "signature": raw_line.strip().rstrip(":{").strip(),
                }
                if include_body and lines:
                    start_idx = max(0, line_no - 1)
                    end_idx = _func_body_end(lines, start_idx)
                    entry.update(
                        _extract_file_surface(text, lines, start_idx, end_idx, name, max_body_lines)
                    )
                hits.append(entry)
        for match in CLASS_RE.finditer(text):
            name = _class_match_name(match)
            if not name or needle not in name.lower():
                continue
            if exact and name != symbol:
                continue
            line_no = text.count("\n", 0, match.start()) + 1
            if (line_no, name) in seen_defs:
                continue
            seen_defs.add((line_no, name))
            line_end = text.find("\n", match.start())
            entry = {
                "path": rel,
                "line": line_no,
                "kind": "class",
                "symbol": name,
                "signature": text[text.rfind("\n", 0, match.start()) + 1:
                                  line_end if line_end != -1 else len(text)].strip().rstrip(":{").strip(),
            }
            if include_body and lines:
                start_idx = max(0, line_no - 1)
                end_idx = _func_body_end(lines, start_idx)
                entry.update(
                    _extract_file_surface(text, lines, start_idx, end_idx, name, max_body_lines)
                )
            hits.append(entry)
        if len(hits) >= limit:
            break

    return hits[:limit]


def _is_definition_line(line: str, needle: str) -> bool:
    """Return True if this line defines the symbol (and is therefore not a usage)."""
    for pattern in DEF_PATTERNS:
        match = pattern.match(line)
        if match and match.group("name") == needle:
            return True
    match = CLASS_RE.match(line)
    return bool(match and _class_match_name(match) == needle)


def references(root, symbol: str, limit: int = 20) -> dict:
    """Find all places that *use* the symbol — split into calls and references.

    `calls`  = `name(` — direct call
    `refs`   = `name` without parentheses: signal/tween Callable, `call("name")`,
               `Callable(self, "name")`, `.tscn` `[connection method="name"]`

    The distinction matters: a symbol with 0 `calls` but 3 `refs` is bound via
    signals and is alive. A symbol with 0 `calls` **and** 0 `refs` is dead.
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
            # In scenes every mention is a binding, never a definition.
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
            if stripped.startswith(("#", "//")):
                continue
            # The definition line itself is not a usage — in any language,
            # not only for `func`.
            if _is_definition_line(line, needle):
                continue
            decl_match = SIGNAL_DECL_RE.match(line)
            if decl_match and decl_match.group("name") == needle:
                continue
            # `class_name Foo` declares the class — it does not use it.
            # Without this check, every dead class would pass the wiring gate.
            class_match = CLASS_RE.match(line)
            if class_match and _class_match_name(class_match) == needle:
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
    """Return calls **and** references as a single list — backward-compatible entry point."""
    refs = references(root, symbol, limit=limit)
    return refs["calls"] + refs["refs"]


def render(
    root,
    symbol: str,
    limit: int = 20,
    with_usage: bool = False,
    include_body: bool = False,
) -> str:
    from . import verify

    hits = find(root, symbol, limit=limit, include_body=include_body)
    usage = references(root, symbol, limit=limit)
    total = usage["call_count"] + usage["ref_count"]
    guards_map = verify.symbol_guards(root)

    if not hits and not total and symbol.strip() not in guards_map:
        return (
            f"No match for '{symbol}' — neither as a definition nor as "
            "a usage (including .tscn signal connections)."
        )

    lines: list[str] = []
    shown_guards: set[str] = set()
    if hits:
        lines.append(f"{len(hits)} definition(s) for '{symbol}':")
        for hit in hits:
            span = (
                f"{hit['line']}-{hit['end_line']}"
                if include_body and hit.get("end_line")
                else f"{hit['line']}"
            )
            lines.append(f"  {hit['path']}:{span}  {hit['signature']}")
            sym_guards = guards_map.get(hit["symbol"], [])
            if sym_guards:
                shown_guards.add(hit["symbol"])
                labels = [
                    f"WO {g['wo']} ({g['title']})" if g.get("title") else f"WO {g['wo']}"
                    for g in sym_guards
                ]
                lines.append(
                    f"  🛡️ Guarded by {', '.join(labels)} — keep signature & callers wired"
                )
            if include_body and hit.get("body"):
                lines.append("  ```")
                block = hit.get("numbered_body") or hit["body"]
                for b_line in block.splitlines():
                    lines.append(f"  {b_line}")
                lines.append("  ```")
                if hit.get("enclosing_class"):
                    lines.append(f"  Enclosing type: {hit['enclosing_class']}")
                if hit.get("file_header"):
                    lines.append("  File header: " + " · ".join(hit["file_header"][:6]))
                if hit.get("siblings"):
                    lines.append(
                        f"  Sibling symbols in {hit['path']} ({len(hit['siblings'])}): "
                        + " | ".join(hit["siblings"][:8])
                    )
    else:
        lines.append(f"No definition for '{symbol}' — used, but not defined.")
        if symbol.strip() in guards_map:
            labels = [
                f"WO {g['wo']} ({g['title']})" if g.get("title") else f"WO {g['wo']}"
                for g in guards_map[symbol.strip()]
            ]
            lines.append(
                f"  🛡️ Guarded by {', '.join(labels)} — required symbol is missing!"
            )

    # Always include wiring info. Costs two lines and prevents WO 8.
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
