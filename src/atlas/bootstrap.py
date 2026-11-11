"""`atlas init`: bring an undocumented repository into an indexable state.

No LLM, no network — a filesystem scan, a token estimate, and the existing
symbol scanner. The output is a Markdown document that passes `lint` cleanly,
plus the rule files a coding agent needs.

Principle: **never overwrite.** Anyone who already has an `AGENTS.md` keeps it.
`init` fills in gaps and reports what was already there — a bootstrap that wipes
out existing work is used exactly once.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

SOURCE_EXTS = (".gd", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
               ".rs", ".cs", ".go", ".java", ".kt", ".swift")
SKIP_DIRS = {
    ".git", ".atlas", ".gdd", ".godot", "node_modules", ".venv", "venv",
    "__pycache__", "build", "dist", "target", "_archive", "_legacy_archive",
    "art_source", ".idea", ".vscode",
}
HOTSPOT_TOKENS = 10_000
UNWIRED_MIN_LINES = 15
TOKENS_PER_CHAR = 3.5  # conservative estimate; consistent with the rest of the tool


def estimate_tokens(text: str) -> int:
    return round(len(text) / TOKENS_PER_CHAR)


def iter_source_files(root: Path):
    """Yield source files without descending into excluded or huge directories.

    Deliberately uses `os.walk` instead of `rglob`: `rglob` raises an exception
    on a single unreadable directory and aborts the entire scan. A bootstrap that
    fails on a foreign `node_modules` is unusable.
    """
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.endswith(SOURCE_EXTS):
                yield Path(dirpath) / name


SKIP_KEYWORDS = {
    # Control structures — look like methods, but aren't
    "if", "for", "while", "switch", "catch", "else", "do", "try", "with",
    "match", "loop", "where", "type",
    "return", "function", "constructor", "super", "this", "new", "typeof",
    "class", "struct", "interface", "protocol", "enum", "record", "trait", "impl",
    "namespace", "package", "import", "using", "throw", "await", "yield",
    "nameof", "sizeof", "lock", "synchronized", "main",
    # Godot virtuals — called by the engine
    "_ready", "_init", "_process", "_physics_process", "_input",
    "_unhandled_input", "_unhandled_key_input", "_enter_tree", "_exit_tree",
    "_draw", "_notification", "_to_string", "__init__", "__str__", "__repr__",
    # Framework hooks called by the framework
    "render", "ngOnInit", "ngOnDestroy", "componentDidMount", "componentWillUnmount",
}

_ANGLE_ATOM = r"(?:[^<>=\-\n]|-(?!>)|=(?!>)|->|=>)"
_TYPE_PARAMS = rf"(?:<(?:{_ANGLE_ATOM}|<{_ANGLE_ATOM}*>)*>)"
_PAREN_ATOM = r"(?:[^()\n]|\([^()\n]*\))"

DEF_PATTERNS = (
    # GDScript / Python / Rust / Swift / Kotlin:
    # [modifiers...] (func|def|fn|fun) [<TypeParams>] name [<TypeParams>] (
    re.compile(
        r"^[ \t]*(?:(?:pub(?:\([^)\n]+\))?|public|private|protected|internal|fileprivate|open"
        r"|static|async|suspend|inline|override|mutating|nonmutating|final|unsafe|const"
        r"|extern(?:[ \t]+\"[^\"]*\")?|operator|tailrec)[ \t]+)*"
        r"(?:func|def|fn|fun)[ \t]+"
        rf"(?:{_TYPE_PARAMS}[ \t]+)*"
        rf"(?P<name>[A-Za-z_]\w*)[ \t]*(?:{_TYPE_PARAMS})?[ \t]*\(",
        re.MULTILINE,
    ),
    # Go functions & receiver methods: func (r *Recv) Name[T any](
    re.compile(
        r"^[ \t]*func[ \t]+(?:\([^)\n]+\)[ \t]+)?(?P<name>[A-Za-z_]\w*)[ \t]*(?:\[(?:[^\[\]\n]|\[[^\[\]\n]*\])+\])?[ \t]*\(",
        re.MULTILINE,
    ),
    # JS/TS: [export [default]] [async] function name[<T>]( — the closing parenthesis
    # may be on a later line (multi-line TS signatures).
    re.compile(
        r"^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*"
        rf"(?P<name>[A-Za-z_$][\w$]*)[ \t]*(?:{_TYPE_PARAMS})?[ \t]*\(",
        re.MULTILINE,
    ),
    # JS/TS: [export] const name = [Wrapper<…>(] [async] ((…) => | arg => | multiline (…) => | function()
    re.compile(
        r"^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?(?:const|let|var)[ \t]+"
        r"(?P<name>[A-Za-z_$][\w$]*)[ \t]*(?::[^=\n]+)?=[ \t]*"
        rf"(?:[A-Za-z_$][\w$.]*(?:{_TYPE_PARAMS})?\([ \t]*)?"
        r"(?:async[ \t]+)?(?:"
        rf"\({_PAREN_ATOM}*\)[^\n=]*?=>"
        r"|[A-Za-z_$][\w$]*[ \t]*=>"
        rf"|\([ \t]*\n(?:[ \t]*[^\s()]{_PAREN_ATOM}*\n|[ \t]*\n){{1,12}}[ \t]*\)[^\n=]*?=>"
        r"|function[ \t]*\*?[ \t]*\("
        r")",
        re.MULTILINE,
    ),
    # GDScript 4 callable / lambda property binding: [@onready] var name [: Callable] := func(
    re.compile(
        r"^[ \t]*(?:@onready[ \t]+)?var[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*(?::[^=\n]*)?=[ \t]*func[ \t]*\(",
        re.MULTILINE,
    ),
    # JS/TS class & interface methods with explicit modifier(s):
    # [public|private|protected|readonly|override|static|async|abstract|get|set]+ [*] name[<T>](
    re.compile(
        r"^[ \t]*(?:(?:public|private|protected|readonly|override|static|async|abstract|get|set)[ \t]+)+"
        rf"(?:\*[ \t]*)?(?P<name>[A-Za-z_$][\w$]*)[ \t]*(?:{_TYPE_PARAMS})?[ \t]*\(",
        re.MULTILINE,
    ),
    # TypeScript unmodified class/object methods with type annotations or 0-arg `name() {`:
    # e.g. `  resolveTurnOrder(units: Unit[]): Unit[] {` or multiline `  resolve(\n    u: Unit,\n  ): void {`
    re.compile(
        rf"^[ \t]+(?:\*[ \t]*)?(?P<name>[A-Za-z_$][\w$]*)[ \t]*(?:{_TYPE_PARAMS})?[ \t]*"
        r"(?:"
        r"\([ \t]*\)[ \t]*(?::[ \t]*[^\n{=>;]*[^\s{=>;])?[ \t]*\{[ \t]*$"
        rf"|\([^():\n]*:[ \t]*{_PAREN_ATOM}+\)[ \t]*(?::[ \t]*[^\n{{=>;]*[^\s{{=>;])?[ \t]*(?:\{{|[ \t]*;$)"
        rf"|\({_PAREN_ATOM}*\)[ \t]*:[ \t]*[^\n{{=>;]*[^\s{{=>;][ \t]*\{{[ \t]*$"
        rf"|\([ \t]*\n(?:[ \t]+[^\s()]{_PAREN_ATOM}*\n|[ \t]*\n){{1,12}}[ \t]*\)[ \t]*(?::[ \t]*[^\n{{=>;]*[^\s{{=>;])?[ \t]*\{{"
        r")",
        re.MULTILINE,
    ),
    # C# / Java typed methods: require at least one access/method modifier + return type + Name(
    re.compile(
        r"^[ \t]*(?:(?:public|private|protected|internal|static|virtual|override|abstract"
        r"|async|sealed|final|synchronized|extern|unsafe|partial|readonly)[ \t]+)+"
        rf"[A-Za-z_][\w.,\[\]\?]*(?:{_TYPE_PARAMS}[\w.\[\]\?]*)?[ \t]+"
        rf"(?P<name>[A-Za-z_]\w*)[ \t]*(?:{_TYPE_PARAMS})?[ \t]*\(",
        re.MULTILINE,
    ),
)

JS_TS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_JS_PARAM = (
    r"(?:(?:\.\.\.)?[A-Za-z_$][\w$]*|\{[^{};\n]*\}|\[[^\[\];\n]*\])"
    r"(?:[ \t]*=[ \t]*[^,)\n]*[^\s,)])?"
)
# Unmodified untyped ES6 class/object methods (`  computeDamage(attacker, defender) {`):
# restricted to JS/TS extensions when known so Kotlin trailing-lambda calls (`withLock(m) {`)
# are never misclassified as definitions.
JS_TS_BARE_METHOD_RE = re.compile(
    rf"^[ \t]+(?:\*[ \t]*)?(?P<name>[A-Za-z_$][\w$]*)[ \t]*\("
    rf"[ \t]*{_JS_PARAM}(?:[ \t]*,[ \t]*{_JS_PARAM})*[ \t]*,?"
    r"[ \t]*\)[ \t]*\{[ \t]*$",
    re.MULTILINE,
)

# Files mounted externally by a bundler or router: `export default`
# there does not need a caller in user code.
ENTRY_FILES = {
    "app.tsx", "app.jsx", "app.ts", "app.js",
    "main.tsx", "main.jsx", "main.ts", "main.js",
    "index.tsx", "index.jsx", "index.ts", "index.js",
    "page.tsx", "page.jsx", "layout.tsx", "layout.jsx",
    "route.ts", "route.tsx", "not-found.tsx", "error.tsx", "loading.tsx",
}

# Minimum body length for a function to count as a core domain symbol.
CORE_SYMBOL_MIN_LINES = 8


def iter_def_patterns(suffix: str = ""):
    """Return applicable definition regexes for a source file extension."""
    ext = (suffix or "").lower()
    if not ext:
        return (*DEF_PATTERNS, JS_TS_BARE_METHOD_RE)
    if ext == ".py":
        return (DEF_PATTERNS[0],)
    if ext == ".gd":
        return (DEF_PATTERNS[0], DEF_PATTERNS[4])
    if ext in {".rs", ".kt", ".swift"}:
        return (DEF_PATTERNS[0],)
    if ext == ".go":
        return (DEF_PATTERNS[0], DEF_PATTERNS[1])
    if ext in {".cs", ".java"}:
        return (DEF_PATTERNS[7],)
    if ext in JS_TS_EXTS:
        return (DEF_PATTERNS[2], DEF_PATTERNS[3], DEF_PATTERNS[5], DEF_PATTERNS[6], JS_TS_BARE_METHOD_RE)
    return DEF_PATTERNS


def _iter_defs(text: str, suffix: str = ""):
    """Yield (name, start_line, def_line) for all recognized functions outside comments/docstrings."""
    from .code import _strip_comments_and_docstrings

    raw_lines = text.splitlines()
    clean_text = "\n".join(_strip_comments_and_docstrings(text, suffix))
    seen: set[tuple[str, int]] = set()
    for pattern in iter_def_patterns(suffix):
        for match in pattern.finditer(clean_text):
            name = match.group("name")
            if not name or name in SKIP_KEYWORDS:
                continue
            start = clean_text.count("\n", 0, match.start())
            key = (name, start)
            if key in seen:
                continue
            seen.add(key)
            line = raw_lines[start] if start < len(raw_lines) else ""
            yield name, start, line


def _advance_triple_quote(line: str, active: str | None, suffix: str = "") -> str | None:
    """Track unescaped `\"\"\"`, `'''`, and multiline `` ` `` literal state across a line."""
    is_go = (suffix or "").lower() == ".go"
    idx = 0
    n = len(line)
    while idx < n:
        ch = line[idx]
        if active is not None:
            if ch == "\\" and not (active == "`" and is_go):
                idx += 2
                continue
            if line.startswith(active, idx):
                step = len(active)
                active = None
                idx += step
                continue
            idx += 1
            continue
        if ch == "\\":
            idx += 2
            continue
        if ch == "#" or line.startswith("//", idx):
            break
        if line.startswith('"""', idx) or line.startswith("'''", idx):
            active = line[idx: idx + 3]
            idx += 3
            continue
        if ch == "`":
            active = "`"
            idx += 1
            continue
        if ch in ('"', "'"):
            quote_ch = ch
            idx += 1
            while idx < n:
                if line[idx] == "\\":
                    idx += 2
                    continue
                if line[idx] == quote_ch:
                    idx += 1
                    break
                idx += 1
            continue
        idx += 1
    return active


