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
from .bootstrap import (
    DEF_PATTERNS,
    SKIP_KEYWORDS,
    _func_body_end,
    _paren_delta,
    iter_def_patterns,
    iter_source_files,
)

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
    r"(?:class|struct|interface|protocol|trait|enum|record)[ \t]+(?P<name>[A-Za-z_]\w*)"
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
SIGNAL_DECL_RE = re.compile(r"^[ \t]*signal[ \t]+(?P<name>[A-Za-z_]\w*)", re.MULTILINE)
ONREADY_VAR_RE = re.compile(
    r"^[ \t]*@onready[ \t]+var[ \t]+(?P<name>[A-Za-z_]\w*)(?![ \t]*(?::[^=\n]*)?=[ \t]*func[ \t]*\()",
    re.MULTILINE,
)
# Scene bindings: `[connection ... method="_on_pressed"]`, `method=&"_on_pressed"`,
# Animation CallMethodTrack `"method": &"_spawn_vfx"`, and SubResource `Callable(..., "_on_hit")`
TSCN_CONN_RE = re.compile(r'\[connection\b[^\]]*?method=&?"(?P<name>[A-Za-z_]\w*)"')
TSCN_METHOD_KEY_RE = re.compile(r'"method"\s*:\s*&?"(?P<name>[A-Za-z_]\w*)"')
TSCN_CALLABLE_RE = re.compile(r'\bCallable\s*\((?:[^()\n]|\([^()\n]*\))*,\s*&?"(?P<name>[A-Za-z_]\w*)"\s*\)')
_RUST_RAW_OPEN_RE = re.compile(r'b?r(#*)"')

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


def _format_signature(lines: list[str], start_idx: int) -> str:
    """Extract a single-line signature, joining multi-line parameter lists up to closing `)`."""
    if start_idx < 0 or start_idx >= len(lines):
        return ""
    first = lines[start_idx]
    depth = _paren_delta(first)
    if depth <= 0:
        return first.strip().rstrip(":{").strip()
    parts = [first.strip()]
    idx = start_idx + 1
    while idx < len(lines) and (idx - start_idx) < 12 and depth > 0:
        cur = lines[idx].strip()
        if cur and not cur.startswith(("#", "//")):
            parts.append(cur)
        depth += _paren_delta(lines[idx])
        idx += 1
    if idx < len(lines) and lines[idx].strip().startswith(("->", "where ")):
        parts.append(lines[idx].strip())
    joined = re.sub(r"\s+", " ", " ".join(parts))
    return joined.rstrip(":{").strip()


