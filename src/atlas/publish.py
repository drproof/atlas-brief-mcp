"""`atlas publish`: write work order status lines to the master document.

Four safeguards because writing to Google Docs is more fragile than reading:
1. Append-only — new status lines are only appended.
2. Revision lock (`requiredRevisionId`) — if a concurrent edit occurred, the
   write fails cleanly rather than landing at the wrong offset.
3. Self-verification — after writing, the line is checked directly via the Docs
   API (`documents.get`). The Drive export that feeds the index lags 0.5–2 s
   behind a write and is not reliable for immediate verification.
4. Wiring gate — a work order marked `done` is only written when its core
   symbols are both defined **and** used. Otherwise: `written: false`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import audit, gdocs, verify

DEFAULT_SECTION = "Work Orders"
FALLBACK_SECTIONS = (
    "Work Orders",
    "Prototyp-Meilenstein-Plan",
    "Prototype Milestone Plan",
    "Meilenstein-Plan",
    "Milestone Plan",
    "Meilensteine",
    "Milestones",
)


def should_sync_gdocs(sync_gdocs: bool = False) -> bool:
    """Return True only when Google Docs write-mirroring is explicitly enabled.

    By default (`False`), Google Docs is treated as a read-only design source (GDD)
    while local Git Markdown (`docs/ARCHITECTURE.md`) is the authoritative single
    source of truth for Work Orders and `- [x]` steps. Opt in per call via
    `sync_gdocs=True` (`--gdocs`) or globally via `ATLAS_SYNC_GDOCS=1`.
    """
    if sync_gdocs:
        return True
    return os.environ.get("ATLAS_SYNC_GDOCS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _promote_wo_to_local(
    root: Path,
    conn,
    item: dict,
    status_override: str | None = None,
    mark_all_steps_done: bool = False,
    dry_run: bool = False,
) -> dict:
    """Materialize an indexed GDD-only Work Order into local `docs/ARCHITECTURE.md`."""
    from . import localwrite

    wo_num = int(item["wo"])
    st = _norm_status(status_override or item.get("status") or "open")
    title = item.get("title") or f"Work Order {wo_num}"
    syms = item.get("symbols") or []
    desc_parts: list[str] = []
    if item.get("spec_anchor"):
        desc_parts.append(f"Spec `{item['spec_anchor']}`")
    if syms:
        desc_parts.append("Symbols: " + ", ".join(f"`{s}`" for s in syms))
    header = format_line(
        wo_num,
        st,
        title,
        " · ".join(desc_parts) if desc_parts else "Promoted from synced GDD.",
    )
    steps_list = item.get("steps") or []
    step_lines = [
        f"- [{'x' if (mark_all_steps_done or s.get('done')) else ' '}] {s['text']}"
        for s in steps_list
    ]
    block = "\n".join([header, *step_lines])
    return localwrite.publish(root, conn, wo_num, block, DEFAULT_SECTION, dry_run=dry_run)


def _norm_status(status: str) -> str:
    """Normalize English and German status strings to `'done'` or `'open'`."""
    lowered = (status or "").strip().lower()
    if lowered in {"done", "erledigt", "abgeschlossen"}:
        return "done"
    if lowered in {"open", "offen", "ausstehend", "todo", "in_progress", "in progress"}:
        return "open"
    raise ValueError(f"Unknown status '{status}' (allowed: done, open)")


def format_line(wo: int, status: str, title: str, description: str = "") -> str:
    """'Work Order 7 — ✅ DONE (Title): Description'"""
    norm = _norm_status(status)
    mark = gdocs.STATUS_MARK[norm]
    line = f"Work Order {int(wo)} — {mark}"
    if title.strip():
        line += f" ({title.strip()})"
    line += ":"
    if description.strip():
        line += f" {description.strip()}"
    return line


def _paragraph_style(element: dict) -> str:
    paragraph = element.get("paragraph") or {}
    style = paragraph.get("paragraphStyle") or {}
    return style.get("namedStyleType", "") or ""


def _paragraph_text(element: dict) -> str:
    paragraph = element.get("paragraph") or {}
    chunks: list[str] = []
    for item in paragraph.get("elements") or []:
        run = item.get("textRun")
        if run:
            chunks.append(run.get("content", ""))
    return "".join(chunks)


def _level(style: str) -> int:
    try:
        return int(style.split("_")[1])
    except (IndexError, ValueError):
        return 99


def iter_tab_bodies(document: dict):
    """Yield (tab_id, tab_title, body) for all tabs — including nested child tabs.

    Google Docs tabs are only returned by the API when `includeTabsContent=True`
    is set. When tabs are absent, falls back to `document["body"]`.
    """
    def walk(tabs):
        for tab in tabs or []:
            props = tab.get("tabProperties") or {}
            body = (tab.get("documentTab") or {}).get("body")
            if body:
                yield props.get("tabId", ""), props.get("title", ""), body
            yield from walk(tab.get("childTabs"))

    tabs = document.get("tabs") or []
    if tabs:
        yield from walk(tabs)
    elif document.get("body"):
        yield "", "", document["body"]


def _iter_body_paragraphs(body: dict | None):
    """Yield every structural element containing a `paragraph` in `body`, including tables and TOCs."""
    def walk_content(elements):
        for element in elements or []:
            if element.get("paragraph"):
                yield element
            table = element.get("table")
            if table:
                for row in table.get("tableRows") or []:
                    for cell in row.get("tableCells") or []:
                        yield from walk_content(cell.get("content"))
            toc = element.get("tableOfContents")
            if toc:
                yield from walk_content(toc.get("content"))

    yield from walk_content((body or {}).get("content"))


def tab_text(body: dict) -> str:
    """Concatenate all text runs of a tab body (including table cells) into a single string."""
    chunks: list[str] = []
    for element in _iter_body_paragraphs(body):
        paragraph = element.get("paragraph") or {}
        for item in paragraph.get("elements") or []:
            run = item.get("textRun")
            if run:
                chunks.append(run.get("content", ""))
    return "".join(chunks)


def _get_gdoc(docs, document_id: str) -> dict:
    """Fetch a Google Doc with `includeTabsContent=True`, falling back cleanly on legacy clients."""
    try:
        return docs.documents().get(
            documentId=document_id, includeTabsContent=True
        ).execute()
    except TypeError:
        return docs.documents().get(documentId=document_id).execute()


def _is_revision_conflict(error: Exception) -> bool:
    """Return True when `error` indicates a stale Google Docs `requiredRevisionId` write lock."""
    cause = getattr(error, "__cause__", None)
    cause_text = f" {cause} {gdocs._explain(cause)}" if cause is not None else ""
    combined = f"{error} {gdocs._explain(error)}{cause_text}".lower()
    return any(
        token in combined
        for token in (
            "requiredrevisionid",
            "writecontrol",
            "modified since the revision",
            "revision specified",
            "stale revision",
        )
    )


def find_tab_body_with_text(document: dict, needle: str) -> tuple[str, str, dict | None]:
    """Return `(tab_id, tab_title, body)` of the tab whose text contains `needle`."""
    for tab_id, title, body in iter_tab_bodies(document):
        if needle in tab_text(body):
            return tab_id, title, body
    return "", "", None


def find_tab_with_text(document: dict, needle: str) -> tuple[str, str]:
    """Return (tab_id, tab_title) of the tab whose text contains `needle`."""
    tab_id, title, _body = find_tab_body_with_text(document, needle)
    return tab_id, title


_WO_HEADER_NUM_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|\*{1,2})?Work\s*Order\s*(\d+)\s*(?:—|–|-|:)",
    re.IGNORECASE,
)


def _utf16_len(text: str) -> int:
    """Return Google Docs UTF-16 code unit count for `text`."""
    return len(text.encode("utf-16-le")) // 2


def _body_has_indices(body: dict | None) -> bool:
    """Return True when `body` carries Google Docs `startIndex` character offsets."""
    for element in _iter_body_paragraphs(body):
        if element.get("startIndex") is not None:
            return True
        paragraph = element.get("paragraph") or {}
        for item in paragraph.get("elements") or []:
            if item.get("startIndex") is not None:
                return True
    return False


def _replace_in_body_ranges(
    docs,
    document_id: str,
    revision: str,
    body: dict | None,
    old_text: str,
    new_text: str,
    tab_id: str = "",
    wo: int | None = None,
    max_replacements: int = 0,
) -> int:
    """Replace `old_text` using exact UTF-16 character ranges across fragmented `textRun`s.

    Google Docs frequently splits a status or checklist line across multiple
    `textRun` elements (bold `Work Order 9`, normal ` — `, colored `⬜ OPEN`),
    causing `replaceAllText` to return 0 occurrences. Range replacement also
    allows scoping duplicate step text (`- [ ] Add unit tests`) to a single
    Work Order `wo` without mutating other Work Orders in the same tab.
    """
    if not old_text or not body or not _body_has_indices(body):
        return 0

    matches: list[tuple[int, int]] = []
    current_wo: int | None = None

    for element in _iter_body_paragraphs(body):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        p_chars: list[str] = []
        u16_offsets: list[int] = []
        cursor: int | None = element.get("startIndex")
        has_offsets = True

        for item in paragraph.get("elements") or []:
            item_start = item.get("startIndex")
            if item_start is not None:
                cursor = int(item_start)
            run = item.get("textRun")
            if not run:
                if cursor is not None and item.get("endIndex") is not None:
                    cursor = int(item["endIndex"])
                continue
            content_str = run.get("content", "")
            if not content_str:
                continue
            if cursor is None:
                has_offsets = False
            for ch in content_str:
                p_chars.append(ch)
                if cursor is not None:
                    u16_offsets.append(cursor)
                    cursor += _utf16_len(ch)
        if cursor is not None:
            u16_offsets.append(cursor)

        p_text = "".join(p_chars)
        if not p_text:
            continue

        is_heading = _paragraph_style(element).startswith("HEADING")
        offset = 0
        for line in p_text.splitlines(keepends=True):
            line_start = offset
            line_end = offset + len(line)
            offset = line_end
            wo_match = _WO_HEADER_NUM_RE.match(line)
            if wo_match:
                current_wo = int(wo_match.group(1))
            elif is_heading or line.lstrip().startswith("#"):
                current_wo = None
            if wo is not None and current_wo != int(wo):
                continue
            if not has_offsets or len(u16_offsets) < len(p_text) + 1:
                continue
            search_from = line_start
            while True:
                idx = p_text.find(old_text, search_from, line_end)
                if idx < 0:
                    break
                end_idx = idx + len(old_text)
                matches.append((u16_offsets[idx], u16_offsets[end_idx]))
                if max_replacements > 0 and len(matches) >= max_replacements:
                    break
                search_from = end_idx
            if max_replacements > 0 and len(matches) >= max_replacements:
                break
        if max_replacements > 0 and len(matches) >= max_replacements:
            break

    if not matches:
        return 0

    requests: list[dict] = []
    for start_u16, end_u16 in reversed(matches):
        del_range: dict = {"startIndex": start_u16, "endIndex": end_u16}
        if tab_id:
            del_range["tabId"] = tab_id
        requests.append({"deleteContentRange": {"range": del_range}})
        if new_text:
            loc: dict = {"index": start_u16}
            if tab_id:
                loc["tabId"] = tab_id
            requests.append({"insertText": {"location": loc, "text": new_text}})

    req_body: dict = {"requests": requests}
    if revision:
        req_body["writeControl"] = {"requiredRevisionId": revision}
    try:
        docs.documents().batchUpdate(documentId=document_id, body=req_body).execute()
    except Exception as error:
        raise gdocs.SyncError(gdocs._explain(error)) from error
    return len(matches)


def _step_safe_for_unindexed_replace_all(tab_body: dict, old_text: str, wo: int) -> bool:
    """Return True when `old_text` occurs under `wo` and nowhere else in an unindexed body."""
    from . import localwrite

    text = tab_text(tab_body)
    wo_ctx = localwrite._wo_context_map(text)
    in_target = False
    in_other = False
    offset = 0
    for line in text.splitlines(keepends=True):
        if old_text in line:
            enc_wo, _enc_open = localwrite._enclosing_wo_for_offset(wo_ctx, offset)
            if enc_wo == int(wo):
                in_target = True
            else:
                in_other = True
        offset += len(line)
    return in_target and not in_other


def _replace_all(
    docs,
    document_id: str,
    revision: str,
    old_text: str,
    new_text: str,
    tab_id: str = "",
    tab_body: dict | None = None,
    wo: int | None = None,
) -> tuple[int, bool]:
    """Run `replaceAllText` scoped to a single tab, falling back to UTF-16 range replacement.

    Never strips `tabsCriteria` when `tab_id` is non-empty: falling back to an
    unscoped `replaceAllText` would modify identical lines in archive tabs.
    When `wo` is provided for a non-header line (such as a checklist step), never
    runs an unscoped `replaceAllText` that could toggle another Work Order's step.
    """
    def send(criteria: str) -> int:
        request: dict = {
            "replaceAllText": {
                "containsText": {"text": old_text, "matchCase": True},
                "replaceText": new_text,
            }
        }
        if criteria:
            request["replaceAllText"]["tabsCriteria"] = {"tabIds": [criteria]}
        body: dict = {"requests": [request]}
        if revision:
            body["writeControl"] = {"requiredRevisionId": revision}
        try:
            response = docs.documents().batchUpdate(
                documentId=document_id, body=body
            ).execute()
        except Exception as error:
            raise gdocs.SyncError(gdocs._explain(error)) from error
        replies = response.get("replies") or [{}]
        return (replies[0].get("replaceAllText") or {}).get("occurrencesChanged", 0)

    is_wo_scoped_step = wo is not None and not _WO_HEADER_NUM_RE.search(old_text)
    if is_wo_scoped_step and tab_body is not None:
        if _body_has_indices(tab_body):
            changed = _replace_in_body_ranges(
                docs, document_id, revision, tab_body, old_text, new_text,
                tab_id=tab_id, wo=wo, max_replacements=1,
            )
            return changed, bool(tab_id and changed)
        if not _step_safe_for_unindexed_replace_all(tab_body, old_text, int(wo)):
            return 0, False

    if tab_id:
        changed = send(tab_id)
        if changed:
            return changed, True
        if tab_body is not None:
            changed = _replace_in_body_ranges(
                docs, document_id, revision, tab_body, old_text, new_text,
                tab_id=tab_id, wo=wo,
            )
            if changed:
                return changed, True
        return 0, False

    changed = send("")
    if changed:
        return changed, False
    if tab_body is not None:
        changed = _replace_in_body_ranges(
            docs, document_id, revision, tab_body, old_text, new_text,
            tab_id="", wo=wo,
        )
        return changed, False
    return 0, False


def _is_section_title(text: str, needle: str) -> bool:
    """Return True if the paragraph starts with the target title rather than merely mentioning it.

    "3. Prototype Milestone Plan (5 Work Orders)" is a section title.
    "Tab-ID: … | Focus: … Prototype Milestone Plan (5 Work Orders)." is a
    navigation line and must not be used as an insertion anchor.
    """
    stripped = (text or "").strip().lower()
    target = needle.lower()
    if stripped.startswith(target):
        return True
    match = re.match(r"^\d+[.)]\s*", stripped)
    if match and stripped[match.end():].startswith(target):
        return True
    return False


def find_insert_index(body: dict, needle: str):
    """Locate the target section in a document body and return (insert_index, heading).

    Inserts after the last paragraph of the section — i.e., right before the
    next heading of the same or higher level.

    Two-pass search: first real `HEADING` paragraphs, then manually bolded
    `NORMAL_TEXT` headings. In both cases the term must *lead* the paragraph —
    mentions inside body prose do not count.
    """
    content = (body or {}).get("content") or []
    if not content:
        return None, None

    start = None
    ref_level = None

    # Pass 1: real headings; Pass 2: manually formatted "headings".
    for headings_only in (True, False):
        for index, element in enumerate(content):
            style = _paragraph_style(element)
            is_heading = style.startswith("HEADING")
            if headings_only and not is_heading:
                continue
            if not headings_only and is_heading:
                continue  # already checked in Pass 1
            if _is_section_title(_paragraph_text(element), needle):
                start = index
                ref_level = _level(style) if is_heading else 99
                break
        if start is not None:
            break

    if start is None:
        return None, None

    for index in range(start + 1, len(content)):
        style = _paragraph_style(content[index])
        if style.startswith("HEADING") and _level(style) <= ref_level:
            previous = content[index - 1] if index > 0 else None
            end = (previous or {}).get("endIndex")
            if end is None:
                end = content[index].get("startIndex", 1)
            # `endIndex` is AFTER the trailing newline. Insert before it so the
            # new paragraph inherits the style of the preceding paragraph rather
            # than the following heading (which would turn the work order into a
            # HEADING_2).
            return max(1, end - 1), _paragraph_text(content[start]).strip()

    ends = [el.get("endIndex") for el in content[start:] if el.get("endIndex")]
    if not ends:
        return None, None
    # Position of the final paragraph's trailing `\n`: insert before it with a
    # leading newline in the inserted text. The earlier `line + "\n"` inserted
    # BEFORE the newline and glued the text onto the last word
    # ("…TextWork Order 7 — ✅").
    return max(1, max(ends) - 1), _paragraph_text(content[start]).strip()


_ANY_WO_STATUS_RE = re.compile(
    r"Work\s*Order\s*\d+\s*(?:—|–|-|:)\s*(?:✅|⬜|☑|☐|🚧)",
    re.IGNORECASE,
)


def _find_wo_section_in_body(body: dict):
    """Locate the section that already contains Work Order status lines when the heading uses a custom title."""
    content = (body or {}).get("content") or []
    if not content:
        return None, None

    last_heading_idx = None
    last_heading_level = 99
    target_heading_idx = None
    target_ref_level = 99

    for index, element in enumerate(content):
        style = _paragraph_style(element)
        text = _paragraph_text(element)
        if style.startswith("HEADING"):
            last_heading_idx = index
            last_heading_level = _level(style)
        if _ANY_WO_STATUS_RE.search(text):
            target_heading_idx = last_heading_idx
            target_ref_level = last_heading_level
            break

    if target_heading_idx is None:
        return None, None

    heading_text = _paragraph_text(content[target_heading_idx]).strip()
    for index in range(target_heading_idx + 1, len(content)):
        style = _paragraph_style(content[index])
        if style.startswith("HEADING") and _level(style) <= target_ref_level:
            previous = content[index - 1] if index > 0 else None
            end = (previous or {}).get("endIndex")
            if end is None:
                end = content[index].get("startIndex", 1)
            return max(1, end - 1), heading_text

    ends = [el.get("endIndex") for el in content[target_heading_idx:] if el.get("endIndex")]
    if not ends:
        return None, None
    return max(1, max(ends) - 1), heading_text



def _tab_text_of(docs, document_id: str, tab_id: str = "") -> str:
    """Read the text of the target tab directly from the Docs API, not the export."""
    try:
        document = docs.documents().get(
            documentId=document_id, includeTabsContent=True
        ).execute()
    except TypeError:  # older client version does not support `includeTabsContent`
        document = docs.documents().get(documentId=document_id).execute()

    texts = {tid: tab_text(body) for tid, _title, body in iter_tab_bodies(document)}
    if tab_id and tab_id in texts:
        return texts[tab_id]
    return "\n".join(texts.values())


def _attachment_gate(root: Path, wo: int | None, status: str,
                     verify_symbols: list[str] | None,
                     dry_run: bool) -> tuple[dict, str]:
    """Check symbol wiring before writing. Returns (payload, refusal).

    Symbols come from two sources: explicitly passed arguments and — more
    importantly — `.atlas/wo-symbols.yml`. Recorded symbols are **always**
    checked even when the caller omits them: an agent that forgets the caller
    also forgets to pass the verification list.
    """
    if _norm_status(status) != "done":
        return {}, ""

    recorded = verify.load_wo_symbols(root).get(int(wo), []) if wo is not None else []
    symbols = list(dict.fromkeys(
        [str(s) for s in (verify_symbols or []) if str(s).strip()]
        + recorded
    ))
    if not symbols:
        return {
            "verify": {
                "checked": 0,
                "ok": True,
                "warning": (
                    "Acceptance without wiring check: no symbols verified. "
                    "Add symbols for completed work orders to .atlas/wo-symbols.yml "
                    "so later regressions can be detected."
                ),
            }
        }, ""

    result = verify.check(root, symbols)
    if not result["ok"] and not dry_run:
        return {"verify": result}, verify.refusal(result)
    return {"verify": result}, ""


def _insert_request(insert_index: int, line: str, tab_id: str = "") -> dict:
    """Build the insert request: on its own line, not glued to the preceding paragraph.

    The leading newline is essential. Without it, the text lands BEFORE the
    section's trailing newline and sticks to the last word
    ("…sectionWork Order 7 — ✅"). With it, a separate paragraph is created that
    inherits the preceding paragraph's style rather than the next heading's.
    """
    location = {"index": insert_index}
    if tab_id:
        location["tabId"] = tab_id
    return {
        "requests": [{
            "insertText": {"location": location, "text": "\n" + line}
        }],
    }


def publish(
    root: Path,
    conn,
    wo: int,
    status: str = "done",
    title: str = "",
    description: str = "",
    section: str = DEFAULT_SECTION,
    dry_run: bool = False,
    client_secret: str | None = None,
    verify_symbols: list[str] | None = None,
    sync_gdocs: bool = False,
) -> dict:
    norm_status = _norm_status(status)
    line = format_line(wo, norm_status, title, description)

    # Wiring gate: runs locally before any credential or network access.
    # An acceptance without a caller is never written — it would record an
    # intention rather than a delivered result (WO 8).
    gate, refusal = _attachment_gate(root, wo, norm_status, verify_symbols, dry_run)
    if refusal:
        audit.log(root, "publish_refused", wo=wo, status=norm_status, title=title,
                  symbols=[e["symbol"] for e in gate["verify"].get("failed", [])],
                  refused=True, written=False, verified=False)
        return {
            "written": False,
            "refused": True,
            "refusal": refusal,
            "wo": int(wo),
            "status": norm_status,
            **gate,
        }

    # Idempotency: a work order exists at most once in the document.
    # Without this guard, a second `publish` appends a duplicate line — and
    # `status` picks the *first* entry, so the stale one wins. That happened on
    # Nov 5: four WOs appeared twice (⬜ OPEN + ✅ DONE), and `atlas status`
    # reported "4 open" even though all four were finished.
    from . import localwrite, status as status_mod

    existing = next(
        (item for item in status_mod.extract_work_orders(conn) if item["wo"] == int(wo)),
        None,
    )
    if existing is not None:
        if existing["status"] == norm_status:
            return {
                "written": False,
                "unchanged": True,
                "wo": int(wo),
                "status": norm_status,
                "message": (
                    f"Work Order {wo} is already recorded as '{norm_status}' in the document — "
                    "nothing appended. Work orders are not duplicated."
                ),
                **gate,
            }
        # Different status: update in place instead of appending a second line.
        result = set_status(root, conn, wo, status=norm_status, dry_run=dry_run,
                            client_secret=client_secret, verify_symbols=verify_symbols,
                            sync_gdocs=sync_gdocs)
        result["reused_set_status"] = True
        result["message"] = (
            f"Work Order {wo} already existed as '{existing['status']}' — "
            "switched status instead of appending a second line."
        )
        return result

    # Local-first Single Source of Truth: whenever local Markdown docs exist
    # (e.g. `docs/ARCHITECTURE.md`) and `sync_gdocs` was not explicitly requested,
    # write directly to local Git Markdown without touching read-only Google Docs.
    local_targets = localwrite.targets(root)
    if not gdocs.known_sources(conn) or (local_targets and not should_sync_gdocs(sync_gdocs)):
        result = localwrite.publish(
            root, conn, int(wo), format_line(wo, norm_status, title, description),
            section, dry_run=dry_run,
        )
        if not dry_run and result.get("verified") and norm_status == "done":
            verify.record_wo_symbols(root, int(wo), verify_symbols)
        return {**result, **gate}

    creds = gdocs.load_credentials(interactive=False, client_secret=client_secret,
                                   scopes=gdocs.WRITE_SCOPES)
    drive, _sheets, docs = gdocs.build_services(creds)

    sources = [s for s in gdocs.known_sources(conn) if s["mimeType"] == gdocs.DOC_MIME]
    if not sources:
        raise gdocs.SyncError(
            "No Google Doc source known. Run `atlas sync --query <Name>` first."
        )

    target = None
    insert_index = None
    heading = None
    tab_id = ""
    tab_title = ""
    for source in sources:
        try:
            document = docs.documents().get(
                documentId=source["id"], includeTabsContent=True
            ).execute()
        except TypeError:
            # Older client version does not support `includeTabsContent`.
            try:
                document = docs.documents().get(documentId=source["id"]).execute()
            except Exception as error:
                raise gdocs.SyncError(gdocs._explain(error)) from error
        except Exception as error:
            raise gdocs.SyncError(gdocs._explain(error)) from error

        candidate_sections = (
            list(dict.fromkeys([section, *FALLBACK_SECTIONS]))
            if section == DEFAULT_SECTION
            else [section]
        )
        for current_tab_id, current_title, tab_body in iter_tab_bodies(document):
            for sec_name in candidate_sections:
                index, found_heading = find_insert_index(tab_body, sec_name)
                if index is not None:
                    target = {"source": source, "document": document}
                    insert_index = index
                    heading = found_heading
                    tab_id = current_tab_id
                    tab_title = current_title
                    break
            if target is not None:
                break
        if target is None and section == DEFAULT_SECTION:
            for current_tab_id, current_title, tab_body in iter_tab_bodies(document):
                index, found_heading = _find_wo_section_in_body(tab_body)
                if index is not None:
                    target = {"source": source, "document": document}
                    insert_index = index
                    heading = found_heading
                    tab_id = current_tab_id
                    tab_title = current_title
                    break
        if target is not None:
            break

    if target is None:
        raise gdocs.SyncError(
            f"Section '{section}' not found in any source document. "
            "Check the headings with `atlas outline`."
        )

    document_id = target["source"]["id"]
    gdocs.assert_writable(conn, document_id)

    # Second guard — this time against the LIVE document, not the index.
    # The index can lag behind (Drive export, see `index_lag`), whereas the
    # live document does not. Without this check, a second `publish` during the
    # lag window would append a duplicate line for the same work order.
    live_text = "\n".join(
        tab_text(body) for _tid, _title, body in iter_tab_bodies(target["document"])
    )
    if _status_line_re(int(wo)).search(live_text):
        result = set_status(root, conn, wo, status=status, dry_run=dry_run,
                            client_secret=client_secret, verify_symbols=verify_symbols,
                            sync_gdocs=sync_gdocs)
        result["reused_set_status"] = True
        return result

    revision = target["document"].get("revisionId", "")
    payload = {
        "document_id": document_id,
        "document_name": target["source"]["name"],
        "tab": tab_title or "(flat)",
        "tab_id": tab_id,
        "section": heading,
        "index": insert_index,
        "line": line,
        "dry_run": dry_run,
    }
    if dry_run:
        return payload

    body = _insert_request(insert_index, line, tab_id)
    if revision:
        body["writeControl"] = {"requiredRevisionId": revision}

    try:
        docs.documents().batchUpdate(documentId=document_id, body=body).execute()
    except Exception as error:
        if not _is_revision_conflict(error):
            raise gdocs.SyncError(gdocs._explain(error)) from error
        try:
            fresh_doc = _get_gdoc(docs, document_id)
        except Exception as refetch_err:
            raise gdocs.SyncError(gdocs._explain(refetch_err)) from refetch_err
        fresh_live = "\n".join(
            tab_text(b) for _tid, _title, b in iter_tab_bodies(fresh_doc)
        )
        if _status_line_re(int(wo)).search(fresh_live):
            result = set_status(
                root, conn, wo, status=status, dry_run=dry_run,
                client_secret=client_secret, verify_symbols=verify_symbols,
                sync_gdocs=sync_gdocs,
            )
            result["reused_set_status"] = True
            return result
        candidate_sections = (
            list(dict.fromkeys([section, *FALLBACK_SECTIONS]))
            if section == DEFAULT_SECTION
            else [section]
        )
        retry_index = None
        for current_tab_id, current_title, tab_body in iter_tab_bodies(fresh_doc):
            if tab_id and current_tab_id != tab_id:
                continue
            for sec_name in candidate_sections:
                idx, found_heading = find_insert_index(tab_body, sec_name)
                if idx is not None:
                    retry_index = idx
                    heading = found_heading
                    tab_id = current_tab_id
                    tab_title = current_title
                    break
            if retry_index is not None:
                break
        if retry_index is None and section == DEFAULT_SECTION:
            for current_tab_id, current_title, tab_body in iter_tab_bodies(fresh_doc):
                if tab_id and current_tab_id != tab_id:
                    continue
                idx, found_heading = _find_wo_section_in_body(tab_body)
                if idx is not None:
                    retry_index = idx
                    heading = found_heading
                    tab_id = current_tab_id
                    tab_title = current_title
                    break
        if retry_index is None:
            raise gdocs.SyncError(gdocs._explain(error)) from error
        insert_index = retry_index
        payload["index"] = insert_index
        retry_body = _insert_request(insert_index, line, tab_id)
        fresh_rev = fresh_doc.get("revisionId", "")
        if fresh_rev:
            retry_body["writeControl"] = {"requiredRevisionId": fresh_rev}
        try:
            docs.documents().batchUpdate(documentId=document_id, body=retry_body).execute()
        except Exception as retry_err:
            raise gdocs.SyncError(gdocs._explain(retry_err)) from retry_err

    # --- Self-verification (Docs API, strongly consistent) ------------------
    # `gdocs.sync` exports via the Drive API, which lags 0.5–2 s behind a write
    # (eventual consistency). Check the line directly via `documents.get`
    # instead — after `batchUpdate`, the Docs API returns the new revision
    # immediately. No `sleep`, no guessing.
    meta = drive.files().get(fileId=document_id, fields="modifiedTime").execute()
    mark = gdocs.STATUS_MARK.get(norm_status, "")
    doc_text = _tab_text_of(docs, document_id, tab_id)
    verified = any(
        f"Work Order {int(wo)} {separator} {mark}" in doc_text
        for separator in SEPARATORS
    )

    # Refresh index and cache for search and `atlas_status`, not for verification.
    gdocs.sync(root, sources=[target["source"]], force=True, conn=conn)

    from . import status as status_mod

    parsed = json.loads(status_mod.status(conn))
    listed = [item["wo"] for item in parsed["work_orders"]]

    payload.update({
        "written": True,
        "modified": meta.get("modifiedTime", ""),
        "verified": verified,
        "verified_via": "documents.get",
        "visible_work_orders": listed,
        **gate,
    })
    if verified and int(wo) not in listed:
        # The document has the line, the index does not yet: export lag.
        payload["index_lag"] = True
        # The Drive export is lagging: `sync` wrote the OLD text to the cache
        # but recorded the NEW `modifiedTime`. Without invalidating, every
        # subsequent `auto_sync` would consider the document unchanged and the
        # line would remain invisible indefinitely.
        from .indexer import invalidate_sync_state
        invalidate_sync_state(conn, document_id)
        payload["warning"] = (
            "Line is in the document, but not yet in the search index "
            "(Drive export is lagging). `atlas status` will show it shortly."
        )
    if not verified:
        payload["warning"] = (
            "Line was written, but could not be verified in the document. "
            f"Expected: 'Work Order {int(wo)} — {mark}'. Check formatting in the doc."
        )
    audit.log(root, "publish", wo=wo, status=norm_status, title=title,
              document=source["name"], tab=tab_title, index=insert_index,
              verified=verified, modified=meta.get("modifiedTime", ""))
    if verified and norm_status == "done":
        verify.record_wo_symbols(root, int(wo), verify_symbols)
    if conn is not None and not dry_run:
        from . import localwrite
        if localwrite.find_line(root, int(wo)) is not None:
            localwrite.set_status(root, conn, int(wo), norm_status, dry_run=False)
            payload["local_synced"] = True
        elif localwrite.targets(root):
            localwrite.publish(root, conn, int(wo), line, section, dry_run=False)
            payload["local_synced"] = True
    return payload


SEPARATORS = ["—", "–", "-", ":"]


def _status_line_re(wo: int):
    """Match a status line for this work order — not every casual mention.

    "Work Order 9 Sync (Tab 1): …" is not a status line; "Work Order 9 — ✅ …"
    is. The separator immediately following the number is the distinguishing mark.
    """
    separators = "|".join(re.escape(item) for item in SEPARATORS)
    return re.compile(rf"(?m)^\s*Work Order\s*{int(wo)}\s*(?:{separators})",
                      re.IGNORECASE)


def _mirror_task_to_gdocs(
    root: Path,
    conn,
    task_query: str,
    status: str,
    client_secret: str | None = None,
    wo: int | None = None,
) -> dict:
    """Best-effort mirror of a checkbox task toggle (`- [ ]` <-> `- [x]`) to bound Google Docs."""
    if conn is None:
        return {"gdocs_task_synced": False}
    sources = [s for s in gdocs.known_sources(conn) if s["mimeType"] == gdocs.DOC_MIME]
    if not sources:
        return {"gdocs_task_synced": False}
    try:
        creds = gdocs.load_credentials(
            interactive=False, client_secret=client_secret, scopes=gdocs.WRITE_SCOPES
        )
        _drive, _sheets, docs = gdocs.build_services(creds)
    except Exception as exc:
        return {"gdocs_task_synced": False, "gdocs_note": str(exc)}

    from . import localwrite

    needle = (task_query or "").strip().casefold()
    if not needle:
        return {"gdocs_task_synced": False}
    norm_status = _norm_status(status)
    target_char = "x" if norm_status == "done" else " "

    for source in sources:
        for attempt in range(2):
            try:
                document = _get_gdoc(docs, source["id"])
            except Exception:
                break
            revision = document.get("revisionId", "")
            retry_conflict = False

            for tab_id, _tab_title, body in iter_tab_bodies(document):
                text = tab_text(body)
                wo_ctx = localwrite._wo_context_map(text)
                candidates: list[tuple[tuple[int, int, int], re.Match, int | None]] = []
                for match in localwrite._TASK_RE.finditer(text):
                    if needle not in match.group("rest").casefold():
                        continue
                    enc_wo, enc_open = localwrite._enclosing_wo_for_offset(wo_ctx, match.start())
                    if wo is not None and enc_wo != int(wo):
                        continue
                    current_mark = match.group("mark").lower()
                    needs_toggle = 0 if current_mark != target_char else 1
                    wo_open_rank = 0 if enc_open else 2
                    candidates.append(((needs_toggle, wo_open_rank, match.start()), match, enc_wo))

                if not candidates:
                    continue
                candidates.sort(key=lambda item: item[0])
                _rank, best, best_wo = candidates[0]
                if best.group("mark").lower() == target_char:
                    return {"gdocs_task_synced": True, "gdocs_unchanged": True}

                old_line = best.group(0).strip()
                new_line = f"{best.group('prefix').lstrip()}[{target_char}]{best.group('rest')}".rstrip()
                effective_wo = int(wo) if wo is not None else best_wo
                try:
                    if effective_wo is not None and _body_has_indices(body):
                        changed = _replace_in_body_ranges(
                            docs,
                            source["id"],
                            revision,
                            body,
                            old_line,
                            new_line,
                            tab_id=tab_id,
                            wo=effective_wo,
                            max_replacements=1,
                        )
                    else:
                        changed, _ = _replace_all(
                            docs,
                            source["id"],
                            revision,
                            old_line,
                            new_line,
                            tab_id=tab_id,
                            tab_body=body,
                            wo=effective_wo,
                        )
                    if changed:
                        gdocs.sync(root, sources=[source], force=True, conn=conn)
                        from .indexer import invalidate_sync_state

                        invalidate_sync_state(conn, source["id"])
                        return {"gdocs_task_synced": True, "gdocs_changed": changed}
                except Exception as exc:
                    if attempt == 0 and _is_revision_conflict(exc):
                        retry_conflict = True
                        break
                    return {"gdocs_task_synced": False, "gdocs_note": str(exc)}
            if not retry_conflict:
                break
    return {"gdocs_task_synced": False}


def set_status(
    root: Path,
    conn,
    wo: int | None = None,
    status: str = "done",
    dry_run: bool = False,
    client_secret: str | None = None,
    verify_symbols: list[str] | None = None,
    task: str | None = None,
    sync_gdocs: bool = False,
) -> dict:
    """Change the status of an already documented work order (✅ ⇄ ⬜) or checkbox task (`- [ ]` ⇄ `- [x]`).

    Local Git Markdown (`docs/ARCHITECTURE.md`) is updated as the authoritative
    source of truth. Google Docs is only modified when `sync_gdocs=True`
    (`ATLAS_SYNC_GDOCS=1`) or when the workspace has no local Markdown files.
    """
    from . import localwrite, status as status_mod

    norm_status = _norm_status(status)
    new_mark = gdocs.STATUS_MARK[norm_status]
    local_targets = localwrite.targets(root)
    mirror_gdocs = should_sync_gdocs(sync_gdocs) or not local_targets

    if wo is None and not (task and str(task).strip()):
        raise ValueError("Provide either 'wo' (Work Order number) or 'task' (checkbox text)")

    if task and str(task).strip():
        task_str = str(task).strip()
        gate, refusal = _attachment_gate(root, None, norm_status, verify_symbols, dry_run)
        if refusal:
            audit.log(root, "set_status_refused", wo=wo, task=task, status=norm_status,
                      symbols=[e["symbol"] for e in gate["verify"].get("failed", [])],
                      refused=True, written=False, verified=False)
            res_refused = {
                "changed": False,
                "refused": True,
                "refusal": refusal,
                "task": task_str,
                "status": norm_status,
                **gate,
            }
            if wo is not None:
                res_refused["wo"] = int(wo)
            return res_refused
        result = localwrite.set_task_status(
            root, conn, task_str, norm_status, dry_run=dry_run, wo=wo,
        )
        # If the step belongs to a legacy GDD-only Work Order not yet in local Markdown,
        # promote that Work Order into `docs/ARCHITECTURE.md` and toggle the step locally.
        if (
            not result.get("verified")
            and not result.get("unchanged")
            and conn is not None
            and local_targets
        ):
            needle = task_str.casefold()
            candidates = [
                item
                for item in status_mod.extract_work_orders(conn)
                if (wo is None or item["wo"] == int(wo))
                and localwrite.find_line(root, int(item["wo"])) is None
                and any(needle in (s.get("text") or "").casefold() for s in (item.get("steps") or []))
            ]
            candidates.sort(key=lambda item: (0 if item.get("status") == "open" else 1, item["wo"]))
            if candidates:
                cand = candidates[0]
                if dry_run:
                    result = {
                        "written": False,
                        "changed": True,
                        "dry_run": True,
                        "promoted_from_gdd": True,
                        "wo": int(cand["wo"]),
                        "task": task_str,
                        "status": norm_status,
                        "source": "local",
                    }
                else:
                    _promote_wo_to_local(root, conn, cand, dry_run=False)
                    result = localwrite.set_task_status(
                        root, conn, task_str, norm_status, dry_run=False, wo=int(cand["wo"])
                    )
                    result["promoted_from_gdd"] = True
        if not dry_run and conn is not None and mirror_gdocs:
            mirror_task = _mirror_task_to_gdocs(
                root, conn, task_str, norm_status,
                client_secret=client_secret, wo=wo,
            )
            result.update(mirror_task)
            if mirror_task.get("gdocs_changed"):
                result["changed"] = True
                result["written"] = True
                result["verified"] = True
        if wo is not None and "wo" not in result:
            result["wo"] = int(wo)
        return {**result, **gate}

    # Flipping a work order to ✅ is also an acceptance — run the same gate as above.
    gate, refusal = _attachment_gate(root, wo, norm_status, verify_symbols, dry_run)
    if refusal:
        audit.log(root, "set_status_refused", wo=wo, status=norm_status,
                  symbols=[e["symbol"] for e in gate["verify"].get("failed", [])],
                  refused=True, written=False, verified=False)
        return {
            "changed": False,
            "refused": True,
            "refusal": refusal,
            "wo": int(wo),
            "status": norm_status,
            **gate,
        }

    items = status_mod.extract_work_orders(conn)
    current = next((item for item in items if item["wo"] == int(wo)), None)

    # Unchecked steps gate: if a Work Order defines `- [ ]` steps, all steps must
    # be checked off before the Work Order itself can be marked `done`.
    if norm_status == "done" and current is not None and not dry_run:
        open_steps = [s["text"] for s in (current.get("steps") or []) if not s.get("done")]
        if open_steps:
            refusal_msg = (
                f"Acceptance refused — Work Order {int(wo)} still has "
                f"{len(open_steps)} open step(s):\n"
                + "\n".join(f"  ✗ [ ] {step}" for step in open_steps)
                + "\n\nComplete and check off each step (`atlas_set_status(task=..., status='done')`) "
                "before marking the Work Order done."
            )
            audit.log(root, "set_status_refused", wo=wo, status=norm_status,
                      open_steps=open_steps, refused=True, written=False, verified=False)
            return {
                "changed": False,
                "refused": True,
                "refusal": refusal_msg,
                "wo": int(wo),
                "status": norm_status,
                "open_steps": open_steps,
                **gate,
            }

    # Local-first Single Source of Truth: update local Markdown (`docs/ARCHITECTURE.md`)
    # directly when local docs exist and Google Docs write-mirroring is not opted in.
    if not gdocs.known_sources(conn) or not mirror_gdocs:
        if current is not None and current["status"] == norm_status:
            return {"changed": False, "wo": int(wo), "status": norm_status,
                    "reason": "Status is already set", **gate}
        if local_targets and current is not None and localwrite.find_line(root, int(wo)) is None:
            promoted = _promote_wo_to_local(
                root,
                conn,
                current,
                status_override=norm_status,
                mark_all_steps_done=(norm_status == "done"),
                dry_run=dry_run,
            )
            promoted.update({
                "changed": not dry_run,
                "status": norm_status,
                "promoted_from_gdd": True,
            })
            if not dry_run and promoted.get("verified") and norm_status == "done":
                verify.record_wo_symbols(root, int(wo), verify_symbols)
            return {**promoted, **gate}
        result = localwrite.set_status(root, conn, int(wo), norm_status, dry_run=dry_run)
        if not dry_run and result.get("verified") and norm_status == "done":
            verify.record_wo_symbols(root, int(wo), verify_symbols)
        return {**result, **gate}

    old_marks: list[str] = []
    if current is not None:
        if current["status"] == norm_status:
            return {"changed": False, "wo": int(wo), "status": norm_status,
                    "reason": "Status is already set"}
        marks = gdocs.MARKS_BY_STATUS.get(current["status"])
        if not marks:
            raise gdocs.SyncError(
                f"Work Order {wo} has no recognisable status ('{current['status']}') — "
                "check the document by hand."
            )
        # Both spellings: existing documents use ERLEDIGT/OFFEN,
        # new ones use DONE/OPEN.
        old_marks = list(marks)
    else:
        # The index can lag behind the live document (`index_lag` after
        # `publish`). When Google sources are known, inspect the live doc first
        # (including German ERLEDIGT/OFFEN variants) instead of aborting.
        old_marks = [
            mark
            for key, marks in gdocs.MARKS_BY_STATUS.items()
            if key != norm_status
            for mark in marks
        ]

    creds = gdocs.load_credentials(interactive=False, client_secret=client_secret,
                                   scopes=gdocs.WRITE_SCOPES)
    _drive, _sheets, docs = gdocs.build_services(creds)

    sources = [s for s in gdocs.known_sources(conn) if s["mimeType"] == gdocs.DOC_MIME]
    samples: list[str] = []
    for source in sources:
        gdocs.assert_writable(conn, source["id"])
        document = _get_gdoc(docs, source["id"])
        revision = document.get("revisionId", "")

        tab_texts = [
            (tab_id, tab_title, tab_text(body))
            for tab_id, tab_title, body in iter_tab_bodies(document)
        ]
        haystack = "\n".join(text for _, _, text in tab_texts)

        # Only pick a separator that actually occurs in the document. Otherwise
        # dry-run would stop on the first candidate and report "(unscoped)"
        # even when the line simply uses a different separator.
        old_text = ""
        new_text = ""
        for candidate_mark in old_marks:
            for separator in SEPARATORS:
                candidate = f"Work Order {int(wo)} {separator} {candidate_mark}"
                if candidate in haystack:
                    old_text = candidate
                    new_text = f"Work Order {int(wo)} {separator} {new_mark}"
                    break
            if old_text:
                break
        if not old_text:
            same_marks = gdocs.MARKS_BY_STATUS.get(norm_status, (new_mark,))
            if current is None and any(
                f"Work Order {int(wo)} {separator} {cand}" in haystack
                for cand in same_marks
                for separator in SEPARATORS
            ):
                return {
                    "changed": False,
                    "written": False,
                    "unchanged": True,
                    "wo": int(wo),
                    "status": norm_status,
                    "reason": "Status is already set",
                    **gate,
                }
            samples.extend(_work_order_lines(tab_texts, wo))
            continue

        tab_id, tab_title, tab_body = find_tab_body_with_text(document, old_text)
        payload = {
            "document_id": source["id"],
            "document_name": source["name"],
            "old": old_text,
            "new": new_text,
            "tab": tab_title or "(unscoped)",
            "tab_id": tab_id,
            "tab_locked": bool(tab_id),
            "dry_run": dry_run,
            **gate,
        }
        if dry_run:
            return payload

        try:
            changed, tab_locked = _replace_all(
                docs, source["id"], revision, old_text, new_text, tab_id,
                tab_body=tab_body, wo=int(wo),
            )
        except Exception as error:
            if not _is_revision_conflict(error):
                raise
            fresh_doc = _get_gdoc(docs, source["id"])
            fresh_rev = fresh_doc.get("revisionId", "")
            retry_tid, retry_ttitle, retry_tbody = find_tab_body_with_text(fresh_doc, old_text)
            if retry_tbody is not None:
                tab_id, tab_title, tab_body = retry_tid, retry_ttitle, retry_tbody
                payload["tab"] = tab_title or "(unscoped)"
                payload["tab_id"] = tab_id
                changed, tab_locked = _replace_all(
                    docs, source["id"], fresh_rev, old_text, new_text, tab_id,
                    tab_body=tab_body, wo=int(wo),
                )
            else:
                fresh_haystack = "\n".join(
                    tab_text(b) for _tid, _t, b in iter_tab_bodies(fresh_doc)
                )
                if new_text in fresh_haystack:
                    changed, tab_locked = 1, bool(tab_id)
                else:
                    raise
        if changed:
            if norm_status == "done" and current is not None:
                for step in current.get("steps") or []:
                    step_text = (step.get("text") or "").strip()
                    if step_text and step.get("done"):
                        try:
                            fresh_doc = _get_gdoc(docs, source["id"])
                            fresh_rev = fresh_doc.get("revisionId", "")
                            fresh_body = next(
                                (
                                    b
                                    for tid, _ttitle, b in iter_tab_bodies(fresh_doc)
                                    if tid == tab_id
                                ),
                                None,
                            )
                            if fresh_body is not None:
                                _replace_all(
                                    docs,
                                    source["id"],
                                    fresh_rev,
                                    f"- [ ] {step_text}",
                                    f"- [x] {step_text}",
                                    tab_id,
                                    tab_body=fresh_body,
                                    wo=int(wo),
                                )
                        except Exception:
                            pass
            gdocs.sync(root, sources=[source], force=True, conn=conn)
            parsed = json.loads(status_mod.status(conn))
            now = next((i for i in parsed["work_orders"] if i["wo"] == int(wo)), None)
            # Verify via the Docs API: the index may still hold the old revision
            # while the Drive export catches up.
            live_text = _tab_text_of(docs, source["id"], tab_id)
            payload.update({
                "changed": True,
                "occurrences": changed,
                "tab_locked": tab_locked,
                "verified": new_text in live_text,
                "verified_via": "documents.get",
                "current_status": now["status"] if now else "unknown",
            })
            if payload["verified"] and (now is None or now["status"] != norm_status):
                payload["index_lag"] = True
                from .indexer import invalidate_sync_state

                invalidate_sync_state(conn, source["id"])
                shown = now["status"] if now else "unknown"
                payload["warning"] = (
                    f"Document was updated, but the index still shows '{shown}' "
                    "(Drive export is lagging)."
                )
            audit.log(root, "set_status", wo=wo, old=old_text, new=new_text,
                      document=source["name"], tab=tab_title,
                      occurrences=changed, tab_locked=tab_locked,
                      verified=payload["verified"])
            if payload["verified"] and norm_status == "done":
                verify.record_wo_symbols(root, int(wo), verify_symbols)
            if conn is not None and not dry_run:
                from . import localwrite
                if localwrite.find_line(root, int(wo)) is not None:
                    localwrite.set_status(root, conn, int(wo), norm_status, dry_run=False)
                    payload["local_synced"] = True
            return payload

        raise gdocs.SyncError(
            f"Line '{old_text}' is in tab '{tab_title}', but could not be "
            "replaced — likely split across multiple text runs. Check the doc manually."
        )

    if conn is not None:
        from . import localwrite
        found_local = localwrite.find_line(root, int(wo))
        if found_local is not None:
            local_res = localwrite.set_status(root, conn, int(wo), norm_status, dry_run=dry_run)
            if not dry_run and local_res.get("verified"):
                if norm_status == "done":
                    verify.record_wo_symbols(root, int(wo), verify_symbols)
                refreshed = localwrite.find_line(root, int(wo))
                if refreshed is not None:
                    mirror = _mirror_to_gdocs(
                        root, conn, int(wo), refreshed[2].group(0).strip(), DEFAULT_SECTION
                    )
                    if mirror.get("gdocs_existing"):
                        mirror.update(
                            _mirror_status_to_gdocs(
                                root, conn, int(wo), norm_status, client_secret=client_secret
                            )
                        )
                    local_res.update(mirror)
            return {**local_res, **gate}

    if current is None:
        raise gdocs.SyncError(
            f"Work Order {wo} is not documented. Run `atlas publish --wo {wo} …` first."
        )
    hint = f" Found instead: {'; '.join(samples)}" if samples else ""
    raise gdocs.SyncError(
        f"Status line for Work Order {wo} not found in any document "
        f"(expected: 'Work Order {wo} — {old_marks[0]}').{hint}"
    )


def _work_order_lines(tab_texts: list[tuple[str, str, str]], wo: int,
                      limit: int = 3) -> list[str]:
    """Return lines mentioning 'Work Order N' as a diagnostic hint for errors."""
    needle = f"work order {int(wo)}"
    found: list[str] = []
    for _tab_id, title, text in tab_texts:
        for line in text.splitlines():
            stripped = line.strip()
            if needle in stripped.casefold() and stripped:
                found.append(f"'{stripped[:70]}' (Tab {title})")
                if len(found) >= limit:
                    return found
    return found


def _mirror_status_to_gdocs(
    root: Path,
    conn,
    wo: int,
    status: str,
    client_secret: str | None = None,
) -> dict:
    """Best-effort sync of an existing Work Order header status (`✅ DONE` ⇄ `⬜ OPEN`) in bound Google Docs."""
    if conn is None:
        return {"gdocs_status_synced": False}
    sources = [s for s in gdocs.known_sources(conn) if s["mimeType"] == gdocs.DOC_MIME]
    if not sources:
        return {"gdocs_status_synced": False}
    try:
        creds = gdocs.load_credentials(
            interactive=False, client_secret=client_secret, scopes=gdocs.WRITE_SCOPES
        )
        _drive, _sheets, docs = gdocs.build_services(creds)
    except Exception as exc:
        return {"gdocs_status_synced": False, "gdocs_note": str(exc)}

    norm_status = _norm_status(status)
    new_mark = gdocs.STATUS_MARK[norm_status]
    same_marks = gdocs.MARKS_BY_STATUS.get(norm_status, (new_mark,))
    old_marks = [
        mark
        for key, marks in gdocs.MARKS_BY_STATUS.items()
        if key != norm_status
        for mark in marks
    ]

    for source in sources:
        for attempt in range(2):
            try:
                document = _get_gdoc(docs, source["id"])
            except Exception:
                break
            revision = document.get("revisionId", "")
            haystack = "\n".join(
                tab_text(body) for _tid, _title, body in iter_tab_bodies(document)
            )
            old_text = ""
            new_text = ""
            for candidate_mark in old_marks:
                for separator in SEPARATORS:
                    candidate = f"Work Order {int(wo)} {separator} {candidate_mark}"
                    if candidate in haystack:
                        old_text = candidate
                        new_text = f"Work Order {int(wo)} {separator} {new_mark}"
                        break
                if old_text:
                    break
            if not old_text:
                if any(
                    f"Work Order {int(wo)} {separator} {cand}" in haystack
                    for cand in same_marks
                    for separator in SEPARATORS
                ):
                    return {"gdocs_status_synced": True, "gdocs_status_changed": False}
                break

            tab_id, _tab_title, tab_body = find_tab_body_with_text(document, old_text)
            try:
                changed, _ = _replace_all(
                    docs, source["id"], revision, old_text, new_text, tab_id,
                    tab_body=tab_body, wo=int(wo),
                )
                if changed:
                    gdocs.sync(root, sources=[source], force=True, conn=conn)
                    from .indexer import invalidate_sync_state

                    invalidate_sync_state(conn, source["id"])
                    return {"gdocs_status_synced": True, "gdocs_status_changed": True}
                break
            except Exception as exc:
                if attempt == 0 and _is_revision_conflict(exc):
                    continue
                return {"gdocs_status_synced": False, "gdocs_note": str(exc)}
    return {"gdocs_status_synced": False}


def _mirror_to_gdocs(root: Path, conn, wo: int, text_block: str, section: str) -> dict:
    """Best-effort mirror of a locally planned/updated Work Order into a bound Google Doc."""
    if conn is None:
        return {"gdocs_synced": False}
    sources = [s for s in gdocs.known_sources(conn) if s["mimeType"] == gdocs.DOC_MIME]
    if not sources:
        return {"gdocs_synced": False}
    try:
        creds = gdocs.load_credentials(interactive=False, scopes=gdocs.WRITE_SCOPES)
        _drive, _sheets, docs = gdocs.build_services(creds)
    except Exception as exc:
        return {"gdocs_synced": False, "gdocs_note": str(exc)}

    candidate_sections = list(dict.fromkeys([
        section, *FALLBACK_SECTIONS
    ]))
    for source in sources:
        for attempt in range(2):
            try:
                document = _get_gdoc(docs, source["id"])
            except Exception:
                break

            live_text = "\n".join(
                tab_text(body) for _tid, _title, body in iter_tab_bodies(document)
            )
            if _status_line_re(int(wo)).search(live_text):
                return {"gdocs_synced": True, "gdocs_document": source["name"], "gdocs_existing": True}

            insert_index = None
            tab_id = ""
            for current_tab_id, _current_title, tab_body in iter_tab_bodies(document):
                for sec_name in candidate_sections:
                    idx, _found_heading = find_insert_index(tab_body, sec_name)
                    if idx is not None:
                        insert_index = idx
                        tab_id = current_tab_id
                        break
                if insert_index is not None:
                    break
            if insert_index is None:
                for current_tab_id, _current_title, tab_body in iter_tab_bodies(document):
                    idx, _found_heading = _find_wo_section_in_body(tab_body)
                    if idx is not None:
                        insert_index = idx
                        tab_id = current_tab_id
                        break

            if insert_index is None:
                break

            revision = document.get("revisionId", "")
            body = _insert_request(insert_index, text_block, tab_id)
            if revision:
                body["writeControl"] = {"requiredRevisionId": revision}
            try:
                docs.documents().batchUpdate(documentId=source["id"], body=body).execute()
                gdocs.sync(root, sources=[source], force=True, conn=conn)
                from .indexer import invalidate_sync_state
                invalidate_sync_state(conn, source["id"])
                return {"gdocs_synced": True, "gdocs_document": source["name"]}
            except Exception as exc:
                if attempt == 0 and _is_revision_conflict(exc):
                    continue
                return {"gdocs_synced": False, "gdocs_note": str(exc)}

    return {"gdocs_synced": False}


_WO_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "und", "der",
    "die", "das", "für", "fuer", "fur", "von", "mit", "aus", "auf", "bei", "den", "dem",
}
_GENERIC_STEP_BOILERPLATE = {
    "add unit tests",
    "write unit tests",
    "add tests",
    "write tests",
    "run tests",
    "unit tests",
    "tests ergänzen",
    "unit-tests ergänzen",
}


def _normalize_wo_title(title: str) -> tuple[str, set[str]]:
    from . import normalize as normalize_mod

    raw = re.sub(r"(?:ae|oe|ue)", lambda m: {"ae": "a", "oe": "o", "ue": "u"}[m.group(0)], (title or "").casefold())
    folded = normalize_mod.fold(raw)
    cleaned = re.sub(r"[^\w\s]+", " ", folded)
    words = [w for w in cleaned.split() if w]
    norm = " ".join(words)
    sig_words = {w for w in words if len(w) >= 3 and w not in _WO_STOPWORDS}
    return norm, sig_words


def _find_duplicate_wo(
    existing_wos: list[dict],
    topic: str,
    steps: list[str] | None = None,
) -> dict | None:
    """Return an existing Work Order whose title matches or strongly overlaps `topic` (unless `steps` are disjoint follow-up tasks)."""
    from . import status as status_mod

    norm_topic, topic_words = _normalize_wo_title(topic)
    if not norm_topic:
        return None
    new_steps_all = {s.strip().casefold() for s in (steps or []) if s and s.strip()}
    new_steps_sig = new_steps_all - _GENERIC_STEP_BOILERPLATE or new_steps_all
    for item in existing_wos:
        if status_mod._is_boilerplate_wo(item):
            continue
        if item.get("status") != "open":
            continue
        cand_title = (item.get("title") or "").strip()
        if not cand_title:
            continue
        norm_cand, cand_words = _normalize_wo_title(cand_title)
        if not norm_cand:
            continue
        title_matched = norm_cand == norm_topic
        if not title_matched and len(topic_words) >= 3 and len(cand_words) >= 3:
            common = len(topic_words & cand_words)
            overlap = common / min(len(topic_words), len(cand_words))
            jaccard = common / len(topic_words | cand_words)
            if overlap >= 0.85 and jaccard >= 0.75:
                title_matched = True
        if not title_matched:
            continue
        existing_steps_all = {
            (s.get("text") or "").strip().casefold()
            for s in (item.get("steps") or [])
            if (s.get("text") or "").strip()
        }
        existing_steps_sig = existing_steps_all - _GENERIC_STEP_BOILERPLATE or existing_steps_all
        if new_steps_sig and existing_steps_sig and new_steps_sig.isdisjoint(existing_steps_sig):
            continue
        return item
    return None


def _next_free_wo_number(root: Path, existing_wos: list[dict]) -> int:
    """Compute the next free Work Order number across indexed docs, `wo-symbols.yml`, and `mutations.log`."""
    numbers: set[int] = {int(item["wo"]) for item in existing_wos if item.get("wo") is not None}
    try:
        numbers.update(int(k) for k in verify.load_wo_symbols(root).keys())
    except Exception:
        pass
    try:
        for entry in audit.tail(root, limit=0, only_wo=True):
            if entry.get("refused") or entry.get("verified") is False:
                continue
            raw_wo = entry.get("wo")
            if raw_wo is not None:
                numbers.add(int(raw_wo))
    except Exception:
        pass
    return max(numbers, default=0) + 1


def plan(
    root: Path,
    conn,
    topic: str,
    steps: list[str] | None = None,
    wo: int | None = None,
    section: str = DEFAULT_SECTION,
    verify_symbols: list[str] | None = None,
    dry_run: bool = False,
    sync_gdocs: bool = False,
) -> dict:
    """Create a new `⬜ OPEN` Work Order linked to the matching spec section (`atlas_where`) and checklist steps."""
    from . import brief as brief_mod, localwrite, search as search_mod, status as status_mod

    existing_wos = status_mod.extract_work_orders(conn)
    if wo is None:
        dup = _find_duplicate_wo(existing_wos, topic, steps=steps)
        if dup is not None:
            if verify_symbols and not dry_run:
                verify.record_wo_symbols(root, int(dup["wo"]), verify_symbols)
            return {
                "written": False,
                "unchanged": True,
                "deduplicated": True,
                "verified": True,
                "wo": int(dup["wo"]),
                "status": dup.get("status", "open"),
                "title": dup.get("title") or topic.strip(),
                "spec_anchor": dup.get("spec_anchor", ""),
                "steps": [s["text"] for s in (dup.get("steps") or [])],
                "verify_symbols": [s.strip() for s in (verify_symbols or []) if s and s.strip()],
                "message": (
                    f"Work Order {dup['wo']} ('{dup.get('title')}') already covers this topic — "
                    "reusing existing Work Order instead of creating a duplicate."
                ),
            }
        wo_num = _next_free_wo_number(root, existing_wos)
    else:
        wo_num = int(wo)

    # Locate the matching specification section automatically (just like `atlas_where`),
    # skipping the Work Orders logbook section itself so we point to the real spec.
    hits = search_mod.run_search(conn, topic, k=5)
    spec_hit = next(
        (
            h for h in hits
            if "work order" not in (h.get("heading") or "").casefold()
            and not (h.get("anchor") or "").endswith("#3-work-orders")
        ),
        hits[0] if hits else None,
    )

    spec_anchor = ""
    spec_origin = ""
    implements: list[str] = []
    if spec_hit is not None:
        spec_anchor = spec_hit["anchor"].split("~")[0]
        spec_origin = f"{spec_hit['path']}:{spec_hit['line']}"
        links_data = json.loads(search_mod.links(conn, spec_anchor))
        for item in brief_mod._parse_list(links_data.get("implements", "")):
            if item not in implements:
                implements.append(item)
        body_data = json.loads(search_mod.read(conn, spec_anchor, max_chars=2000))
        for found in brief_mod.FILE_RE.finditer(body_data.get("body", "")):
            rel = found.group(0).replace("res://", "")
            if rel not in implements:
                implements.append(rel)

    desc_parts: list[str] = []
    if spec_anchor:
        desc_parts.append(f"Spec `{spec_anchor}` ({spec_origin})")
    if implements:
        desc_parts.append("Files: " + ", ".join(f"`{f}`" for f in implements[:5]))
    if verify_symbols:
        clean_syms = [s.strip() for s in verify_symbols if s and s.strip()]
        if clean_syms:
            desc_parts.append("Symbols: " + ", ".join(f"`{s}`" for s in clean_syms))

    header = format_line(
        wo_num,
        "open",
        topic.strip(),
        " · ".join(desc_parts) if desc_parts else "Implement according to project documentation.",
    )
    clean_steps = [s.strip() for s in (steps or []) if s and s.strip()]
    block_lines = [header] + [f"- [ ] {step}" for step in clean_steps]
    block = "\n".join(block_lines)

    result = localwrite.publish(root, conn, wo_num, block, section, dry_run=dry_run)
    if not dry_run and result.get("verified"):
        if verify_symbols:
            verify.record_wo_symbols(root, wo_num, verify_symbols)
        if should_sync_gdocs(sync_gdocs):
            result.update(_mirror_to_gdocs(root, conn, wo_num, block, section))

    result.update({
        "wo": wo_num,
        "title": topic.strip(),
        "spec_anchor": spec_anchor,
        "spec_origin": spec_origin,
        "implements": implements,
        "steps": clean_steps,
        "verify_symbols": [s.strip() for s in (verify_symbols or []) if s and s.strip()],
    })
    return result