def _paren_delta(line: str) -> int:
    """Return net `(` minus `)` outside quotes/comments on a single line."""
    delta = 0
    in_quote: str | None = None
    idx = 0
    while idx < len(line):
        ch = line[idx]
        if ch == "\\":
            idx += 2
            continue
        if in_quote:
            if ch == in_quote:
                in_quote = None
            idx += 1
            continue
        if ch == "#" or line.startswith("//", idx):
            break
        if ch in ('"', "'", "`"):
            in_quote = ch
        elif ch == "(":
            delta += 1
        elif ch == ")":
            delta -= 1
        idx += 1
    return delta


def _func_body_end(lines: list[str], start: int, suffix: str = "") -> int:
    """Return the last line of the function body starting at `start` (0-based).

    Handles multi-line signatures (`def foo(\\n a: int\\n) -> bool:`), Rust `where`
    clauses, and unindented text inside `\"\"\"...\"\"\"` or `` `...` `` multiline strings.
    """
    line = lines[start]
    def_indent = len(line) - len(line.lstrip())
    end = start
    in_triple = _advance_triple_quote(line, None, suffix=suffix)
    paren_depth = max(0, _paren_delta(line))
    sig_closed = paren_depth == 0 and line.rstrip().endswith((":", "{"))
    in_where = False

    for offset in range(start + 1, len(lines)):
        current = lines[offset]
        if in_triple is not None:
            end = offset
            in_triple = _advance_triple_quote(current, in_triple, suffix=suffix)
            continue
        if not current.strip():
            continue
        indent = len(current) - len(current.lstrip())
        stripped = current.strip()

        if paren_depth > 0:
            end = offset
            paren_depth = max(0, paren_depth + _paren_delta(current))
            if paren_depth == 0 and stripped.endswith((":", "{")):
                sig_closed = True
            in_triple = _advance_triple_quote(current, in_triple, suffix=suffix)
            continue

        if in_where and not sig_closed:
            end = offset
            if stripped.endswith("{") or stripped == "{":
                sig_closed = True
                in_where = False
            continue

        if indent <= def_indent:
            if not sig_closed and (
                stripped.startswith((")", "{", "->", "where ", "where:"))
                or stripped in ("{", "):", "where")
            ):
                end = offset
                if stripped == "where" or stripped.startswith(("where ", "where:")):
                    if stripped.endswith("{"):
                        sig_closed = True
                    else:
                        in_where = True
                elif stripped.endswith((":", "{")):
                    sig_closed = True
                continue
            # Continuation of a multi-line signature — the body starts after it.
            if stripped.startswith(")") or stripped in ("{", "):"):
                end = offset
                continue
            # The closing brace of a TS/C# method still belongs to the function.
            if stripped in ("}", "};", "});") and indent == def_indent:
                end = offset
            break
        sig_closed = True
        end = offset
        in_triple = _advance_triple_quote(current, in_triple, suffix=suffix)
    return end