def _strip_comments_and_docstrings(text: str, suffix: str = "") -> list[str]:
    """Strip multiline docstrings (`\"\"\"`/`'''`), raw multiline strings, `/* ... */`, and `#`/`//` comments.

    Preserves exact line count (`len(result) == len(text.splitlines())`) and keeps
    single-line string literals (`"..."`, `'...'`) intact so dynamic bindings such as
    `call("_on_skill")` and `Callable(self, "_on_skill")` continue to register as references.
    """
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    ext = (suffix or "").lower()
    hash_comments = ext in {"", ".py", ".gd"}
    triple_dq = ext in {"", ".py", ".gd", ".kt", ".swift", ".java", ".cs"}
    triple_sq = ext in {"", ".py", ".gd"}
    slash_comments = ext in {
        "", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
        ".rs", ".cs", ".go", ".java", ".kt", ".swift",
    }
    is_rust = ext == ".rs"
    is_csharp = ext == ".cs"
    is_go = ext == ".go"

    out: list[str] = []
    state = "NORMAL"
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if state == "LINE_COMMENT":
            if ch == "\n":
                out.append("\n")
                state = "NORMAL"
            i += 1
            continue

        if state == "BLOCK_COMMENT":
            if text.startswith("*/", i):
                out.append("  ")
                state = "NORMAL"
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue

        if state in ("TRIPLE_DQ", "TRIPLE_SQ"):
            closing = '"""' if state == "TRIPLE_DQ" else "'''"
            if ch == "\\" and i + 1 < n:
                out.append("\n" if text[i + 1] == "\n" else " ")
                i += 2
                continue
            if text.startswith(closing, i):
                out.append("   ")
                state = "NORMAL"
                i += 3
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue

        if state in ("DQ", "SQ", "BT"):
            quote_ch = '"' if state == "DQ" else ("'" if state == "SQ" else "`")
            if ch == "\\" and i + 1 < n:
                out.append(text[i: i + 2])
                i += 2
                continue
            out.append(ch)
            if ch == quote_ch or (ch == "\n" and state != "BT"):
                state = "NORMAL"
            i += 1
            continue

        # state == "NORMAL"
        if triple_dq and text.startswith('"""', i):
            state = "TRIPLE_DQ"
            out.append("   ")
            i += 3
            continue

        if triple_sq and text.startswith("'''", i):
            state = "TRIPLE_SQ"
            out.append("   ")
            i += 3
            continue

        if slash_comments and text.startswith("/*", i):
            state = "BLOCK_COMMENT"
            out.append("  ")
            i += 2
            continue

        if slash_comments and text.startswith("//", i):
            state = "LINE_COMMENT"
            i += 2
            continue

        if hash_comments and ch == "#":
            state = "LINE_COMMENT"
            i += 1
            continue

        if is_rust and ch in ("r", "b") and (
            i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
        ):
            raw_m = _RUST_RAW_OPEN_RE.match(text, i)
            if raw_m:
                closing = '"' + raw_m.group(1)
                end_pos = text.find(closing, raw_m.end())
                close_end = end_pos + len(closing) if end_pos != -1 else n
                segment = text[i:close_end]
                if "\n" in segment:
                    out.append("".join("\n" if c == "\n" else " " for c in segment))
                else:
                    out.append(segment)
                i = close_end
                continue

        if is_csharp and text.startswith('@"', i):
            j = i + 2
            while j < n:
                if text.startswith('""', j):
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            segment = text[i:j]
            if "\n" in segment:
                out.append("".join("\n" if c == "\n" else " " for c in segment))
            else:
                out.append(segment)
            i = j
            continue

        if is_go and ch == "`":
            end_pos = text.find("`", i + 1)
            close_end = end_pos + 1 if end_pos != -1 else n
            segment = text[i:close_end]
            if "\n" in segment:
                out.append("".join("\n" if c == "\n" else " " for c in segment))
            else:
                out.append(segment)
            i = close_end
            continue

        if ch == '"':
            state = "DQ"
            out.append(ch)
            i += 1
            continue

        if ch == "'":
            if is_rust:
                # Distinguish Rust char literals (`'x'`, `'\n'`) from lifetimes (`'a`, `'static`).
                if re.match(r"'(?:\\[^'\n]+|[^'\\\n])'", text[i: i + 8]):
                    state = "SQ"
            else:
                state = "SQ"
            out.append(ch)
            i += 1
            continue

        if ch == "`" and slash_comments:
            state = "BT"
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out).splitlines()


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
    suffix: str = "",
    clean_text: str | None = None,
) -> dict:
    """Build the working surface around a definition: leading comments/decorators, numbered body, file header, enclosing class, and sibling signatures."""
    # 1. Include leading decorators (including multi-line `@app.post(\n...\n)`) and doc comments above the definition
    context_start = def_idx
    paren_balance = 0
    while context_start > 0 and (def_idx - context_start) < 20:
        prev_raw = lines[context_start - 1]
        prev = prev_raw.strip()
        if not prev:
            break
        if paren_balance < 0:
            context_start -= 1
            paren_balance += _paren_delta(prev_raw)
            continue
        if prev.startswith(("@", "#", "//", "/*", "*", "*/", "#[")):
            context_start -= 1
            continue
        if prev.endswith((")", "],", "},")) and _paren_delta(prev_raw) < 0:
            # Look ahead upwards to verify an `@decorator(` opens this bracket block
            probe = context_start - 1
            bal = _paren_delta(prev_raw)
            found_decorator = False
            while probe > 0 and (def_idx - probe) < 20 and bal < 0:
                probe -= 1
                bal += _paren_delta(lines[probe])
                if bal >= 0 and lines[probe].lstrip().startswith("@"):
                    found_decorator = True
                    break
            if found_decorator:
                context_start = probe
                continue
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
    scan_source = (
        clean_text
        if clean_text is not None
        else "\n".join(_strip_comments_and_docstrings(text, suffix))
    )
    siblings: list[str] = []
    seen_sibs: set[tuple[int, str]] = set()
    for pattern in iter_def_patterns(suffix):
        for match in pattern.finditer(scan_source):
            name = match.group("name")
            if not name or name in SKIP_KEYWORDS or name == symbol:
                continue
            s_line = scan_source.count("\n", 0, match.start()) + 1
            if (s_line, name) in seen_sibs:
                continue
            seen_sibs.add((s_line, name))
            sig = _format_signature(lines, s_line - 1)
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
        if needle not in text.lower():
            continue
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        clean_lines = _strip_comments_and_docstrings(text, path.suffix)
        clean_text = "\n".join(clean_lines)
        if needle not in clean_text.lower():
            continue
        seen_defs: set[tuple[int, str]] = set()
        for pattern in iter_def_patterns(path.suffix):
            for match in pattern.finditer(clean_text):
                name = match.group("name")
                if not name or name in SKIP_KEYWORDS:
                    continue
                if needle not in name.lower():
                    continue
                if exact and name != symbol:
                    continue
                line_no = clean_text.count("\n", 0, match.start()) + 1
                if (line_no, name) in seen_defs:
                    continue
                seen_defs.add((line_no, name))
                entry: dict = {
                    "path": rel,
                    "line": line_no,
                    "kind": "func",
                    "symbol": name,
                    "signature": _format_signature(lines, line_no - 1),
                }
                if include_body and lines:
                    start_idx = max(0, line_no - 1)
                    end_idx = _func_body_end(lines, start_idx, suffix=path.suffix)
                    entry.update(
                        _extract_file_surface(
                            text,
                            lines,
                            start_idx,
                            end_idx,
                            name,
                            max_body_lines,
                            suffix=path.suffix,
                            clean_text=clean_text,
                        )
                    )
                hits.append(entry)
        for match in CLASS_RE.finditer(clean_text):
            name = _class_match_name(match)
            if not name or needle not in name.lower():
                continue
            if exact and name != symbol:
                continue
            line_no = clean_text.count("\n", 0, match.start()) + 1
            if (line_no, name) in seen_defs:
                continue
            seen_defs.add((line_no, name))
            entry = {
                "path": rel,
                "line": line_no,
                "kind": "class",
                "symbol": name,
                "signature": _format_signature(lines, line_no - 1),
            }
            if include_body and lines:
                start_idx = max(0, line_no - 1)
                end_idx = _func_body_end(lines, start_idx, suffix=path.suffix)
                entry.update(
                    _extract_file_surface(
                        text,
                        lines,
                        start_idx,
                        end_idx,
                        name,
                        max_body_lines,
                        suffix=path.suffix,
                        clean_text=clean_text,
                    )
                )
            hits.append(entry)
        if path.suffix.lower() == ".gd":
            for gd_kind, gd_re in (("signal", SIGNAL_DECL_RE), ("var", ONREADY_VAR_RE)):
                for match in gd_re.finditer(clean_text):
                    name = match.group("name")
                    if not name or needle not in name.lower():
                        continue
                    if exact and name != symbol:
                        continue
                    line_no = clean_text.count("\n", 0, match.start()) + 1
                    if (line_no, name) in seen_defs:
                        continue
                    seen_defs.add((line_no, name))
                    entry = {
                        "path": rel,
                        "line": line_no,
                        "kind": gd_kind,
                        "symbol": name,
                        "signature": lines[line_no - 1].strip() if line_no - 1 < len(lines) else "",
                    }
                    if include_body and lines:
                        start_idx = max(0, line_no - 1)
                        entry.update(
                            _extract_file_surface(
                                text,
                                lines,
                                start_idx,
                                start_idx,
                                name,
                                max_body_lines,
                                suffix=path.suffix,
                                clean_text=clean_text,
                            )
                        )
                    hits.append(entry)
        if len(hits) >= limit:
            break

    return hits[:limit]


