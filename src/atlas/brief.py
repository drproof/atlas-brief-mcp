"""Task brief: build a ready-to-use delegation prompt from the index.

No model, no hallucination — only real search hits, real code mappings,
and project guardrails. Works both as a CLI (copy-paste into any chat)
and as an MCP tool.
"""

from __future__ import annotations

from .config import cache_dir

import ast
import json
import re
from datetime import date
from pathlib import Path

from . import search as search_mod

MAX_SECTIONS = 5
SECTION_CHARS = 1600
MAX_CHARS = 8000

# Code paths in the GDD usually appear inline in prose ("scripts/camp_hub.gd"),
# not in frontmatter. Extract them from the matched sections.
FILE_RE = re.compile(
    r"(?:res://)?(?:scripts|scenes|shaders|assets|quests|data|tools|addons)"
    r"/[\w./-]+\.(?:gd|tscn|tres|json|gdshader|glsl)",
    re.IGNORECASE,
)


def _parse_list(raw: str) -> list[str]:
    """Frontmatter lists are stored via str() — accept both forms."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("["):
        for parse in (json.loads, ast.literal_eval):
            try:
                value = parse(text)
                if isinstance(value, list):
                    return [str(item) for item in value if str(item).strip()]
            except Exception:
                continue
    return [part.strip() for part in text.split(",") if part.strip()]


RULES_LIMIT = 2600


def _load_rules(root: Path) -> str:
    """Load project guardrails — AGENTS.md takes precedence over legacy files.

    An older `rules.md` sitting next to it must not shadow `AGENTS.md`: it
    contained outdated timeouts and no GDD requirement, causing the agent to
    follow rules that had been wrong for weeks.
    """
    for candidate in (root / "AGENTS.md", cache_dir(root) / "rules.md",
                      root / "rules.md", root / "CLAUDE.md"):
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                return _trim_rules(text)
    return ""


def _trim_rules(text: str) -> str:
    """Keep only the GDD portion of the guardrails — the rest is in AGENTS.md anyway.

    Cut at the first `## 1.` section, but only when preceded by a `## 0.`
    section (the GDD block). Otherwise the file is flat and is only truncated
    at the length limit — otherwise nothing would remain of it.
    """
    match = re.search(r"\n##\s*1\.", text)
    if match and match.start() > 0 and re.search(r"\n##\s*0\.", text[: match.start()]):
        text = text[: match.start()]
    if len(text) > RULES_LIMIT:
        cut = text.rfind("\n", 0, RULES_LIMIT)
        text = text[: cut if cut > 0 else RULES_LIMIT]
        text += "\n\n_… see AGENTS.md for further rules._"
    return text.strip()


def build(
    conn,
    root: Path,
    goal: str,
    max_sections: int = MAX_SECTIONS,
    max_chars: int = MAX_CHARS,
    include_rules: bool = True,
    work_order: int | None = None,
    extra_files: list[str] | None = None,
) -> str:
    """Build the task brief as Markdown."""
    hits = search_mod.run_search(conn, goal, k=max_sections * 3)

    selected: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        base = hit["anchor"].split("~")[0]
        if base in seen:
            continue
        seen.add(base)
        selected.append(hit)
        if len(selected) >= max_sections:
            break

    budget = max_chars
    sections: list[str] = []
    doc_ids: set[str] = set()
    file_hits: list[str] = []
    for hit in selected:
        base = hit["anchor"].split("~")[0]
        payload = json.loads(search_mod.read(conn, base, max_chars=min(SECTION_CHARS, max(300, budget))))
        budget -= payload["chars"]
        doc_ids.add(hit["doc_id"])
        for found in FILE_RE.finditer(payload["body"]):
            path = found.group(0).replace("res://", "")
            if path not in file_hits:
                file_hits.append(path)
        origin = f"{hit['path']}:{hit['line']}"
        trail = f" — {hit['breadcrumb']}" if hit["breadcrumb"] else ""
        sections.append(
            f"### {payload['heading']}\n"
            f"*{origin}{trail}* · anchor: `{base}`\n\n"
            f"{payload['body']}\n"
        )
        if budget <= 0:
            break

    files: list[str] = list(extra_files or [])
    for item in file_hits:
        if item not in files:
            files.append(item)
    for doc_id in sorted(doc_ids):
        row = conn.execute("SELECT implements FROM docs WHERE id=?", (doc_id,)).fetchone()
        if row is None:
            continue
        for item in _parse_list(row["implements"]):
            if item not in files:
                files.append(item)

    lines: list[str] = [
        f"# Task brief: {goal}",
        "",
        f"_Generated from the documentation index on {date.today().isoformat()}. "
        f"Anchors are stable — re-read them any time with `atlas_read(<anchor>)`._",
        "",
        f"## 1. Relevant sections ({len(sections)})",
        "",
    ]
    if sections:
        lines.extend(sections)
    else:
        lines.append(
            "_No match in the documentation. Try different wording, or use `atlas_outline`._\n"
        )

    lines.extend(["## 2. Affected files", ""])
    if files:
        lines.extend(f"- `{item}`" for item in files)
    else:
        lines.append("_No `implements:` mapping in the docs — search the code yourself._")
    lines.append("")

    if include_rules:
        rules = _load_rules(root)
        if rules:
            lines.extend(["## 3. Guardrails (still in force)", "", rules, ""])

    lines.extend([
        "## 4. Work order",
        "",
        "- [ ] Break the goal into checkable steps",
        "- [ ] Read the affected files from section 2 first",
        "- [ ] Verify after the change (test/run) and quote the result",
        "",
    ])

    if work_order:
        lines.extend([
            f"## 5. Closing (mandatory — work order {work_order})",
            "",
            "Record the status through the tool. Do **not** edit the document yourself:",
            "",
            "```",
            f'atlas publish --wo {work_order} --title "<title>" '
            '--desc "<one sentence: what was implemented>"',
            "```",
            "",
            "`verified: true` in the response is the proof. If the field is missing,",
            "the task is **not** documented — even if the code is finished.",
            "",
        ])
    else:
        lines.extend([
            "## 5. Closing",
            "",
            "Record the status through the tool (MCP: `atlas_publish`, CLI: `atlas publish --wo <N> …`).",
            "Do **not** edit the document yourself. `verified: true` is the proof.",
            "",
        ])

    lines.extend([
        "> If anything is unclear, re-read the anchor from section 1 with `atlas_read`",
        "> instead of opening the whole documentation file.",
        "",
    ])

    text = "\n".join(lines)
    if len(text) > max_chars * 2:
        text = text[: max_chars * 2].rstrip() + "\n\n_… brief truncated._\n"
    return text