def _has_decorator(lines: list[str], start: int) -> bool:
    """Check whether a decorator precedes the definition — including multi-line ones.

    `@app.post(\\n "/api",\\n)\\ndef handler():` — the line right above `def`
    is `)`. Checking only `lines[start-1]` would treat the endpoint as unwired.
    """
    line = lines[start]
    def_indent = len(line) - len(line.lstrip())
    index = start - 1
    while index >= 0:
        current = lines[index]
        if not current.strip():
            return False  # blank line separates decorator from definition
        stripped_above = current.lstrip()
        if stripped_above.startswith(("@", "#[")) or (
            stripped_above.startswith("[") and stripped_above.rstrip().endswith("]")
        ):
            return True
        if len(current) - len(current.lstrip()) < def_indent:
            return False
        index -= 1
    return False


def _core_symbols(text: str, limit: int = 12, suffix: str = "") -> list[str]:
    """Return the largest functions in a file — not the first ones.

    The first twelve `func` lines of a GDScript file are `_ready`, `get_int`,
    `get_float`, `next_int`: virtuals and trivial getters. As "core symbols"
    they are useless. What matters is where logic lives, and it lives in the
    long functions.
    """
    lines = text.splitlines()
    sized: list[tuple[int, str]] = []
    for name, start, _line in _iter_defs(text, suffix=suffix):
        length = _func_body_end(lines, start, suffix=suffix) - start + 1
        if length < CORE_SYMBOL_MIN_LINES:
            continue
        sized.append((length, name))
    sized.sort(key=lambda item: -item[0])
    seen: set[str] = set()
    result: list[str] = []
    for _length, name in sized:
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= limit:
            break
    return result