def _is_definition_line(line: str, needle: str, suffix: str = "") -> bool:
    """Return True if this line defines the symbol (and is therefore not a usage)."""
    for pattern in iter_def_patterns(suffix):
        match = pattern.match(line)
        if match and match.group("name") == needle:
            return True
    match = CLASS_RE.match(line)
    if match and _class_match_name(match) == needle:
        return True
    if suffix.lower() in {"", ".gd"}:
        sig_m = SIGNAL_DECL_RE.match(line)
        if sig_m and sig_m.group("name") == needle:
            return True
        var_m = ONREADY_VAR_RE.match(line)
        if var_m and var_m.group("name") == needle:
            return True
    return False


def _definition_lines_in_text(text: str, needle: str, suffix: str = "") -> set[int]:
    """Return 1-based line numbers where `needle` is declared in `text` (outside comments/docstrings)."""
    clean_text = "\n".join(_strip_comments_and_docstrings(text, suffix))
    def_lines: set[int] = set()
    for pattern in iter_def_patterns(suffix):
        for match in pattern.finditer(clean_text):
            if match.group("name") == needle:
                def_lines.add(clean_text.count("\n", 0, match.start()) + 1)
    for match in CLASS_RE.finditer(clean_text):
        if _class_match_name(match) == needle:
            def_lines.add(clean_text.count("\n", 0, match.start()) + 1)
    if suffix.lower() in {"", ".gd"}:
        for gd_re in (SIGNAL_DECL_RE, ONREADY_VAR_RE):
            for match in gd_re.finditer(clean_text):
                if match.group("name") == needle:
                    def_lines.add(clean_text.count("\n", 0, match.start()) + 1)
    return def_lines


