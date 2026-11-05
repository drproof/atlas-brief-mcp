"""Suchschicht: BM25, Boni, Budget-Kappung."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .normalize import build_match, fold, terms

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
    (re.compile(r"\*\*|__"), ""),              # Fett-Marker
    (re.compile(r"`+"), ""),                   # Inline-Code
    (re.compile(r"\s*\|\s*"), " | "),          # Tabellen-Gitter
    (re.compile(r"[ \t]+"), " "),
]


def clean_snippet(text: str) -> str:
    """Entfernt Markdown-Rauschen aus FTS5-Snippets (spart Tokens)."""
    for pattern, replacement in _MD_NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()


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

    # Work-Order-Logbücher wiederholen alle System- und Klassennamen und gewinnen
    # dadurch bei Systemfragen gegen die Dokumentation. Abwerten, nicht ausblenden:
    # bei Begriffen, die nur dort stehen ('Kriegshorn'), bleiben sie nützlich.
    log_penalties = _log_penalty(conn, rows)

    wanted = terms(query)
    # Auch Teilwörter für den Heading-Boost: 'helden-klassen' soll 'Helden, Skills
    # & 3D-Camp' treffen, nicht am Bindestrich scheitern.
    wanted_parts: set[str] = set()
    for term in wanted:
        wanted_parts.add(term)
        wanted_parts.update(part for part in term.split("-") if len(part) > 3)

    results: list[dict[str, Any]] = []
    for row in rows:
        if doc and row["doc_id"] != doc:
            continue
        score = float(row["score"])  # bm25 ist negativ: kleiner = besser
        heading_folded = fold(row["heading"])
        if any(part in heading_folded for part in wanted_parts):
            score *= 1.5
        if row["level"] == 2:
            score *= 1.1
        # Inhaltsverzeichnis-Zeilen ('Tab 1 … Tab 2 …') listen alle Stichworte
        # des Dokuments auf und verdrängen sonst die echten Themenseiten.
        if _looks_like_index(row["body"] or ""):
            score /= 2.5
        # Work-Order-Logbuch: wiederholt alle Systemnamen und verdrängt damit die
        # Themenseiten (siehe _log_penalty).
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
    """Erkennt Inhaltsverzeichnis-/Navigationszeilen.

    Solche Zeilen listen alle Stichworte des Dokuments auf und konkurrieren
    dadurch mit jeder Themenseite.
    """
    if body.count("Tab ") >= 2:
        return True
    if body.count("`t.") >= 3:
        return True
    return False


WO_LINE_RE = re.compile(r"Work\s*Order\s*\d+\s*[—–:\-]", re.IGNORECASE)

# Ab dieser Zahl von Work-Order-Zeilen ist ein Abschnitt ein Logbuch.
# Vorher stand hier 3 — damit galt auch der Prototyp-Meilenstein-Plan mit fünf
# Einträgen als Logbuch und verlor gegen jeden Fließtext-Absatz.
LOG_MIN_LINES = 8
# Ab hier ist es ein echtes Logbuch: voller Malus.
LOG_HARD_LINES = 16
# Überschriften, die eine Übersicht ankündigen — nie ein Logbuch, egal wie
# viele Work Orders darunter stehen.
LOG_HEADING_HINTS = ("plan", "meilenstein", "roadmap", "übersicht", "fahrplan",
                     "backlog", "statusübersicht")


def _log_penalty(conn: sqlite3.Connection, rows) -> dict[tuple[str, str], float]:
    """Score-Malus für Abschnitte, die ein Work-Order-Logbuch sind.

    Der Chunker zerlegt einen langen Abschnitt in Teile von 1500 Zeichen. Ein
    einzelner Teil enthält dann nur 1–2 Work-Order-Zeilen und sieht wie ein
    normaler Abschnitt aus. Deshalb wird über **alle** Teile eines Headings
    gezählt, nicht pro Teil.

    Rückgabe: `{(doc_id, heading): faktor}`. Fehlt ein Heading, gilt 1.0.
    """
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
            verdict[key] = 0.5   # halber Malus: Übersicht mit vielen Einträgen
        else:
            verdict[key] = 0.25  # voller Malus: Logbuch
    return {key: factor for key, factor in verdict.items() if factor < 1.0}


def _compound_clause(conn: sqlite3.Connection, query: str) -> str | None:
    """Komposita-Klausel für Wörter, die es im Index nicht gibt.

    „Gewölbestürmer" steht im GDD als „Gewölbe-Stürmer" — als ein Token trifft das
    nie. Die Teile treffen gemeinsam: `("gewolbe"* AND "sturmer"*)`.

    Die Klausel wird **zusätzlich** zur normalen Suche verodert, nicht als Ersatz:
    sonst blockiert ein häufiges Wort wie „perk" den präzisen Komposita-Treffer.
    """
    clauses: list[str] = []
    for term in terms(query):
        if len(term) < 8 or _in_vocab(conn, term):
            continue

        pieces: list[str] = []
        longest = ""
        for cut in range(4, len(term) - 3):
            head, tail = term[:cut], term[cut:]
            if _in_vocab(conn, head) and _in_vocab(conn, tail):
                pieces.extend([head, tail])
            elif _in_vocab(conn, head) and len(head) > len(longest):
                longest = head

        unique = list(dict.fromkeys(pieces))
        if len(unique) >= 2:
            clauses.append("(" + " AND ".join(f'"{piece}"*' for piece in unique[:2]) + ")")
        elif len(longest) >= 6:
            # Nur ein Teil belegt ('Rüstungsregeln' -> 'rustungs'): als Präfix
            # nehmen. Erst ab 6 Zeichen, sonst wird daraus Rauschen.
            clauses.append(f'"{longest}"*')
    return " OR ".join(clauses) or None


def where(conn: sqlite3.Connection, topic: str, max_chars: int = 400) -> str:
    budget = _clamp(max_chars)
    hits = run_search(conn, topic, k=3)
    if not hits:
        payload = {
            "found": False,
            "hint": "Kein Treffer. Kürzere Begriffe versuchen oder atlas_outline() nutzen.",
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
    # Der breadcrumb ist die wichtigste Zusatzinfo ("in welchem Tab?"), also
    # wird er als letztes geopfert — nicht gleichzeitig mit den Alternativen.
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
    """Reduziert eine Trefferliste stufenweise, bis sie ins Budget passt.

    Terminiert garantiert: Am Ende bleiben 0 Treffer plus Hinweis.

    `truncated` wird **vor** jedem Budget-Vergleich gesetzt. Früher geschah das
    danach — die Antwort war dann um die 19 Zeichen des Feldes zu lang, also
    genau an der Stelle über dem Limit, die sie einhalten sollte.
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
    payload["hint"] = "max_chars zu klein für Trefferdetails — erhöhen."
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
        payload["hint"] = "Kein Treffer. Kürzere Begriffe versuchen oder atlas_outline() nutzen."
    return _shrink(payload, budget)


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
        return _dumps({
            "error": f"Anker '{anchor}' nicht gefunden.",
            "hint": "atlas_where() oder atlas_outline() nutzen.",
        })

    first = rows[0]
    body = "\n\n".join(row["body"] for row in rows).strip()

    # Gliederungsköpfe wie "## 4. Kampfsystem" haben oft keinen eigenen Text —
    # der Inhalt steht in den Unterabschnitten. Ohne diesen Fallback liefert
    # atlas_read eine leere Antwort und der Agent liest doch die ganze Datei.
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
                       "hint": "atlas_outline(doc='…') für ein einzelnes Dokument."})
    return _dumps({"outline": text, "docs": len({r["doc_id"] for r in rows}), "truncated": False})