SUBPROJECT_MARKERS = (
    "project.godot", "package.json", "Cargo.toml", "pyproject.toml",
    "go.mod", "pom.xml", "build.gradle", ".git",
)


def _detect_subproject(domain_dir: Path) -> str:
    """Check whether this folder hosts its own project (i.e., a partial copy).

    A subfolder with `project.godot`, `package.json`, or `.git` is its own
    repository. Callers outside it are invisible to the scanner — "unwired" is a
    weaker claim there, and that needs to be noted in the docs before someone
    deletes code on suspicion.
    """
    for marker in SUBPROJECT_MARKERS:
        if (domain_dir / marker).exists():
            return marker
    # One level deeper: `games/dungeon_quest/project.godot`
    try:
        for child in domain_dir.iterdir():
            if not child.is_dir():
                continue
            for marker in SUBPROJECT_MARKERS:
                if (child / marker).exists():
                    return f"{child.name}/{marker}"
    except OSError:
        pass
    return ""


def scan_repository(root: Path) -> dict:
    """Group files by domain, tokens, hotspots, and unwired symbols."""
    root = Path(root)
    sources: list[tuple[str, str, int]] = []  # (rel, text, tokens)
    domains: dict[str, dict] = {}

    for path in sorted(iter_source_files(root)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        tokens = estimate_tokens(text)
        sources.append((rel, text, tokens))

        parts = Path(rel).parts
        domain = parts[0] if len(parts) > 1 else "(root)"
        bucket = domains.setdefault(
            domain,
            {"files": [], "tokens": 0, "symbols": [], "root": root / parts[0]},
        )
        bucket["files"].append({"path": rel, "lines": len(text.splitlines()),
                                "tokens": tokens})
        bucket["tokens"] += tokens
        # Symbols of the largest file first, so the domain list shows real logic
        # rather than twelve getters from a random file.
        for name in _core_symbols(text, limit=6, suffix=path.suffix):
            if name not in bucket["symbols"] and len(bucket["symbols"]) < 12:
                bucket["symbols"].append(name)

    texts = {rel: text for rel, text, _t in sources}
    for bucket in domains.values():
        bucket["files"].sort(key=lambda item: -item["tokens"])
        bucket["subproject"] = _detect_subproject(bucket["root"])
        # Core symbols from the largest files in the domain, not the first ones:
        # a 60-line helper file does not contain core logic.
        symbols: list[str] = []
        for item in bucket["files"][:5]:
            for name in _core_symbols(
                texts.get(item["path"], ""), limit=4, suffix=Path(item["path"]).suffix
            ):
                if name not in symbols:
                    symbols.append(name)
        bucket["symbols"] = symbols[:12]

    hotspots = [
        {"path": rel, "tokens": tokens}
        for rel, _text, tokens in sources if tokens > HOTSPOT_TOKENS
    ]
    hotspots.sort(key=lambda item: -item["tokens"])

    unwired = _find_unwired(sources)

    return {
        "root": str(root),
        "files": len(sources),
        "tokens": sum(t for _r, _t, t in sources),
        "domains": dict(sorted(domains.items(), key=lambda kv: -kv[1]["tokens"])),
        "hotspots": hotspots,
        "unwired": unwired,
    }


def _find_unwired(sources: list[tuple[str, str, int]]) -> list[dict]:
    """Find functions > 15 lines whose name appears nowhere else in the codebase.

    One pass over the combined text per symbol: `str.count` runs in C, keeping
    the scan under one second even across a hundred files.

    Two kinds of wiring that are not direct calls but still count:

    - **Test convention**: `test_foo` is discovered by pytest by name, never
      called directly. 15 of 20 hits in a real Python project were test functions —
      nobody reads a warning list that is 75% false positives.
    - **Decorator registration**: `@mcp.tool()`, `@app.route(...)`,
      `@pytest.fixture` bind the function to a framework, so it never appears
      as a call in the code.
    """
    haystack = "\n".join(text for _rel, text, _t in sources)
    findings: list[dict] = []
    seen: set[str] = set()

    for rel, text, _tokens in sources:
        lines = text.splitlines()
        is_entry = Path(rel).name.lower() in ENTRY_FILES
        for name, start, def_line in _iter_defs(text, suffix=Path(rel).suffix):
            if name in seen or len(name) < 4:
                continue
            # Test conventions (pytest, unittest, JS test runners)
            if name.startswith("test_") or name.startswith("Test") or name.endswith("_test"):
                continue
            # An `export default` in App.tsx/page.tsx/route.ts is mounted
            # externally by the bundler or router — not a missing caller.
            if is_entry and "export default" in def_line:
                continue
            # Decorator above the definition (even multi-line) = bound to a
            # framework, not a missing caller.
            if _has_decorator(lines, start):
                continue
            length = _func_body_end(lines, start) - start + 1
            if length < UNWIRED_MIN_LINES:
                continue
            seen.add(name)
            # The name itself appears once (the definition). More means used.
            if haystack.count(name) <= 1:
                findings.append({
                    "symbol": name,
                    "path": rel,
                    "line": start + 1,
                    "lines": length,
                })

    findings.sort(key=lambda item: -item["lines"])
    return findings[:20]


def _domain_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "root"


def generate_architecture_skeleton(scan: dict, project: str = "") -> str:
    """Markdown that passes `lint` cleanly: no empty sections, no heading jumps,
    work orders in the standard format."""
    name = project or Path(scan["root"]).name
    stamp = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# Architecture: {name}",
        "",
        f"_Generated by `atlas init` on {stamp} from {scan['files']} source files "
        f"(~{scan['tokens']:,} tokens). Deterministic, straight from the filesystem — "
        "no model inference. The TODO blocks are yours to fill in: `atlas init` "
        "never overwrites this file._",
        "",
        "## 1. Domains",
        "",
        "Each domain is a folder. File paths are spelled out on purpose: "
        "`atlas_links` recognises them, and an agent can read one file instead of "
        "searching a whole directory.",
        "",
    ]

    for index, (domain, bucket) in enumerate(scan["domains"].items(), start=1):
        slug = _domain_slug(domain)
        files_count = len(bucket["files"])
        subproject = bucket.get("subproject", "")
        marker = f" · **sub-project** `{subproject}`" if subproject else ""
        lines.append(f"### 1.{index} {domain} — {files_count} "
                     f"{'file' if files_count == 1 else 'files'}, "
                     f"~{bucket['tokens']:,} tokens  [{slug}]{marker}")
        lines.append("")
        lines.append("Files (by size):")
        lines.append("")
        for item in bucket["files"][:15]:
            lines.append(f"- `{item['path']}` — {item['lines']} lines, "
                         f"~{item['tokens']:,} tokens")
        if len(bucket["files"]) > 15:
            lines.append(f"- … {len(bucket['files']) - 15} more")
        if bucket["symbols"]:
            lines.append("")
            lines.append("Core symbols: "
                         + ", ".join(f"`{s}`" for s in bucket["symbols"]))
        lines.append("")
        lines.append("TODO: what this domain is for, in two sentences, plus the "
                     "invariants a refactor must not break.")
        lines.append("")

    lines += [
        "## 2. Risks",
        "",
        "### 2.1 Context hotspots (over 10,000 tokens per file)",
        "",
    ]
    if scan["hotspots"]:
        lines.append("These files blow any context window if read in one piece. "
                     "For changes, search them (`atlas_code_find`) — don't open them.")
        lines.append("")
        for item in scan["hotspots"]:
            lines.append(f"- `{item['path']}` — ~{item['tokens']:,} tokens")
    else:
        lines.append("No file over 10,000 tokens — every file is readable in one step.")
    lines.append("")

    lines.append("### 2.2 Unwired symbols (defined, never called)")
    lines.append("")
    if scan["unwired"]:
        lines.append("These functions are longer than 15 lines and are mentioned "
                     "nowhere outside their own definition. Either dead code, or the "
                     "caller is missing — check whether a work order built them "
                     "before deleting anything.")
        lines.append("")
        lines.append("**This is a hint, not a verdict.** Only this repository is "
                     "visible: a caller in another repo, in a framework (decorator, "
                     "signal, reflection) or in a partial copy stays invisible. With "
                     "extracted modules the list is unreliable — check first, then "
                     "delete.")
        lines.append("")
        for item in scan["unwired"]:
            lines.append(f"- `{item['symbol']}` in `{item['path']}:{item['line']}` "
                         f"({item['lines']} lines)")
    else:
        lines.append("Every function over 15 lines is used somewhere.")
    lines.append("")

    lines += [
        "## 3. Work Orders",
        "",
        "Work Order 1 — ⬜ OPEN (Complete the architecture doc): fill in the TODO "
        "blocks in section 1 per domain — purpose, invariants, and the files that "
        "must not be touched.",
        "",
        "Status line format: `Work Order <N> — ⬜ OPEN (title): description` or "
        "`✅ DONE`. `atlas_status` reads them, `atlas_publish` writes them. A "
        "completed order is never reopened — a regression is a new work order.",
        "",
    ]
    return "\n".join(lines)