_PY_FROM_IMPORT_RE = re.compile(r"^[ \t]*from[ \t]+[\w.]+[ \t]+import[ \t]+")
_PY_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+[A-Za-z_][\w., \t\\]*$")
_JS_IMPORT_RE = re.compile(r"^[ \t]*(?:import|export[ \t]+type)[ \t]+(?![ \t]*\()")
_JS_IMPORT_DONE_RE = re.compile(r"""(?:;\s*$|\bfrom\s+['"][^'"]+['"])""")
_RUST_USE_RE = re.compile(r"^[ \t]*(?:pub(?:\([^)\n]+\))?[ \t]+)?use[ \t]+[A-Za-z_:]")
_CS_USING_RE = re.compile(
    r"^[ \t]*(?:global[ \t]+)?using[ \t]+(?:static[ \t]+)?(?!(?:var|new)\b)[A-Za-z_][\w.<>, \t=]*;[ \t]*$"
)


def _import_lines_in_text(clean_text: str, suffix: str = "") -> set[int]:
    """Return 1-based line numbers of pure import declarations so uncalled imports never pass the wiring gate."""
    ext = (suffix or "").lower()
    lines = clean_text.splitlines()
    import_lines: set[int] = set()
    idx = 0
    n = len(lines)

    while idx < n:
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            idx += 1
            continue

        if ext in {"", ".py"} and (_PY_FROM_IMPORT_RE.match(line) or _PY_IMPORT_RE.match(line)):
            import_lines.add(idx + 1)
            depth = _paren_delta(line)
            cont = stripped.endswith("\\")
            idx += 1
            steps = 0
            while idx < n and steps < 40 and (depth > 0 or cont):
                import_lines.add(idx + 1)
                cur = lines[idx]
                depth += _paren_delta(cur)
                cont = cur.strip().endswith("\\")
                idx += 1
                steps += 1
            continue

        if ext == ".go" and re.match(r"^[ \t]*import\b", line):
            import_lines.add(idx + 1)
            if "(" in line and ")" not in line:
                idx += 1
                steps = 0
                while idx < n and steps < 60:
                    import_lines.add(idx + 1)
                    if ")" in lines[idx]:
                        idx += 1
                        break
                    idx += 1
                    steps += 1
                continue
            idx += 1
            continue

        if ext in {".java", ".kt", ".swift"} and re.match(r"^[ \t]*import[ \t]+", line):
            import_lines.add(idx + 1)
            idx += 1
            continue

        if ext in {"", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"} and _JS_IMPORT_RE.match(line):
            import_lines.add(idx + 1)
            if re.match(r"""^[ \t]*import[ \t]+['"][^'"]+['"]""", line) or _JS_IMPORT_DONE_RE.search(line):
                idx += 1
                continue
            idx += 1
            steps = 0
            while idx < n and steps < 30:
                import_lines.add(idx + 1)
                if _JS_IMPORT_DONE_RE.search(lines[idx]):
                    idx += 1
                    break
                idx += 1
                steps += 1
            continue

        if ext in {"", ".rs"} and _RUST_USE_RE.match(line):
            import_lines.add(idx + 1)
            if ";" not in line:
                idx += 1
                steps = 0
                while idx < n and steps < 30:
                    import_lines.add(idx + 1)
                    if ";" in lines[idx]:
                        idx += 1
                        break
                    idx += 1
                    steps += 1
                continue
            idx += 1
            continue

        if ext in {"", ".cs"} and _CS_USING_RE.match(line):
            import_lines.add(idx + 1)
            idx += 1
            continue

        idx += 1

    return import_lines


def references(root, symbol: str, limit: int = 20) -> dict:
    """Find all places that *use* the symbol — split into calls and references.

    `calls`  = `name(` — direct call (excluding docstrings, block comments, and line comments)
    `refs`   = `name` without parentheses: signal/tween Callable, `call("name")`,
               `Callable(self, "name")`, `.tscn` `[connection method="name"]` or CallMethodTrack

    The distinction matters: a symbol with 0 `calls` but 3 `refs` is bound via
    signals and is alive. A symbol with 0 `calls` **and** 0 `refs` (including a
    symbol that is merely imported and never invoked) is dead.
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
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")

        rel = path.relative_to(root).as_posix()

        if path.suffix == ".tscn":
            # In scenes every mention is a binding (signal connection, CallMethodTrack, or Callable).
            for number, line in enumerate(text.splitlines(), 1):
                matched_tscn = False
                for tscn_re in (TSCN_CONN_RE, TSCN_METHOD_KEY_RE, TSCN_CALLABLE_RE):
                    for m in tscn_re.finditer(line):
                        if m.group("name") == needle:
                            matched_tscn = True
                            break
                    if matched_tscn:
                        break
                if matched_tscn:
                    result["refs"].append(
                        {"path": rel, "line": number, "text": line.strip()[:160]}
                    )
            continue

        raw_lines = text.splitlines()
        clean_lines = _strip_comments_and_docstrings(text, path.suffix)
        clean_text = "\n".join(clean_lines)
        def_lines = _definition_lines_in_text(text, needle, path.suffix)
        import_lines = _import_lines_in_text(clean_text, path.suffix)

        for idx, raw_line in enumerate(raw_lines):
            number = idx + 1
            if number in def_lines or number in import_lines:
                continue
            clean_line = clean_lines[idx] if idx < len(clean_lines) else ""
            if not clean_line or not word.search(clean_line):
                continue
            if _is_definition_line(clean_line, needle, path.suffix):
                continue

            entry = {"path": rel, "line": number, "text": raw_line.strip()[:160]}
            if call_re.search(clean_line):
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
