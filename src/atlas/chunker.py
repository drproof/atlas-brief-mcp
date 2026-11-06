"""Markdown -> sections with stable anchors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .slug import slugify

MAX_CHARS = 1500

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
PARA_RE = re.compile(r"\n\s*\n")

# Google Docs exports punctuation escaped ("1\.", "\-", "\_"). That hurts
# search and snippets without adding content. Unescape only these characters.
ESCAPE_RE = re.compile(r"\\([.\-_*\[\]#!|>~])")


def clean_markdown(text: str) -> str:
    """Remove export escapes like '1\\.' -> '1.' without touching real backslashes."""
    return ESCAPE_RE.sub(r"\1", text)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class Section:
    anchor: str
    doc_id: str
    path: str
    heading: str
    level: int
    breadcrumb: str
    line_start: int
    line_end: int
    body: str
    part: int = 1
    parts: int = 1
    tags: str = ""


@dataclass
class Doc:
    doc_id: str
    path: str
    title: str
    meta: dict = field(default_factory=dict)
    sections: list[Section] = field(default_factory=list)


def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """Return (meta, body, dropped_lines). Fault-tolerant."""
    if not text.startswith("---"):
        return {}, text, 0
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() not in ("---",):
        return {}, text, 0
    end = None
    for i in range(1, min(len(lines), 300)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text, 0
    raw = "".join(lines[1:end])
    meta: dict = {}
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}
    return meta, "".join(lines[end + 1:]), end + 1


def scan_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return (line_index, level, title). Headings inside code fences are ignored."""
    out: list[tuple[int, int, str]] = []
    in_fence = False
    marker = ""
    for idx, line in enumerate(lines):
        fence = FENCE_RE.match(line)
        if fence:
            current = fence.group(1)
            if not in_fence:
                in_fence, marker = True, current
            elif current == marker:
                in_fence, marker = False, ""
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            title = match.group(2).strip()
            if title:
                out.append((idx, len(match.group(1)), title))
    return out


def _split_body(body: str, max_chars: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    parts: list[str] = []
    current = ""
    for para in PARA_RE.split(body):
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            parts.append(current)
        rest = para
        while len(rest) > max_chars:
            cut = rest.rfind(" ", 0, max_chars)
            if cut < max_chars * 0.5:
                cut = max_chars
            parts.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        current = rest
    if current:
        parts.append(current)
    # Merge tiny trailing fragments into the preceding part (e.g. 12-char leftovers
    # from a table split) — they are useless on their own.
    if len(parts) > 1 and len(parts[-1]) < 100:
        parts[-2] = f"{parts[-2]}\n\n{parts[-1]}".strip()
        parts.pop()
    return parts or [body]


def chunk(text: str, doc_id: str, path: str, max_chars: int = MAX_CHARS) -> Doc:
    """Split a Markdown document into sections."""
    meta, body, dropped = parse_frontmatter(text)
    body = clean_markdown(body)
    lines = body.splitlines()

    tags = meta.get("tags") or []
    if isinstance(tags, list):
        tags_str = " ".join(str(t) for t in tags)
    else:
        tags_str = str(tags)

    title = str(meta.get("title") or (meta.get("id") or "") or doc_id)

    headings = scan_headings(lines)
    blocks: list[tuple[int, int, str, int, int]] = []

    if not headings:
        blocks.append((0, int(meta.get("level", 1) or 1), title, 0, max(len(lines) - 1, 0)))
    else:
        if headings[0][0] > 0:
            preamble = "\n".join(lines[: headings[0][0]]).strip()
            if preamble:
                blocks.append((-1, 1, "(Preamble)", 0, headings[0][0] - 1))
        for i, (idx, level, heading) in enumerate(headings):
            end = headings[i + 1][0] - 1 if i + 1 < len(headings) else len(lines) - 1
            blocks.append((idx, level, heading, idx, end))

    used: set[str] = set()
    stack: list[tuple[int, str]] = []
    sections: list[Section] = []

    for idx, level, heading, start, end in blocks:
        if idx == -1:
            breadcrumb = ""
        else:
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            breadcrumb = " > ".join(t for _, t in stack[:-1])

        body_lines = lines[start + 1: end + 1] if idx != -1 else lines[start: end + 1]
        raw_body = "\n".join(body_lines).strip()

        base = f"{doc_id}#{slugify(heading)}"
        anchor = base
        counter = 2
        while anchor in used:
            anchor = f"{base}-{counter}"
            counter += 1
        used.add(anchor)

        pieces = _split_body(raw_body, max_chars)
        total = len(pieces)
        for number, piece in enumerate(pieces, start=1):
            part_anchor = anchor if number == 1 else f"{anchor}~{number}"
            sections.append(
                Section(
                    anchor=part_anchor,
                    doc_id=doc_id,
                    path=path,
                    heading=heading,
                    level=level,
                    breadcrumb=breadcrumb,
                    line_start=start + 1 + dropped,
                    line_end=end + 1 + dropped,
                    body=piece,
                    part=number,
                    parts=total,
                    tags=tags_str,
                )
            )

    return Doc(doc_id=doc_id, path=path, title=title, meta=meta, sections=sections)