_DOMAIN_HEADING_RE = re.compile(
    r"^###\s+1\.\d+\s+(?P<domain>[^\n—]+?)\s+—[^\n]*?(?:\[(?P<slug>[a-z0-9-]+)\])?[^\n]*$",
    re.MULTILINE,
)


def _extract_domain_notes(existing_text: str) -> dict[str, str]:
    """Extract user-written prose/notes per domain from an existing ARCHITECTURE.md."""
    notes: dict[str, str] = {}
    matches = list(_DOMAIN_HEADING_RE.finditer(existing_text))
    risks_pos = existing_text.find("\n## 2.")
    if risks_pos == -1:
        risks_pos = len(existing_text)

    for idx, match in enumerate(matches):
        domain = match.group("domain").strip()
        slug = (match.group("slug") or _domain_slug(domain)).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else risks_pos
        block = existing_text[start:end]

        kept_lines: list[str] = []
        for line in block.splitlines():
            stripped = line.strip()
            if stripped == "Files (by size):":
                continue
            if stripped.startswith("- `") and (" lines, ~" in stripped or " line, ~" in stripped):
                continue
            if re.match(r"^-\s+…\s+\d+\s+more$", stripped):
                continue
            if stripped.startswith("Core symbols:"):
                continue
            kept_lines.append(line)

        cleaned = "\n".join(kept_lines).strip()
        if cleaned:
            notes[domain] = cleaned
            notes[slug] = cleaned
    return notes


