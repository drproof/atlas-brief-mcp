"""Search layer: BM25, heading boosts, and character-budget clamping."""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from typing import Any

from .normalize import STOPWORDS, build_match, fold, fold_fts, terms

MAX_HARD_CHARS = 8000


def _clamp(value: int, ceiling: int = MAX_HARD_CHARS) -> int:
    return max(120, min(int(value), ceiling))


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _trim(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text.rfind(" ", 0, limit)
    if cut < limit * 0.6:
        cut = limit
    return text[:cut].rstrip() + " …", True


_MD_NOISE = [
    (re.compile(r"«(\S{1,4})»"), r"\1"),      # »Raketen«-»Abwehr« -> »Raketen-Abwehr«
    (re.compile(r"\*\*|__"), ""),              # bold markers
    (re.compile(r"`+"), ""),                   # inline code
    (re.compile(r"\s*\|\s*"), " | "),          # table grid
    (re.compile(r"[ \t]+"), " "),
]


def clean_snippet(text: str) -> str:
    """Strip Markdown noise from FTS5 snippets (saves tokens)."""
    for pattern, replacement in _MD_NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()


_DIGRAPH_RE = re.compile(r"ae|oe|ue")
_DIGRAPH_MAP = {"ae": "a", "oe": "o", "ue": "u"}


def _de_digraph(token: str) -> str:
    return _DIGRAPH_RE.sub(lambda m: _DIGRAPH_MAP[m.group(0)], token)


def run_search(
    conn: sqlite3.Connection,
    query: str,
    k: int = 5,
    doc: str | None = None,
) -> list[dict[str, Any]]:
    match = build_match(query)
    if not match:
        return []

    clause = _compound_clause(conn, query)
    if clause:
        match = f"({match}) OR {clause}"

    rows = _query(conn, match)
    if not rows:
        return []

    # Work order logbooks repeat every system and class name and would otherwise
    # outrank the actual design sections on system queries. Downrank rather than
    # hide: for terms that appear only there ('Kriegshorn'), they remain useful.
    log_penalties = _log_penalty(conn, rows)

    wanted = terms(query)
    # Include hyphen-split parts, de-digraphed umlauts ('ruestungs' -> 'rustung'),
    # and Fugen-stripped stems for the heading boost.
    wanted_parts: set[str] = set()
    for term in wanted:
        wanted_parts.add(term)
        for part in term.split("-"):
            if len(part) >= 3:
                wanted_parts.add(part)
                dedig = _de_digraph(part)
                if len(dedig) >= 3:
                    wanted_parts.add(dedig)
                for fugen in ("s", "es", "en"):
                    if dedig.endswith(fugen) and len(dedig) - len(fugen) >= 4:
                        wanted_parts.add(dedig[: -len(fugen)])

    results: list[dict[str, Any]] = []
    for row in rows:
        if doc and row["doc_id"] != doc:
            continue
        score = float(row["score"])  # bm25 is negative: smaller = better
        heading_folded = fold(row["heading"]) + " " + fold_fts(row["heading"])
        if any(part in heading_folded for part in wanted_parts):
            score *= 1.5
        if row["level"] == 2:
            score *= 1.1
        # Table-of-contents lines ('Tab 1 … Tab 2 …') list every keyword in the
        # document and would otherwise crowd out the actual topic sections.
        if _looks_like_index(row["body"] or ""):
            score /= 2.5
        # Work order logbook: repeats all system names and crowds out topic
        # sections (see _log_penalty).
        score *= log_penalties.get((row["doc_id"], row["heading"]), 1.0)
        results.append({
            "anchor": row["anchor"],
            "doc_id": row["doc_id"],
            "path": row["path"],
            "line": row["line_start"],
            "heading": row["heading"],
            "breadcrumb": row["breadcrumb"] or "",
            "snippet": clean_snippet(row["snippet"] or ""),
            "score": round(score, 3),
        })

    results.sort(key=lambda item: item["score"])
    return results[: max(1, k)]


def _query(conn: sqlite3.Connection, match: str):
    return conn.execute(
        """
        SELECT s.anchor, s.doc_id, s.path, s.heading, s.breadcrumb,
               s.line_start, s.line_end, s.level, s.chars, s.body,
               snippet(sections_fts, 2, '»', '«', '…', 14) AS snippet,
               bm25(sections_fts, 8.0, 4.0, 1.0, 2.0) AS score
        FROM sections_fts
        JOIN sections s ON s.rowid = sections_fts.rowid
        WHERE sections_fts MATCH ?
        ORDER BY score
        LIMIT 60
        """,
        (match,),
    ).fetchall()


def _in_vocab(conn: sqlite3.Connection, token: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM fts_vocab WHERE term=? LIMIT 1", (token,)
    ).fetchone() is not None


def _looks_like_index(body: str) -> bool:
    """Detect table-of-contents / navigation lines.

    Such lines list every keyword in the document and therefore compete with
    every topic section.
    """
    if body.count("Tab ") >= 2:
        return True
    if body.count("`t.") >= 3:
        return True
    return False


WO_LINE_RE = re.compile(r"Work\s*Order\s*\d+\s*[—–:\-]", re.IGNORECASE)

LOG_MIN_LINES = 8
LOG_HARD_LINES = 16
LOG_HEADING_HINTS = (
    "plan", "meilenstein", "milestone", "roadmap", "übersicht",
    "overview", "fahrplan", "backlog", "statusübersicht",
)


def _log_penalty(conn: sqlite3.Connection, rows) -> dict[tuple[str, str], float]:
    """Compute a score penalty factor for sections that are work order logbooks."""
    verdict: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (row["doc_id"], row["heading"])
        if key in verdict:
            continue
        heading_low = (row["heading"] or "").casefold()
        if any(hint in heading_low for hint in LOG_HEADING_HINTS):
            verdict[key] = 1.0
            continue
        parts = conn.execute(
            "SELECT body FROM sections WHERE doc_id=? AND heading=?", key
        ).fetchall()
        total = sum(len(WO_LINE_RE.findall(part["body"] or "")) for part in parts)
        if total < LOG_MIN_LINES:
            verdict[key] = 1.0
        elif total < LOG_HARD_LINES:
            verdict[key] = 0.5
        else:
            verdict[key] = 0.25
    return {key: factor for key, factor in verdict.items() if factor < 1.0}


def _prefix_in_vocab(conn: sqlite3.Connection, prefix: str) -> bool:
    if not prefix:
        return False
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return conn.execute(
        "SELECT 1 FROM fts_vocab WHERE term >= ? AND term < ? LIMIT 1",
        (prefix, upper),
    ).fetchone() is not None


def _orthographic_variants(conn: sqlite3.Connection, term: str) -> list[str]:
    """Return attested FTS vocabulary / stem variants for ASCII umlauts (`ae`/`oe`/`ue`) and `ss` <-> `ß`."""
    candidates: list[str] = []
    dedig = _de_digraph(term)
    for base in dict.fromkeys((term, dedig)):
        if base != term:
            candidates.append(base)
        if "ss" in base:
            candidates.append(base.replace("ss", "ß"))
            # Handle mixed compounds like `stossschaden` (`stoß` + `schaden`)
            idx = 0
            while True:
                pos = base.find("ss", idx)
                if pos < 0:
                    break
                single_sub = base[:pos] + "ß" + base[pos + 2:]
                if single_sub not in candidates:
                    candidates.append(single_sub)
                idx = pos + 1

    attested: list[str] = []
    for cand in candidates:
        if len(cand) < 3 or cand == term:
            continue
        if _in_vocab(conn, cand) or (len(cand) >= 4 and _prefix_in_vocab(conn, cand)):
            attested.append(cand)
            continue
        for fugen in ("s", "es", "en", "n"):
            if cand.endswith(fugen) and len(cand) - len(fugen) >= 4:
                stem = cand[: -len(fugen)]
                if _in_vocab(conn, stem):
                    attested.append(stem)
                    break
    return list(dict.fromkeys(attested))


_FUGENLAUTE = ("", "s", "es", "n", "en", "er", "e")


def _split_compound_vocab(conn: sqlite3.Connection, word: str, max_parts: int = 3) -> list[str]:
    """Split a compound word into 2 or 3 attested FTS vocabulary stems (handling German Fugenlaute)."""
    n = len(word)
    if n < 6:
        return []
    if n >= 8:
        for cut in range(4, n - 3):
            head = word[:cut]
            if not _in_vocab(conn, head):
                continue
            for fugen in _FUGENLAUTE:
                rest_start = cut + len(fugen)
                if rest_start > n - 4 or not word.startswith(fugen, cut):
                    continue
                tail = word[rest_start:]
                if _in_vocab(conn, tail):
                    return [head, tail]
                if max_parts >= 3 and len(tail) >= 6:
                    sub = _split_compound_vocab(conn, tail, max_parts=max_parts - 1)
                    if sub:
                        return [head, *sub]

    # Fallback for 3-letter domain stems ('tie'+'breaker', 'eis'+'schaden', 'hex'+'feld')
    for cut in range(3, n - 2):
        head = word[:cut]
        if head in STOPWORDS or not _in_vocab(conn, head):
            continue
        for fugen in ("", "s"):
            rest_start = cut + len(fugen)
            if rest_start > n - 3 or not word.startswith(fugen, cut):
                continue
            tail = word[rest_start:]
            if tail in STOPWORDS:
                continue
            if _in_vocab(conn, tail):
                return [head, tail]
            if max_parts >= 3 and len(tail) >= 6:
                sub = _split_compound_vocab(conn, tail, max_parts=max_parts - 1)
                if sub:
                    return [head, *sub]
    return []


def _compound_clause(conn: sqlite3.Connection, query: str) -> str | None:
    """Build FTS clauses for orthographic variants, Fugen-stems, and 2/3-part compounds."""
    clauses: list[str] = []
    seen_clauses: set[str] = set()

    def _emit(expr: str) -> None:
        if expr and expr not in seen_clauses:
            seen_clauses.add(expr)
            clauses.append(expr)

    for term in terms(query):
        if "-" in term:
            continue

        # 1. Orthographic variants (`ae/oe/ue -> a/o/u`, `ss -> ß`)
        for variant in _orthographic_variants(conn, term):
            safe_v = variant.replace('"', "")
            _emit(f'"{safe_v}"*')

        # 2. Trailing Fugenlaut on hyphen-split or standalone modifier ('rustungs' -> 'rustung')
        if len(term) >= 5 and not _in_vocab(conn, term) and not _prefix_in_vocab(conn, term):
            base_forms = list(dict.fromkeys((term, _de_digraph(term))))
            for form in base_forms:
                for fugen in ("s", "es", "en", "n"):
                    if form.endswith(fugen) and len(form) - len(fugen) >= 4:
                        stem = form[: -len(fugen)]
                        if _in_vocab(conn, stem):
                            _emit(f'"{stem}"*')
                            break

        if len(term) < 6 or _in_vocab(conn, term):
            continue

        # 3. 2-part and 3-part compound decomposition (including de-digraphed & Eszett forms)
        probe_forms = list(dict.fromkeys((term, _de_digraph(term), term.replace("ss", "ß"))))
        matched_parts: list[str] = []
        longest = ""
        for form in probe_forms:
            parts = _split_compound_vocab(conn, form, max_parts=3)
            if len(parts) >= 2:
                matched_parts = parts
                break
            for cut in range(len(form) - 3, 3, -1):
                head = form[:cut]
                if len(head) > len(longest) and _in_vocab(conn, head):
                    longest = head
                    break

        if len(matched_parts) >= 2:
            _emit("(" + " AND ".join(f'"{piece}"*' for piece in matched_parts) + ")")
        elif len(longest) >= 6:
            _emit(f'"{longest}"*')

    return " OR ".join(clauses) or None


def where(conn: sqlite3.Connection, topic: str, max_chars: int = 400) -> str:
    budget = _clamp(max_chars)
    hits = run_search(conn, topic, k=3)
    if not hits:
        payload = {
            "found": False,
            "hint": "No match. Try shorter terms or use atlas_outline().",
        }
        return _dumps(payload)

    top = hits[0]
    base = top["anchor"].split("~")[0]
    alternatives = [
        {"anchor": h["anchor"].split("~")[0], "heading": h["heading"]}
        for h in hits[1:3]
    ]
    payload = {
        "found": True,
        "anchor": base,
        "path": top["path"],
        "line": top["line"],
        "heading": top["heading"],
        "breadcrumb": top["breadcrumb"],
        "alternatives": alternatives,
        "read_with": f'atlas_read("{base}")',
    }
    text = _dumps(payload)
    # The breadcrumb is the most valuable extra context ("which tab?"), so drop
    # it last — not at the same time as the alternatives.
    if len(text) > budget:
        payload.pop("alternatives", None)
        text = _dumps(payload)
    if len(text) > budget:
        payload.pop("read_with", None)
        text = _dumps(payload)
    if len(text) > budget:
        payload.pop("breadcrumb", None)
        text = _dumps(payload)
    return text


def _shrink(payload: dict[str, Any], budget: int) -> str:
    """Progressively shrink a hit list until it fits within the character budget.

    Guaranteed to terminate: falls back to 0 hits plus a hint.

    `truncated` is set **before** each budget check. Previously it was added
    afterward — pushing the response 19 characters over the exact limit it was
    supposed to respect.
    """
    text = _dumps(payload)
    if len(text) <= budget:
        return text

    payload["truncated"] = True

    for hit in payload["hits"]:
        hit["snippet"], _ = _trim(hit.get("snippet", ""), 120)
    text = _dumps(payload)
    if len(text) <= budget:
        return text

    payload["hits"] = payload["hits"][:2]
    payload["count"] = len(payload["hits"])
    for hit in payload["hits"]:
        hit["snippet"], _ = _trim(hit.get("snippet", ""), 60)
    if len(_dumps(payload)) <= budget:
        return _dumps(payload)

    payload["hits"] = payload["hits"][:1]
    payload["count"] = len(payload["hits"])
    if payload["hits"]:
        payload["hits"][0].pop("breadcrumb", None)
        payload["hits"][0].pop("doc_id", None)
        payload["hits"][0]["snippet"], _ = _trim(payload["hits"][0].get("snippet", ""), 40)
    if len(_dumps(payload)) <= budget:
        return _dumps(payload)

    payload["hits"] = []
    payload["count"] = 0
    payload["hint"] = "max_chars too small for hit details — increase it."
    return _dumps(payload)


def search(
    conn: sqlite3.Connection,
    query: str,
    k: int = 5,
    doc: str | None = None,
    max_chars: int = 1200,
) -> str:
    budget = _clamp(max_chars)
    hits = run_search(conn, query, k=k, doc=doc)
    payload: dict[str, Any] = {"query": query, "hits": hits, "count": len(hits)}
    if not hits:
        payload["hint"] = "No match. Try shorter terms or use atlas_outline()."
    return _shrink(payload, budget)


def _suggest_anchors(conn: sqlite3.Connection, anchor: str, limit: int = 5) -> list[str]:
    """Return closest indexed section anchors when a requested `anchor` does not exist."""
    needle = (anchor or "").strip().split("~")[0]
    if not needle:
        return []
    doc_prefix = needle.split("#", 1)[0].casefold() if "#" in needle else ""
    slug_part = needle.split("#", 1)[1].casefold() if "#" in needle else needle.casefold()
    slug_tokens = {tok for tok in re.split(r"[^a-z0-9]+", fold(slug_part)) if len(tok) >= 2}

    rows = conn.execute(
        "SELECT DISTINCT anchor, doc_id, heading FROM sections WHERE part=1"
    ).fetchall()
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for row in rows:
        cand = (row["anchor"] or "").split("~")[0]
        if not cand or cand in seen:
            continue
        seen.add(cand)
        cand_low = cand.casefold()
        cand_slug = cand_low.split("#", 1)[1] if "#" in cand_low else cand_low
        ratio = difflib.SequenceMatcher(None, needle.casefold(), cand_low).ratio()
        slug_ratio = difflib.SequenceMatcher(None, slug_part, cand_slug).ratio()
        score = max(ratio, slug_ratio * 0.95)
        if doc_prefix and (row["doc_id"] or "").casefold() == doc_prefix:
            score += 0.2
        if slug_part and (slug_part in cand_slug or cand_slug in slug_part):
            score += 0.25
        if slug_tokens:
            cand_tokens = {
                tok
                for tok in re.split(r"[^a-z0-9]+", fold(f"{cand_slug} {row['heading'] or ''}"))
                if len(tok) >= 2
            }
            overlap = len(slug_tokens & cand_tokens) / len(slug_tokens)
            score += overlap * 0.35
        if score >= 0.35:
            scored.append((score, cand))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [cand for _s, cand in scored[: max(1, limit)]]


def read(
    conn: sqlite3.Connection,
    anchor: str,
    offset: int = 0,
    max_chars: int = 3000,
) -> str:
    budget = _clamp(max_chars)
    base = anchor.split("~")[0]
    rows = conn.execute(
        "SELECT rowid, * FROM sections WHERE anchor = ? OR anchor LIKE ? ORDER BY part",
        (base, base + "~%"),
    ).fetchall()
    if not rows:
        suggestions = _suggest_anchors(conn, anchor)
        err_payload: dict[str, Any] = {
            "error": f"anchor '{anchor}' not found.",
            "suggestions": suggestions,
            "hint": "Use atlas_where() or atlas_outline().",
        }
        return _dumps(err_payload)

    first = rows[0]
    body = "\n\n".join(row["body"] for row in rows).strip()

    # Container headings like "## 4. Combat System" often have no body text of
    # their own — the content lives in the subsections. Without this fallback,
    # `atlas_read` returns an empty body and the agent falls back to reading the
    # entire file.
    children_included = 0
    if len(body) < 80:
        kids = conn.execute(
            "SELECT anchor, heading, level, body FROM sections "
            "WHERE doc_id=? AND rowid > ? ORDER BY rowid",
            (first["doc_id"], first["rowid"]),
        ).fetchall()
        parts: list[str] = []
        for kid in kids:
            if kid["level"] <= first["level"]:
                break
            if not kid["body"].strip():
                continue
            parts.append("#" * min(kid["level"], 6) + f" {kid['heading']}\n{kid['body']}")
            children_included += 1
        if parts:
            body = "\n\n".join(parts)

    total = len(body)
    start = max(0, int(offset))
    chunk = body[start: start + budget]
    payload = {
        "anchor": base,
        "path": first["path"],
        "heading": first["heading"],
        "breadcrumb": first["breadcrumb"] or "",
        "line": first["line_start"],
        "chars": len(chunk),
        "total_chars": total,
        "offset": start,
        "truncated": start + len(chunk) < total,
        "body": chunk,
    }
    if children_included:
        payload["children_included"] = children_included
    if payload["truncated"]:
        payload["next_offset"] = start + len(chunk)
    return _dumps(payload)


def outline(conn: sqlite3.Connection, doc: str | None = None, max_chars: int = 800) -> str:
    budget = _clamp(max_chars)
    sql = "SELECT anchor, doc_id, path, heading, level FROM sections WHERE part=1"
    params: list[Any] = []
    if doc:
        sql += " AND doc_id=?"
        params.append(doc)
    sql += " ORDER BY doc_id, rowid"
    rows = conn.execute(sql, params).fetchall()

    lines: list[str] = []
    current_doc = None
    for row in rows:
        if row["doc_id"] != current_doc:
            current_doc = row["doc_id"]
            if lines:
                lines.append("")
            lines.append(f"# {row['doc_id']}  ({row['path']})")
        indent = "  " * max(0, row["level"] - 1)
        lines.append(f"{indent}- {row['heading']}  [{row['anchor']}]")

    text = "\n".join(lines)
    if len(text) > budget:
        cut = text.rfind("\n", 0, budget)
        text = text[:cut if cut > 0 else budget]
        return _dumps({"outline": text, "truncated": True,
                       "hint": "Use atlas_outline(doc='…') for a single document."})
    return _dumps({"outline": text, "docs": len({r["doc_id"] for r in rows}), "truncated": False})


def tasks(
    conn: sqlite3.Connection,
    status: str = "open",
    owner: str | None = None,
    max_chars: int = 1000,
) -> str:
    budget = _clamp(max_chars)
    sql = "SELECT anchor, path, text, done, owner, tag, line FROM tasks"
    clauses: list[str] = []
    params: list[Any] = []
    if status == "open":
        clauses.append("done=0")
    elif status == "done":
        clauses.append("done=1")
    if owner and owner.strip().lstrip("@"):
        clean_owner = owner.strip().lstrip("@")
        clauses.append("LOWER(owner)=LOWER(?)")
        params.append(clean_owner)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY done, anchor LIMIT 60"
    rows = conn.execute(sql, params).fetchall()

    items = [
        {
            "anchor": row["anchor"],
            "path": row["path"] or "",
            "line": row["line"],
            "text": row["text"],
            "owner": row["owner"] or "",
            "tag": row["tag"] or "",
            "done": bool(row["done"]),
        }
        for row in rows
    ]
    payload: dict[str, Any] = {"count": len(items), "tasks": items}
    text = _dumps(payload)
    if len(text) > budget:
        payload["truncated"] = True
        for item in payload["tasks"]:
            item["text"], _ = _trim(item["text"], 90)
            text = _dumps(payload)
            if len(text) <= budget:
                return text
        while len(payload["tasks"]) > 1 and len(_dumps(payload)) > budget:
            payload["tasks"].pop()
    return _dumps(payload)


def links(conn: sqlite3.Connection, anchor: str, max_chars: int = 400) -> str:
    budget = _clamp(max_chars)
    base = anchor.split("~")[0]
    row = conn.execute(
        "SELECT doc_id, path, heading FROM sections WHERE anchor=? OR anchor LIKE ? LIMIT 1",
        (base, f"{base}~%"),
    ).fetchone()
    if row is None:
        suggestions = _suggest_anchors(conn, anchor)
        return _dumps({
            "error": f"anchor '{anchor}' not found.",
            "suggestions": suggestions,
            "hint": "Use atlas_where() or atlas_outline().",
        })

    doc = conn.execute(
        "SELECT implements, tags FROM docs WHERE id=?", (row["doc_id"],)
    ).fetchone()
    siblings = conn.execute(
        "SELECT anchor, heading FROM sections WHERE doc_id=? AND part=1 AND anchor != ? LIMIT 8",
        (row["doc_id"], base),
    ).fetchall()

    payload = {
        "anchor": base,
        "heading": row["heading"],
        "path": row["path"] or "",
        "implements": (doc["implements"] if doc else "") or "",
        "tags": (doc["tags"] if doc else "") or "",
        "siblings": [{"anchor": s["anchor"], "heading": s["heading"]} for s in siblings],
    }
    text = _dumps(payload)
    if len(text) > budget:
        payload["siblings"] = payload["siblings"][:3]
        text = _dumps(payload)
    return text
