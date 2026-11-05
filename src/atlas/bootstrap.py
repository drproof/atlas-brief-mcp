"""`atlas init`: ein Repository ohne Doku in einen indexierbaren Zustand bringen.

Kein LLM, kein Netzwerk — ein Dateisystem-Scan, eine Tokenschätzung und der
bestehende Symbol-Scanner. Das Ergebnis ist ein Markdown-Dokument, das `lint`
fehlerfrei besteht, plus die Regeldateien, die ein Coding-Agent braucht.

Grundsatz: **nichts überschreiben.** Wer eine `AGENTS.md` hat, behält sie.
`init` ergänzt Lücken und meldet, was schon da war — ein Bootstrap, der
bestehende Arbeit plattmacht, wird genau einmal benutzt.
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
TOKENS_PER_CHAR = 3.5  # konservative Schätzung; konsistent mit dem Rest des Tools


def estimate_tokens(text: str) -> int:
    return round(len(text) / TOKENS_PER_CHAR)


def iter_source_files(root: Path):
    """Quelldateien, ohne in gesperrte oder riesige Ordner abzusteigen.

    Bewusst `os.walk` statt `rglob`: `rglob` wirft bei einem einzigen
    unlesbaren Verzeichnis eine Exception und bricht den ganzen Scan ab. Ein
    Bootstrap, der an einem fremden `node_modules` scheitert, ist unbrauchbar.
    """
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.endswith(SOURCE_EXTS):
                yield Path(dirpath) / name


SKIP_KEYWORDS = {
    # Kontrollstrukturen — sehen wie Methoden aus, sind keine
    "if", "for", "while", "switch", "catch", "else", "do", "try", "with",
    "return", "function", "constructor", "super", "this", "new", "typeof",
    # Godot-Virtuals — die Engine ruft sie
    "_ready", "_init", "_process", "_physics_process", "_input",
    "_unhandled_input", "_unhandled_key_input", "_enter_tree", "_exit_tree",
    "_draw", "_notification", "_to_string", "__init__", "__str__", "__repr__",
    # Framework-Hooks, die das Framework ruft
    "render", "ngOnInit", "ngOnDestroy", "componentDidMount", "componentWillUnmount",
}
DEF_PATTERNS = (
    # GDScript / Python / Rust
    re.compile(
        r"^[ \t]*(?:static[ \t]+)?(?:func|def|fn)[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*\(",
        re.MULTILINE,
    ),
    # JS/TS: [export [default]] [async] function name( — die schließende Klammer
    # darf auf einer späteren Zeile stehen (mehrzeilige TS-Signaturen).
    re.compile(
        r"^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*"
        r"(?P<name>[A-Za-z_$][\w$]*)[ \t]*\(",
        re.MULTILINE,
    ),
    # JS/TS: [export] const name = [Wrapper<…>(] [async] (…) =>  |  = function(
    # Der Wrapper-Part deckt React.memo(, forwardRef<…>( und ähnliche Higher-Order-
    # Komponenten ab — sonst fehlen ausgerechnet die komplexen UI-Komponenten.
    re.compile(
        r"^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?(?:const|let|var)[ \t]+"
        r"(?P<name>[A-Za-z_$][\w$]*)[ \t]*(?::[^=\n]+)?=[ \t]*"
        r"(?:[A-Za-z_$][\w$.]*(?:<[^>\n]*>)?\([ \t]*)?"
        r"(?:async[ \t]+)?(?:\([^)\n]*\)[^\n=]*?=>|function[ \t]*\()",
        re.MULTILINE,
    ),
)

# Dateien, die ein Bundler oder Router von außen mountet: `export default`
# braucht dort keinen Aufruf im eigenen Code.
ENTRY_FILES = {
    "app.tsx", "app.jsx", "app.ts", "app.js",
    "main.tsx", "main.jsx", "main.ts", "main.js",
    "index.tsx", "index.jsx", "index.ts", "index.js",
    "page.tsx", "page.jsx", "layout.tsx", "layout.jsx",
    "route.ts", "route.tsx", "not-found.tsx", "error.tsx", "loading.tsx",
}

# Ab dieser Rumpflänge gilt eine Funktion als Kern-Symbol einer Domäne.
CORE_SYMBOL_MIN_LINES = 8


def _iter_defs(text: str):
    """(name, startzeile, definitionszeile) für alle erkannten Funktionen."""
    seen: set[tuple[str, int]] = set()
    for pattern in DEF_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group("name")
            if not name or name in SKIP_KEYWORDS:
                continue
            start = text.count("\n", 0, match.start())
            key = (name, start)
            if key in seen:
                continue
            seen.add(key)
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.start())
            line = text[line_start: line_end if line_end != -1 else len(text)]
            yield name, start, line


def _func_body_end(lines: list[str], start: int) -> int:
    """Letzte Zeile des Rumpfs, der in `start` beginnt (0-basiert).

    Maßstab ist die Einrückung der **Definitionszeile**, nicht Spalte 0. Der
    frühere Spalte-0-Test war nur für Top-Level-Code richtig: In einer
    Python-Klasse steht keine Methode auf Spalte 0, also lief jede Methode bis
    zum Ende der ganzen Klasse und galt als beliebig lang.
    """
    line = lines[start]
    def_indent = len(line) - len(line.lstrip())
    end = start
    for offset in range(start + 1, len(lines)):
        current = lines[offset]
        if not current.strip():
            continue
        indent = len(current) - len(current.lstrip())
        stripped = current.strip()
        if indent <= def_indent:
            # Fortsetzung einer mehrzeiligen Signatur — erst danach beginnt der
            # Rumpf. `): RevenueResult {`, `) -> dict:`, `) => {`, oder Allman-
            # Stil mit `{` auf eigener Zeile.
            if stripped.startswith(")") or stripped in ("{", "):"):
                end = offset
                continue
            # Die schließende Klammer einer TS/C#-Methode gehört noch dazu.
            if stripped in ("}", "};", "});") and indent == def_indent:
                end = offset
            break
        end = offset
    return end


def _has_decorator(lines: list[str], start: int) -> bool:
    """Steht über der Definition ein Dekorator — auch ein mehrzeiliger?

    `@app.post(\\n "/api",\\n)\\ndef handler():` — die Zeile direkt über `def`
    ist `)`. Wer nur `lines[start-1]` prüft, hält den Endpunkt für unverdrahtet.
    """
    line = lines[start]
    def_indent = len(line) - len(line.lstrip())
    index = start - 1
    while index >= 0:
        current = lines[index]
        if not current.strip():
            return False  # Leerzeile trennt Dekorator und Definition
        if current.lstrip().startswith("@"):
            return True
        if len(current) - len(current.lstrip()) < def_indent:
            return False
        index -= 1
    return False


def _core_symbols(text: str, limit: int = 12) -> list[str]:
    """Die größten Funktionen einer Datei — nicht die ersten.

    Die ersten zwölf `func`-Zeilen einer GDScript-Datei sind `_ready`, `get_int`,
    `get_float`, `next_int`: Virtuals und triviale Getter. Als „Kern-Symbole"
    sind sie wertlos. Interessant ist, wo Logik steht, und die steht in den
    langen Funktionen.
    """
    lines = text.splitlines()
    sized: list[tuple[int, str]] = []
    for name, start, _line in _iter_defs(text):
        length = _func_body_end(lines, start) - start + 1
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
    """Trägt dieser Ordner ein eigenes Projekt? Dann ist er eine Teilkopie.

    Ein Unterordner mit `project.godot`, `package.json` oder `.git` ist ein
    eigenes Repository. Aufrufer außerhalb davon sind für den Scanner
    unsichtbar — „unverdrahtet" ist dort eine schwächere Aussage, und das muss
    in der Doku stehen, bevor jemand auf Verdacht löscht.
    """
    for marker in SUBPROJECT_MARKERS:
        if (domain_dir / marker).exists():
            return marker
    # Eine Ebene tiefer: `games/dungeon_quest/project.godot`
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
    """Dateien nach Domäne, Tokens, Hotspots und unverdrahtete Symbole."""
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
        # Die Symbole der größten Datei zuerst, damit die Domänenliste die
        # Logik zeigt und nicht zwölf Getter aus einer Zufallsdatei.
        for name in _core_symbols(text, limit=6):
            if name not in bucket["symbols"] and len(bucket["symbols"]) < 12:
                bucket["symbols"].append(name)

    for bucket in domains.values():
        bucket["files"].sort(key=lambda item: -item["tokens"])
        bucket["subproject"] = _detect_subproject(bucket["root"])
        # Kern-Symbole aus den größten Dateien der Domäne, nicht aus den
        # erstbesten: In einer 60-Zeilen-Hilfsdatei steht kein Kern.
        texts = {rel: text for rel, text, _t in sources}
        symbols: list[str] = []
        for item in bucket["files"][:5]:
            for name in _core_symbols(texts.get(item["path"], ""), limit=4):
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
    """Funktionen > 15 Zeilen, deren Name nirgends sonst im Code auftaucht.

    Ein Durchlauf über den Gesamttext pro Symbol: `str.count` läuft in C, damit
    bleibt der Scan auch bei hundert Dateien unter einer Sekunde.

    Zwei Anbindungsarten, die kein Aufruf sind und trotzdem zählen:

    - **Test-Konvention**: `test_foo` wird von pytest über den Namen gefunden,
      nie aufgerufen. 15 von 20 Treffern in einem echten Python-Projekt waren
      solche Funktionen — eine Warnliste, die zu 75 % falsch ist, liest niemand.
    - **Dekorator-Registrierung**: `@mcp.tool()`, `@app.route(...)`,
      `@pytest.fixture` hängen die Funktion an ein Framework. Sie steht dann
      nirgends als Aufruf im Code.
    """
    haystack = "\n".join(text for _rel, text, _t in sources)
    findings: list[dict] = []
    seen: set[str] = set()

    for rel, text, _tokens in sources:
        lines = text.splitlines()
        is_entry = Path(rel).name.lower() in ENTRY_FILES
        for name, start, def_line in _iter_defs(text):
            if name in seen or len(name) < 4:
                continue
            # Test-Konventionen (pytest, unittest, JS-Test-Runner)
            if name.startswith("test_") or name.startswith("Test") or name.endswith("_test"):
                continue
            # Ein `export default` in App.tsx/page.tsx/route.ts mountet der
            # Bundler oder Router von außen — kein fehlender Aufrufer.
            if is_entry and "export default" in def_line:
                continue
            # Dekorator über der Definition (auch mehrzeilig) = an ein Framework
            # gebunden, kein fehlender Aufrufer.
            if _has_decorator(lines, start):
                continue
            length = _func_body_end(lines, start) - start + 1
            if length < UNWIRED_MIN_LINES:
                continue
            seen.add(name)
            # Der Name selbst zählt einmal (die Definition). Mehr heißt: benutzt.
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


AGENTS_TEMPLATE = """# Project guidelines: {name}