def update_architecture_doc(existing_text: str, scan: dict, project: str = "") -> str:
    """Refresh domain metrics, hotspots, and unwired symbols while preserving user domain notes and Work Orders."""
    fresh = generate_architecture_skeleton(scan, project=project)
    notes = _extract_domain_notes(existing_text)

    # 1. Replace default TODO in fresh domain blocks with preserved user notes
    default_todo = (
        "TODO: what this domain is for, in two sentences, plus the "
        "invariants a refactor must not break."
    )
    if notes:
        rebuilt_domains: list[str] = []
        pos = 0
        for match in _DOMAIN_HEADING_RE.finditer(fresh):
            domain = match.group("domain").strip()
            slug = (match.group("slug") or _domain_slug(domain)).strip()
            next_heading = fresh.find("\n### 1.", match.end())
            risks_heading = fresh.find("\n## 2. Risks", match.end())
            candidates = [p for p in (next_heading, risks_heading) if p != -1]
            block_end = min(candidates) if candidates else len(fresh)

            rebuilt_domains.append(fresh[pos:match.start()])
            block = fresh[match.start():block_end]
            user_note = notes.get(domain) or notes.get(slug)
            if user_note:
                block = block.replace(default_todo, user_note, 1)
            rebuilt_domains.append(block)
            pos = block_end
        rebuilt_domains.append(fresh[pos:])
        fresh = "".join(rebuilt_domains)

    # 2. Preserve existing `## 3. Work Orders` (and anything after it) verbatim
    wo_marker = "\n## 3."
    old_wo_idx = existing_text.find(wo_marker)
    new_wo_idx = fresh.find(wo_marker)
    if old_wo_idx != -1 and new_wo_idx != -1:
        tail = existing_text[old_wo_idx:].rstrip("\n") + "\n"
        fresh = fresh[:new_wo_idx] + tail
    return fresh


