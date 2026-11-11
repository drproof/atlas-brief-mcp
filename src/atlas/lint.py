"""Structure check: find formatting issues that hurt GDD indexing and retrieval.

No judgment on content — only on structure. Findings serve as a cleanup
checklist for a one-time pass over the Google Doc or Markdown files.
"""

from __future__ import annotations

import json
import re
import sqlite3

LINE_WO_RE = re.compile(r"Work\s*Order\s*(\d+)", re.IGNORECASE)
# Same markers that `status.py` recognizes as a status. If `DONE`/`TODO`/`OPEN`
# were missing here, `lint` would flag a line as "missing status" even though
# `status.py` reads it as completed.
STATUS_RE = re.compile(
    r"✅|⬜|☑|☐|🚧|\bERLEDIGT\b|\bABGESCHLOSSEN\b|\bOFFEN\b|\bAUSSTEHEND\b|\bDONE\b|\bTODO\b|\bOPEN\b|\bIN[\s_-]+PROGRESS\b",
    re.IGNORECASE,
)
ESCAPED_RE = re.compile(r"\\[.\-_*\[\]#!|>~]")

MAX_ISSUES = 200


def _issue(kind: str, severity: str, row, detail: str, scope: str = "managed") -> dict:
    return {
        "type": kind,
        "severity": severity,
        "scope": scope,
        "anchor": row["anchor"],
        "path": row["path"],
        "line": row["line_start"],
        "heading": row["heading"][:70],
        "detail": detail,
    }


def _scope_of(path: str, managed: set[str] | None) -> str:
    """Return whether this file is managed by `atlas` or discovered legacy docs.

    Without a `managed` list, everything is managed: that is the state before
    `atlas init` and for Google Docs projects where there is only one source.
    With no configuration, behavior is unchanged.
    """
    if managed is None:
        return "managed"
    normalized = path.replace("\\", "/").lstrip("./")
    if normalized in managed:
        return "managed"
    # The sync cache is always managed: Google Docs are the source of truth.
    if normalized.startswith(".atlas/cache/") or normalized.startswith(".gdd/cache/"):
        return "managed"
    return "discovered"


def lint(conn: sqlite3.Connection, max_issues: int = MAX_ISSUES,
         managed: set[str] | None = None) -> str:
    rows = conn.execute(
        "SELECT doc_id, anchor, path, heading, level, body, chars, line_start, part, parts "
        "FROM sections ORDER BY doc_id, rowid"
    ).fetchall()

    issues: list[dict] = []
    counts: dict[str, int] = {}
    heading_seen: dict[str, int] = {}
    previous: dict[str, tuple[int, str]] = {}
    doc_paths: dict[str, str] = {}

    for index, row in enumerate(rows):
        doc = row["doc_id"]
        heading = row["heading"].strip()
        body = row["body"] or ""
        scope = _scope_of(row["path"] or "", managed)
        doc_paths.setdefault(doc, row["path"] or "")

        def add(kind: str, severity: str, detail: str) -> None:
            counts[kind] = counts.get(kind, 0) + 1
            if len(issues) < max_issues:
                issues.append(_issue(kind, severity, row, detail, scope))

        if not heading:
            add("empty_heading", "high",
                "Heading without text — an empty heading line in the Google Doc")

        if heading and row["part"] == 1:
            # Only part 1 counts: parts 2..n are continuations of the same
            # heading, not duplicates.
            key = f"{doc}::{heading.lower()}"
            heading_seen[key] = heading_seen.get(key, 0) + 1

        prev = previous.get(doc)
        if prev and prev[0] and row["level"] > prev[0] + 1:
            add("level_jump", "medium",
                f"Jump from H{prev[0]} to H{row['level']} ('{prev[1][:40]}' -> '{heading[:40]}')")
        previous[doc] = (row["level"], heading)

        next_level = None
        if index + 1 < len(rows) and rows[index + 1]["doc_id"] == doc:
            next_level = rows[index + 1]["level"]
        is_leaf = next_level is None or next_level <= row["level"]
        # Only check unsplit sections: parts 2..n are continuations and
        # naturally short.
        if (is_leaf and row["part"] == 1 and row["parts"] == 1
                and len(body.strip()) < 40 and heading
                and heading not in ("(Preamble)", "(Präambel)")):
            add("empty_section", "high",
                "Section without content and without subsections")

        if row["chars"] and row["chars"] > 5000:
            add("long_section", "medium",
                f"{row['chars']} chars — candidate for subheadings")

        if body.count("Tab ") >= 2 or body.count("`t.") >= 3:
            add("toc_line", "low",
                "Navigation/table-of-contents line competes with topic sections")

        for line in body.splitlines():
            if re.match(r"^\s*[-*]\s*\[[ xX]\]", line):
                continue
            match = LINE_WO_RE.search(line)
            if not match:
                continue
            # Only check real status entries: a separator must follow the number
            # ("Work Order 4 — …"). Mentions like "API from Work Order 4
            # (unit.gd)" are prose references, not malformed status lines.
            tail = line[match.end(): match.end() + 3].strip()
            if not re.match(r"^[—–:\-]", tail):
                continue
            window = line[match.end(): match.end() + 40]
            if not STATUS_RE.search(window):
                add("work_order_format", "high",
                    f"'Work Order {match.group(1)}' has a separator, but no status marker (✅/⬜)")
                break

        if ESCAPED_RE.search(body):
            add("export_escape", "low",
                "Export escapes like '1\\.' or '\\-' in text")

    duplicates = {key: value for key, value in heading_seen.items() if value > 1}
    for key, value in duplicates.items():
        doc, heading = key.split("::", 1)
        path = doc_paths.get(doc, "")
        counts["duplicate_heading"] = counts.get("duplicate_heading", 0) + value - 1
        if len(issues) < max_issues:
            issues.append({
                "type": "duplicate_heading",
                "severity": "low",
                "scope": _scope_of(path, managed),
                "anchor": "",
                "path": path,
                "line": 0,
                "heading": heading[:70],
                "detail": f"'{heading}' appears {value}× in {doc} — anchors will be numbered",
            })

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda item: (order.get(item["severity"], 3), item["type"]))

    managed_issues = [item for item in issues if item.get("scope") == "managed"]
    payload = {
        "sections": len(rows),
        "counts": dict(sorted(counts.items(), key=lambda item: -item[1])),
        "issues": issues,
        # Only documents managed by atlas itself determine the exit code.
        # Discovered legacy docs are reported, but do not fail acceptance.
        "ok": len(managed_issues) == 0,
        "managed_issues": len(managed_issues),
        "discovered_issues": len(issues) - len(managed_issues),
        "truncated": len(issues) >= max_issues,
    }
    return json.dumps(payload, ensure_ascii=False)