def tasks(
    conn: sqlite3.Connection,
    status: str = "open",
    owner: str | None = None,
    max_chars: int = 1000,
) -> str:
    budget = _clamp(max_chars)
    sql = "SELECT anchor, text, done, owner, tag, line FROM tasks"
    clauses: list[str] = []
    params: list[Any] = []
    if status == "open":
        clauses.append("done=0")
    elif status == "done":
        clauses.append("done=1")
    if owner:
        clauses.append("owner=?")
        params.append(owner)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY done, anchor LIMIT 60"
    rows = conn.execute(sql, params).fetchall()

    items = [
        {
            "anchor": row["anchor"],
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
        "SELECT doc_id, heading FROM sections WHERE anchor=? OR anchor LIKE ? LIMIT 1",
        (base, f"{base}~%"),
    ).fetchone()
    if row is None:
        return _dumps({"error": f"Anker '{anchor}' nicht gefunden."})

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
        "implements": (doc["implements"] if doc else "") or "",
        "tags": (doc["tags"] if doc else "") or "",
        "siblings": [{"anchor": s["anchor"], "heading": s["heading"]} for s in siblings],
    }
    text = _dumps(payload)
    if len(text) > budget:
        payload["siblings"] = payload["siblings"][:3]
        text = _dumps(payload)
    return text