HOOK_SCRIPT = """#!/usr/bin/env sh
# Managed by `atlas init --hook`: verify completed Work Order symbols & links before commit
atlas check
"""


def install_git_hook(root: Path, dry_run: bool = False) -> tuple[bool, str]:
    """Install or update `.git/hooks/pre-commit` to run `atlas check`."""
    hook_path = Path(root) / ".git" / "hooks" / "pre-commit"
    rel = ".git/hooks/pre-commit"
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        if "atlas check" in existing:
            return False, rel
        if not dry_run:
            suffix = ("" if existing.endswith("\n") else "\n") + "\n# atlas regression check\natlas check\n"
            hook_path.write_text(existing + suffix, encoding="utf-8")
            try:
                hook_path.chmod(0o755)
            except OSError:
                pass
        return True, f"{rel} (updated)"

    if not dry_run:
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(HOOK_SCRIPT, encoding="utf-8")
        try:
            hook_path.chmod(0o755)
        except OSError:
            pass
    return True, rel


AGENTS_TEMPLATE = """# Project guidelines: {name}

## Documentation

**Documentation is accessed through the `atlas` MCP server only:**

| Question | Tool |
|---|---|
| "Where is ...?" | `atlas_where("topic")` |
| Full section text | `atlas_read("<anchor>")` |
| Which work orders are done? | `atlas_status()` |
| Plan a new work order + steps | `atlas_plan("topic", steps=[...])` |
| Task brief (or next open WO) | `atlas_brief()` |
| Symbol/file in code | `atlas_code_find("name")` |

**Mandatory:**
- For every documentation question use `atlas_where` first — do not open the
  source files with Read/Grep and do not search the codebase.
- `.atlas/` is off-limits to file tools: the cache is overwritten on the next
  sync, and reading it directly costs tens of thousands of tokens.
- Work order status is a tool call, not a text edit: `atlas_publish` (new
  order) or `atlas_set_status` (tick an existing one off).
- No checkoff without green tests. Checkoff is the last step of a turn.
- Pass `verify_symbols=[...]` when closing: functions that are defined but
  never called do not count as delivered.
- A `✅` is a point-in-time acceptance. If a feature breaks later, the old
  order is not reopened — a new one is created.
"""

ANTIGRAVITY_TEMPLATE = """# Atlas rules

- **Documentation access is a tool call:** use the `atlas` MCP server
  (`atlas_where`, `atlas_read`, `atlas_status`, `atlas_code_find`). Never grep
  `.atlas/cache/` — the cache is overwritten on the next sync.
- **`.atlas/` is off-limits to file tools.**
- **Work order status is written by tool, not by editing:** `atlas_publish` for a
  new order, `atlas_set_status` to tick an existing one off.
- **No checkoff without green tests**, and pass `verify_symbols=[...]`: a symbol
  that is defined but never called is not delivered.
- **`✅` is a point-in-time acceptance.** A later regression becomes a new work
  order, never a reopened old one.
"""

MCP_TEMPLATE = """{{
  "mcpServers": {{
    "atlas": {{
      "command": "atlas",
      "args": ["serve"],
      "env": {{"ATLAS_ROOT": "."}}
    }}
  }}
}}
"""

GITIGNORE_LINES = (".atlas/", ".gdd/", "__pycache__/", "*.pyc")

