"""Struktur-Prüfung: findet, was das GDD für den Index schwer lesbar macht.

Kein Urteil über Inhalte — nur über Form. Die Befunde sind die Arbeitsliste
für eine einmalige Sanierung des Google Docs.
"""

from __future__ import annotations

import json
import re
import sqlite3

LINE_WO_RE = re.compile(r"Work\s*Order\s*(\d+)", re.IGNORECASE)
STATUS_RE = re.compile(r"✅|⬜|☑|☐|ERLEDIGT|OFFEN", re.IGNORECASE)
ESCAPED_RE = re.compile(r"\\[.\-_*\[\]#!|>~]")

MAX_ISSUES = 200


def _issue(kind: str, severity: str, row, detail: str) -> dict:
    return {
        "type": kind,
        "severity": severity,
        "anchor": row["anchor"],
        "path": row["path"],
        "line": row["line_start"],
        "heading": row["heading"][:70],
        "detail": detail,
    }


def lint(conn: sqlite3.Connection, max_issues: int = MAX_ISSUES) -> str:
    rows = conn.execute(
        "SELECT doc_id, anchor, path, heading, level, body, chars, line_start, part, parts "
        "FROM sections ORDER BY doc_id, rowid"
    ).fetchall()

    issues: list[dict] = []
    counts: dict[str, int] = {}
    heading_seen: dict[str, int] = {}
    previous: dict[str, tuple[int, str]] = {}

    for index, row in enumerate(rows):
        doc = row["doc_id"]
        heading = row["heading"].strip()
        body = row["body"] or ""

        def add(kind: str, severity: str, detail: str) -> None:
            counts[kind] = counts.get(kind, 0) + 1
            if len(issues) < max_issues:
                issues.append(_issue(kind, severity, row, detail))

        if not heading:
            add("empty_heading", "hoch",
                "Überschrift ohne Text — im Google Doc eine leere Überschriftenzeile")

        if heading and row["part"] == 1:
            # Nur der erste Teil zählt: part 2..n sind Fortsetzungen derselben
            # Überschrift, keine Duplikate.
            key = f"{doc}::{heading.lower()}"
            heading_seen[key] = heading_seen.get(key, 0) + 1

        prev = previous.get(doc)
        if prev and prev[0] and row["level"] > prev[0] + 1:
            add("level_jump", "mittel",
                f"Sprung von H{prev[0]} auf H{row['level']} ('{prev[1][:40]}' -> '{heading[:40]}')")
        previous[doc] = (row["level"], heading)

        next_level = None
        if index + 1 < len(rows) and rows[index + 1]["doc_id"] == doc:
            next_level = rows[index + 1]["level"]
        is_leaf = next_level is None or next_level <= row["level"]
        # Nur ungesplittete Abschnitte prüfen: part 2..n sind Fortsetzungen und
        # naturgemäß kurz.
        if (is_leaf and row["part"] == 1 and row["parts"] == 1
                and len(body.strip()) < 40 and heading and heading != "(Präambel)"):
            add("empty_section", "hoch",
                "Abschnitt ohne Inhalt und ohne Unterabschnitte")

        if row["chars"] and row["chars"] > 5000:
            add("long_section", "mittel",
                f"{row['chars']} Zeichen — Kandidat für Untergliederung")

        if body.count("Tab ") >= 2 or body.count("`t.") >= 3:
            add("toc_line", "niedrig",
                "Navigations-/Inhaltsverzeichnis-Zeile konkurriert mit Themenseiten")

        for line in body.splitlines():
            match = LINE_WO_RE.search(line)
            if not match:
                continue
            # Nur echte Status-Einträge prüfen: nach der Nummer muss ein Trenner
            # folgen ("Work Order 4 — …"). Erwähnungen wie "API aus Work Order 4
            # (unit.gd)" sind keine Statusangabe und kein Formfehler.
            tail = line[match.end(): match.end() + 3].strip()
            if not re.match(r"^[—–:\-]", tail):
                continue
            window = line[match.end(): match.end() + 40]
            if not STATUS_RE.search(window):
                add("work_order_format", "hoch",
                    f"'Work Order {match.group(1)}' mit Trenner, aber ohne Status-Marker (✅/⬜)")
                break

        if ESCAPED_RE.search(body):
            add("export_escape", "niedrig",
                "Export-Escapes wie '1\\.' oder '\\-' im Text")

    duplicates = {key: value for key, value in heading_seen.items() if value > 1}
    for key, value in duplicates.items():
        doc, heading = key.split("::", 1)
        counts["duplicate_heading"] = counts.get("duplicate_heading", 0) + value - 1
        if len(issues) < max_issues:
            issues.append({
                "type": "duplicate_heading",
                "severity": "niedrig",
                "anchor": "",
                "path": "",
                "line": 0,
                "heading": heading[:70],
                "detail": f"'{heading}' kommt {value}× in {doc} vor — Anker werden durchnummeriert",
            })

    order = {"hoch": 0, "mittel": 1, "niedrig": 2}
    issues.sort(key=lambda item: (order.get(item["severity"], 3), item["type"]))

    payload = {
        "sections": len(rows),
        "counts": dict(sorted(counts.items(), key=lambda item: -item[1])),
        "issues": issues,
        "truncated": len(issues) >= max_issues,
    }
    return json.dumps(payload, ensure_ascii=False)