## Documentation

**Documentation is accessed through the `atlas` MCP server only:**

| Question | Tool |
|---|---|
| "Where is ...?" | `atlas_where("topic")` |
| Full section text | `atlas_read("<anchor>")` |
| Which work orders are done? | `atlas_status()` |
| Task brief for a subagent | `atlas_brief("task")` |
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
    """Dateien, die `atlas init` erzeugt hat. None = keine Liste vorhanden.

    `None` und `set()` bedeuten Verschiedenes: None heißt „dieses Projekt kennt
    kein managed-Konzept, prüfe alles" (Altprojekte, Google-Docs-Setups).
    Ein leeres Set hieße „nichts ist verwaltet" und würde lint entwurzeln.
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
                 force: bool = False, dry_run: bool = False) -> dict:
    """Scan, Doku-Gerüst, Regeldateien, Index. Liefert einen Bericht.

    `dry_run` zeigt, was entstehen würde, und schreibt nichts — der einzige
    Weg, das Tool gefahrlos an einem fremden Repository auszuprobieren.
    """
    root = Path(root).resolve()
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = root / out_path

    scan = scan_repository(root)
    created: list[str] = []
    kept: list[str] = []
    would_create: list[str] = []

    if out_path.exists() and not force:
        kept.append(out_path.relative_to(root).as_posix())
    elif dry_run:
        would_create.append(out_path.relative_to(root).as_posix())
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            generate_architecture_skeleton(scan, project=root.name), encoding="utf-8"
        )
        created.append(out_path.relative_to(root).as_posix())

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
        "kept": kept,
    }
    if not dry_run:
        # `lint` muss später wissen, welche Dokumente atlas erzeugt hat: nur die
        # entscheiden über den Exit-Code. Fremde Alt-Doku wird gezeigt, nicht
        # gewertet. Vorhandene Einträge bleiben erhalten.
        existing = load_managed(root) or set()
        relative = out_path.relative_to(root).as_posix()
        save_managed(root, sorted(existing | {relative}))
        report["managed"] = sorted(existing | {relative})
        created.append(f"{managed_path(root).relative_to(root).as_posix()}")
    if dry_run:
        report["would_create"] = would_create
    return report