MANAGED_FILE = "managed.json"


def managed_path(root) -> Path:
    from .config import cache_dir

    return cache_dir(root) / MANAGED_FILE


def load_managed(root) -> set[str] | None:
    """Return files created by `atlas init`. None = no managed list exists.

    `None` and `set()` mean different things: None means "this project has no
    managed concept, check everything" (legacy projects, Google Docs setups).
    An empty set would mean "nothing is managed" and would neuter `lint`.
    """
    import json

    path = managed_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    paths = data.get("paths") or []
    return {str(item).replace("\\", "/").lstrip("./") for item in paths}


def save_managed(root, paths: list[str]) -> Path:
    import json

    from .config import cache_dir

    target = cache_dir(root) / MANAGED_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = sorted({str(p).replace("\\", "/").lstrip("./") for p in paths})
    target.write_text(
        json.dumps(
            {"paths": normalized, "generator": "atlas init"},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return target


def _write_if_absent(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _ensure_gitignore(root: Path) -> bool:
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    missing = [line for line in GITIGNORE_LINES if line not in existing.splitlines()]
    if not missing:
        return False
    block = ("\n" if existing and not existing.endswith("\n") else "") + "\n".join(missing) + "\n"
    path.write_text(existing + block, encoding="utf-8")
    return True


def init_project(root, out: str | Path = "docs/ARCHITECTURE.md",
                 force: bool = False, dry_run: bool = False,
                 update: bool = False, hook: bool = False) -> dict:
    """Scan, generate doc skeleton, rule files, and index. Returns a report.

    `dry_run` shows what would be created and writes nothing — the only safe
    way to preview the tool on an unfamiliar repository.
    `update` refreshes metrics, hotspots, and unwired symbols in an existing
    architecture document while preserving domain notes and Work Orders.
    `hook` installs `.git/hooks/pre-commit` running `atlas check`.
    """
    root = Path(root).resolve()
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = root / out_path

    scan = scan_repository(root)
    created: list[str] = []
    updated: list[str] = []
    kept: list[str] = []
    would_create: list[str] = []

    rel_out = out_path.relative_to(root).as_posix()
    if out_path.exists() and update and not force:
        if dry_run:
            would_create.append(f"{rel_out} (updated)")
        else:
            existing_doc = out_path.read_text(encoding="utf-8")
            out_path.write_text(
                update_architecture_doc(existing_doc, scan, project=root.name),
                encoding="utf-8",
            )
            updated.append(rel_out)
    elif out_path.exists() and not force:
        kept.append(rel_out)
    elif dry_run:
        would_create.append(rel_out)
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            generate_architecture_skeleton(scan, project=root.name), encoding="utf-8"
        )
        created.append(rel_out)

    for name, content in (
        ("AGENTS.md", AGENTS_TEMPLATE.format(name=root.name)),
        (".antigravityrules", ANTIGRAVITY_TEMPLATE),
        (".mcp.json", MCP_TEMPLATE),
    ):
        path = root / name
        if path.exists():
            kept.append(name)
        elif dry_run:
            would_create.append(name)
        else:
            _write_if_absent(path, content)
            created.append(name)

    gitignore_missing = [
        line for line in GITIGNORE_LINES
        if line not in (
            (root / ".gitignore").read_text(encoding="utf-8").splitlines()
            if (root / ".gitignore").exists() else []
        )
    ]
    if gitignore_missing:
        if dry_run:
            would_create.append(f".gitignore (+{len(gitignore_missing)})")
        else:
            _ensure_gitignore(root)
            created.append(".gitignore (updated)")

    if hook:
        changed_hook, hook_label = install_git_hook(root, dry_run=dry_run)
        if changed_hook:
            if dry_run:
                would_create.append(hook_label)
            else:
                created.append(hook_label)
        else:
            kept.append(hook_label)

    report = {
        "root": str(root),
        "dry_run": dry_run,
        "files_scanned": scan["files"],
        "tokens": scan["tokens"],
        "domains": len(scan["domains"]),
        "domain_names": list(scan["domains"].keys())[:12],
        "hotspots": len(scan["hotspots"]),
        "unwired": len(scan["unwired"]),
        "document": str(out_path),
        "created": created,
        "updated": updated,
        "kept": kept,
    }
    if not dry_run:
        # `lint` needs to know later which documents atlas generated: only those
        # determine the exit code. Legacy docs are shown, not scored.
        # Existing entries are preserved.
        existing = load_managed(root) or set()
        relative = out_path.relative_to(root).as_posix()
        save_managed(root, sorted(existing | {relative}))
        report["managed"] = sorted(existing | {relative})
        created.append(f"{managed_path(root).relative_to(root).as_posix()}")
    if dry_run:
        report["would_create"] = would_create
    return report
