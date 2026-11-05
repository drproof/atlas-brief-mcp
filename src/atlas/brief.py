"""Task-Brief: baut aus dem Index einen fertigen Delegations-Prompt.

Kein Modell, keine Halluzination — nur echte Suchtreffer, echte Code-Zuordnungen
und die Projekt-Leitplanken. Funktioniert als CLI (Copy-Paste in jeden Chat)
und als MCP-Tool.
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

# Code-Pfade stehen im GDD meist im Fließtext ("scripts/camp_hub.gd"), nicht im
# Frontmatter. Deshalb werden sie aus den Trefferabschnitten extrahiert.
FILE_RE = re.compile(
    r"(?:res://)?(?:scripts|scenes|shaders|assets|quests|data|tools|addons)"
    r"/[\w./-]+\.(?:gd|tscn|tres|json|gdshader|glsl)",
    re.IGNORECASE,
)


def _parse_list(raw: str) -> list[str]:
    """Frontmatter-Listen wurden als str() abgelegt — beide Formen akzeptieren."""
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
    """Projekt-Leitplanken — AGENTS.md hat Vorrang vor Altlasten.

    Eine daneben liegende alte `rules.md` darf AGENTS.md nicht verdrängen: sie
    enthielt überholte Timeouts und keine GDD-Pflicht. Der Agent hätte dann nach
    Regeln gearbeitet, die seit Wochen falsch sind.
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
    """Nur der GDD-Teil der Leitplanken — der Rest steht ohnehin in AGENTS.md.

    Geschnitten wird am ersten `## 1.`-Abschnitt, aber nur wenn davor ein
    `## 0.` steht (das ist der GDD-Block). Sonst ist die Datei flach und wird
    nur am Längenlimit gekürzt — sonst bliebe von ihr nichts übrig.
    """
    match = re.search(r"\n##\s*1\.", text)
    if match and match.start() > 0 and re.search(r"\n##\s*0\.", text[: match.start()]):
        text = text[: match.start()]
    if len(text) > RULES_LIMIT:
        cut = text.rfind("\n", 0, RULES_LIMIT)
        text = text[: cut if cut > 0 else RULES_LIMIT]
        text += "\n\n_… weitere Regeln siehe AGENTS.md._"
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
    """Erzeugt den Brief als Markdown."""
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
            f"*{origin}{trail}* · Anker: `{base}`\n\n"
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
        f"# Task-Brief: {goal}",
        "",
        f"_Erzeugt aus dem GDD-Index am {date.today().isoformat()}. "
        f"Anker sind stabil — mit `atlas_read(<anker>)` jederzeit nachlesbar._",
        "",
        f"## 1. Relevante GDD-Abschnitte ({len(sections)})",
        "",
    ]
    if sections:
        lines.extend(sections)
    else:
        lines.append(
            "_Kein Treffer im GDD. Prüfe die Formulierung oder nutze `atlas_outline`._\n"
        )

    lines.extend(["## 2. Betroffene Dateien", ""])
    if files:
        lines.extend(f"- `{item}`" for item in files)
    else:
        lines.append("_Keine `implements:`-Zuordnung im GDD hinterlegt — im Code selbst suchen._")
    lines.append("")

    if include_rules:
        rules = _load_rules(root)
        if rules:
            lines.extend(["## 3. Leitplanken (unverändert gültig)", "", rules, ""])

    lines.extend([
        "## 4. Arbeitsauftrag",
        "",
        "- [ ] Ziel in prüfbare Schritte zerlegen",
        "- [ ] Betroffene Dateien aus Abschnitt 2 zuerst lesen",
        "- [ ] Nach der Änderung verifizieren (Test/Start), Ergebnis zitieren",
        "",
    ])

    if work_order:
        lines.extend([
            f"## 5. Abschluss (Pflicht — Work Order {work_order})",
            "",
            "Status ins Google Doc eintragen. **Nicht** selbst ins Doc schreiben:",
            "",
            "```",
            f'atlas publish --wo {work_order} --title "<Titel>" '
            '--desc "<ein Satz: was implementiert wurde>"',
            "```",
            "",
            "`verified: true` in der Antwort ist der Nachweis. Fehlt das Feld,",
            "ist die Aufgabe **nicht** dokumentiert — auch wenn der Code fertig ist.",
            "",
        ])
    else:
        lines.extend([
            "## 5. Abschluss",
            "",
            "Status ins Doc eintragen (MCP: `atlas_publish`, CLI: `atlas publish --wo <N> …`).",
            "**Nicht** selbst ins Doc schreiben. `verified: true` ist der Nachweis.",
            "",
        ])

    lines.extend([
        "> Bei Unklarheiten den Anker aus Abschnitt 1 mit `atlas_read` nachlesen,",
        "> statt die GDD-Datei vollständig zu öffnen.",
        "",
    ])

    text = "\n".join(lines)
    if len(text) > max_chars * 2:
        text = text[: max_chars * 2].rstrip() + "\n\n_… Brief gekürzt._\n"
    return text
