"""`atlas measure`: what does a question cost with and without the index?

The numbers in the README come from a one-off field benchmark and cannot be
verified by a stranger on faith. This module runs the same measurement on **the
user's own project**: per question, "read everything" vs. "where + read".

Honesty rules enforced by the code:

* The naive baseline is the **entire** doc set, not a single section. An agent
  without an index does not know where the answer lives and must read it all.
* If `where` finds nothing, that is a miss, not a saving. Such questions are
  counted as `missed` and excluded from the savings calculation.
* Auto-generated questions include the section heading and therefore part of
  the answer. The report states this caveat instead of presenting the number
  as an independent benchmark.
"""

from __future__ import annotations

import json
import sqlite3

from pathlib import Path

from . import bootstrap, code as code_mod, search as search_mod

CHARS_PER_TOKEN = 4


def _tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def document_tokens(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return (total tokens across all documents, document count) — the naive baseline."""
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(body)), 0), COUNT(DISTINCT doc_id) FROM sections"
    ).fetchone()
    return max(0, row[0] // CHARS_PER_TOKEN), row[1]


def auto_questions(conn: sqlite3.Connection, limit: int = 8) -> list[str]:
    """Generate questions from the largest sections — as a self-test of the index.

    Deliberately picks the longest sections: they hold the most information,
    which is where a full-text read hurts the most.
    """
    rows = conn.execute(
        "SELECT heading FROM sections "
        "ORDER BY LENGTH(body) DESC LIMIT ?", (limit * 3,)
    ).fetchall()
    questions: list[str] = []
    seen: set[str] = set()
    for row in rows:
        heading = row["heading"]
        if not heading or heading in seen:
            continue
        seen.add(heading)
        questions.append(f"Where is {heading} described?")
        if len(questions) >= limit:
            break
    return questions


def measure_code(root, sample: int = 8) -> dict:
    """Compare reading full source files against `atlas_code_find` for the largest symbols in the repo."""
    if not root:
        return {"files": 0, "codebase_tokens": 0, "rows": [], "naive_total": 0, "atlas_total": 0, "saved": 0.0}
    root_path = Path(root)
    if not root_path.exists():
        return {"files": 0, "codebase_tokens": 0, "rows": [], "naive_total": 0, "atlas_total": 0, "saved": 0.0}

    candidates: list[dict] = []
    total_files = 0
    codebase_tokens = 0
    for path in sorted(bootstrap.iter_source_files(root_path)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total_files += 1
        file_tok = _tokens(text)
        codebase_tokens += file_tok
        rel = path.relative_to(root_path).as_posix()
        lines = text.splitlines()
        is_test_file = rel.startswith("tests/") or Path(rel).name.startswith("test_")
        for name, start, _def_line in bootstrap._iter_defs(text):
            if len(name) < 4:
                continue
            if is_test_file or name.startswith("test_") or name.startswith("Test") or name.endswith("_test"):
                continue
            body_len = bootstrap._func_body_end(lines, start) - start + 1
            candidates.append({
                "symbol": name,
                "path": rel,
                "file_tokens": file_tok,
                "lines": body_len,
            })

    # Pick symbols from the largest files first (and largest functions within them), deduplicated by symbol name
    candidates.sort(key=lambda item: (-item["file_tokens"], -item["lines"]))
    picked: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        if item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        picked.append(item)
        if len(picked) >= sample:
            break

    rows: list[dict] = []
    naive_total = 0
    atlas_total = 0
    for item in picked:
        rendered = code_mod.render(root_path, item["symbol"], limit=5, with_usage=True)
        cost = _tokens(rendered)
        # Naive baseline: reading the defining file (or at minimum the codebase if multi-file caller check)
        naive = max(item["file_tokens"], cost)
        naive_total += naive
        atlas_total += cost
        rows.append({
            "symbol": item["symbol"],
            "path": item["path"],
            "naive": naive,
            "atlas": cost,
            "saved": round(1 - cost / naive, 3) if naive else 0.0,
        })

    return {
        "files": total_files,
        "codebase_tokens": codebase_tokens,
        "symbols": len(rows),
        "naive_total": naive_total,
        "atlas_total": atlas_total,
        "saved": round(1 - atlas_total / naive_total, 3) if naive_total else 0.0,
        "rows": rows,
    }


def measure(root, conn, questions: list[str] | None = None, sample: int = 8) -> dict:
    """Compare the naive full-read path against the indexed path for each question (and code symbols)."""
    baseline, docs = document_tokens(conn)
    generated = questions is None
    queries = (questions or auto_questions(conn, limit=sample)) or []

    rows = []
    naive_total = 0
    atlas_total = 0
    missed = 0

    for question in queries:
        hit = json.loads(search_mod.where(conn, question))
        if not hit.get("found"):
            missed += 1
            rows.append({"question": question, "found": False})
            continue

        text = search_mod.read(conn, hit["anchor"], max_chars=3000)
        cost = _tokens(text)

        naive_total += baseline
        atlas_total += cost
        rows.append({
            "question": question,
            "found": True,
            "anchor": hit["anchor"],
            "path": hit["path"],
            "heading": hit["heading"],
            "naive": baseline,
            "atlas": cost,
            "saved": round(1 - cost / baseline, 3) if baseline else 0.0,
        })

    answered = len(rows) - missed
    code_report = measure_code(root, sample=sample)
    return {
        "documents": docs,
        "baseline_tokens": baseline,
        "questions": len(queries),
        "answered": answered,
        "missed": missed,
        "naive_total": naive_total,
        "atlas_total": atlas_total,
        "saved": round(1 - atlas_total / naive_total, 3) if naive_total else 0.0,
        "rows": rows,
        "code": code_report,
        "generated_questions": generated,
        "caveat": (
            "Questions were derived from section headings, so each one contains "
            "part of its own answer. Pass --questions FILE for an independent test."
            if generated else ""
        ),
    }


def render(payload: dict, limit: int = 12) -> str:
    """Render a compact summary table for the terminal."""
    lines = [
        f"Documents: {payload['documents']} · naive baseline: {payload['baseline_tokens']:,} tokens per question",
        "",
        f"{'Question':<46} {'naive':>9} {'atlas':>7} {'saved':>7}",
        "-" * 72,
    ]
    for row in payload["rows"][:limit]:
        question = row["question"]
        if len(question) > 44:
            question = question[:41] + "..."
        if not row["found"]:
            lines.append(f"{question:<46} {'—':>9} {'miss':>7} {'':>7}")
            continue
        lines.append(
            f"{question:<46} {row['naive']:>9,} {row['atlas']:>7,} "
            f"{row['saved'] * 100:>6.0f}%"
        )
    if len(payload["rows"]) > limit:
        lines.append(f"{'… ' + str(len(payload['rows']) - limit) + ' more':<46}")

    lines += [
        "-" * 72,
        f"{'Total':<46} {payload['naive_total']:>9,} {payload['atlas_total']:>7,} "
        f"{payload['saved'] * 100:>6.0f}%",
        "",
        f"{payload['answered']}/{payload['questions']} answered"
        + (f", {payload['missed']} without a hit" if payload["missed"] else ""),
    ]
    if payload["caveat"]:
        lines += ["", payload["caveat"]]

    code_data = payload.get("code") or {}
    code_rows = code_data.get("rows") or []
    if code_rows:
        lines += [
            "",
            f"Code files: {code_data.get('files', 0)} (~{code_data.get('codebase_tokens', 0):,} tokens) "
            "· Symbol lookup (`atlas_code_find` vs. file read)",
            "",
            f"{'Symbol (path)':<46} {'file':>9} {'atlas':>7} {'saved':>7}",
            "-" * 72,
        ]
        for crow in code_rows[:limit]:
            label = f"{crow['symbol']} ({crow['path']})"
            if len(label) > 44:
                label = label[:41] + "..."
            lines.append(
                f"{label:<46} {crow['naive']:>9,} {crow['atlas']:>7,} "
                f"{crow['saved'] * 100:>6.0f}%"
            )
        lines += [
            "-" * 72,
            f"{'Code Total':<46} {code_data['naive_total']:>9,} {code_data['atlas_total']:>7,} "
            f"{code_data['saved'] * 100:>6.0f}%",
        ]
    return "\n".join(lines)
