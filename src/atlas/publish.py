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
import re
from pathlib import Path

from . import audit, gdocs, verify

DEFAULT_SECTION = "Work Orders"


def format_line(wo: int, status: str, title: str, description: str = "") -> str:
    """'Work Order 7 — ✅ DONE (Title): Description'"""
    mark = gdocs.STATUS_MARK.get(status.lower())
    if mark is None:
        raise ValueError(f"Unknown status '{status}' (allowed: done, open)")
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


def tab_text(body: dict) -> str:
    """Concatenate all text runs of a tab body into a single string."""
    chunks: list[str] = []
    for element in (body or {}).get("content") or []:
        paragraph = element.get("paragraph") or {}
        for item in paragraph.get("elements") or []:
            run = item.get("textRun")
            if run:
                chunks.append(run.get("content", ""))
    return "".join(chunks)


def find_tab_with_text(document: dict, needle: str) -> tuple[str, str]:
    """Return (tab_id, tab_title) of the tab whose text contains `needle`."""
    for tab_id, title, body in iter_tab_bodies(document):
        if needle in tab_text(body):
            return tab_id, title
    return "", ""


def _replace_all(docs, document_id: str, revision: str, old_text: str,
                 new_text: str, tab_id: str = "") -> tuple[int, bool]:
    """Run `replaceAllText`, scoped to a single tab when possible.

    Without `tabsCriteria`, the Docs API searches **all** tabs: an identical
    status string in an archive tab would also be modified. Returns:
    (number of occurrences replaced, whether the tab lock took effect).
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

    if tab_id:
        changed = send(tab_id)
        if changed:
            return changed, True
        # The line may be split across multiple text runs — in that case the
        # tab filter misses, though the match is still unique across the doc.
    return send(""), False


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
    if status.lower() not in {"done", "erledigt"}:
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
) -> dict:
    line = format_line(wo, status, title, description)

    # Wiring gate: runs locally before any credential or network access.
    # An acceptance without a caller is never written — it would record an
    # intention rather than a delivered result (WO 8).
    gate, refusal = _attachment_gate(root, wo, status, verify_symbols, dry_run)
    if refusal:
        audit.log(root, "publish_refused", wo=wo, status=status, title=title,
                  symbols=[e["symbol"] for e in gate["verify"].get("failed", [])],
                  refused=True, written=False, verified=False)
        return {
            "written": False,
            "refused": True,
            "refusal": refusal,
            "wo": int(wo),
            "status": status.lower(),
            **gate,
        }

    # Idempotency: a work order exists at most once in the document.
    # Without this guard, a second `publish` appends a duplicate line — and
    # `status` picks the *first* entry, so the stale one wins. That happened on
    # Nov 5: four WOs appeared twice (⬜ OPEN + ✅ DONE), and `atlas status`
    # reported "4 open" even though all four were finished.
    from . import status as status_mod

    existing = next(
        (item for item in status_mod.extract_work_orders(conn) if item["wo"] == int(wo)),
        None,
    )
    if existing is not None:
        if existing["status"] == status.lower():
            return {
                "written": False,
                "unchanged": True,
                "wo": int(wo),
                "status": status.lower(),
                "message": (
                    f"Work Order {wo} is already recorded as '{status}' in the document — "
                    "nothing appended. Work orders are not duplicated."
                ),
                **gate,
            }
        # Different status: update in place instead of appending a second line.
        result = set_status(root, conn, wo, status=status, dry_run=dry_run,
                            client_secret=client_secret, verify_symbols=verify_symbols)
        result["reused_set_status"] = True
        result["message"] = (
            f"Work Order {wo} already existed as '{existing['status']}' — "
            "switched status instead of appending a second line."
        )
        return result

    # No Google document bound to this project: write locally to the managed
    # Markdown docs. Without this branch, the workflow `atlas init` sets up
    # ("status is a tool call") ended in an error for any user without a
    # Google account.
    if not gdocs.known_sources(conn):
        from . import localwrite

        result = localwrite.publish(
            root, conn, int(wo), format_line(wo, status, title, description),
            section, dry_run=dry_run,
        )
        if not dry_run and result.get("verified") and status.lower() in {"done", "erledigt"}:
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

        for current_tab_id, current_title, tab_body in iter_tab_bodies(document):
            index, found_heading = find_insert_index(tab_body, section)
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
                            client_secret=client_secret, verify_symbols=verify_symbols)
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
        raise gdocs.SyncError(gdocs._explain(error)) from error

    # --- Self-verification (Docs API, strongly consistent) ------------------
    # `gdocs.sync` exports via the Drive API, which lags 0.5–2 s behind a write
    # (eventual consistency). Check the line directly via `documents.get`
    # instead — after `batchUpdate`, the Docs API returns the new revision
    # immediately. No `sleep`, no guessing.
    meta = drive.files().get(fileId=document_id, fields="modifiedTime").execute()
    mark = gdocs.STATUS_MARK.get(status.lower(), "")
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
    audit.log(root, "publish", wo=wo, status=status, title=title,
              document=source["name"], tab=tab_title, index=insert_index,
              verified=verified, modified=meta.get("modifiedTime", ""))
    if verified and status.lower() in {"done", "erledigt"}:
        verify.record_wo_symbols(root, int(wo), verify_symbols)
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


def set_status(
    root: Path,
    conn,
    wo: int | None = None,
    status: str = "done",
    dry_run: bool = False,
    client_secret: str | None = None,
    verify_symbols: list[str] | None = None,
    task: str | None = None,
) -> dict:
    """Change the status of an already documented work order (✅ ⇄ ⬜) or checkbox task (`- [ ]` ⇄ `- [x]`).

    Uses `replaceAllText` rather than character offsets: matches the full
    status prefix ('Work Order 9 — ✅ ERLEDIGT'), which is unique in the document.
    The bare phrase 'Work Order 9' also appears in navigation and prose sections.
    """
    from . import status as status_mod

    new_mark = gdocs.STATUS_MARK.get(status.lower())
    if new_mark is None:
        raise ValueError(f"Unknown status '{status}' (allowed: done, open)")

    if wo is None and not (task and str(task).strip()):
        raise ValueError("Provide either 'wo' (Work Order number) or 'task' (checkbox text)")

    if wo is None and task and str(task).strip():
        from . import localwrite

        gate, refusal = _attachment_gate(root, None, status, verify_symbols, dry_run)
        if refusal:
            audit.log(root, "set_status_refused", task=task, status=status,
                      symbols=[e["symbol"] for e in gate["verify"].get("failed", [])],
                      refused=True, written=False, verified=False)
            return {
                "changed": False,
                "refused": True,
                "refusal": refusal,
                "task": str(task).strip(),
                "status": status.lower(),
                **gate,
            }
        result = localwrite.set_task_status(
            root, conn, str(task).strip(), status, dry_run=dry_run
        )
        return {**result, **gate}

    # Flipping a work order to ✅ is also an acceptance — run the same gate as above.
    gate, refusal = _attachment_gate(root, wo, status, verify_symbols, dry_run)
    if refusal:
        audit.log(root, "set_status_refused", wo=wo, status=status,
                  symbols=[e["symbol"] for e in gate["verify"].get("failed", [])],
                  refused=True, written=False, verified=False)
        return {
            "changed": False,
            "refused": True,
            "refusal": refusal,
            "wo": int(wo),
            "status": status.lower(),
            **gate,
        }

    items = status_mod.extract_work_orders(conn)
    current = next((item for item in items if item["wo"] == int(wo)), None)

    # Unchecked steps gate: if a Work Order defines `- [ ]` steps, all steps must
    # be checked off before the Work Order itself can be marked `done`.
    if status.lower() in {"done", "erledigt"} and current is not None and not dry_run:
        open_steps = [s["text"] for s in (current.get("steps") or []) if not s.get("done")]
        if open_steps:
            refusal_msg = (
                f"Acceptance refused — Work Order {int(wo)} still has "
                f"{len(open_steps)} open step(s):\n"
                + "\n".join(f"  ✗ [ ] {step}" for step in open_steps)
                + "\n\nComplete and check off each step (`atlas_set_status(task=..., status='done')`) "
                "before marking the Work Order done."
            )
            audit.log(root, "set_status_refused", wo=wo, status=status,
                      open_steps=open_steps, refused=True, written=False, verified=False)
            return {
                "changed": False,
                "refused": True,
                "refusal": refusal_msg,
                "wo": int(wo),
                "status": status.lower(),
                "open_steps": open_steps,
                **gate,
            }
    # No Google document bound to this project: write locally to the managed
    # Markdown docs. Without this branch, the workflow `atlas init` sets up
    # ("status is a tool call") ended in an error for any user without a
    # Google account.
    if not gdocs.known_sources(conn):
        from . import localwrite

        if current is not None and current["status"] == status.lower():
            return {"changed": False, "wo": int(wo), "status": status,
                    "reason": "Status is already set", **gate}
        result = localwrite.set_status(root, conn, int(wo), status, dry_run=dry_run)
        if not dry_run and result.get("verified") and status.lower() in {"done", "erledigt"}:
            verify.record_wo_symbols(root, int(wo), verify_symbols)
        return {**result, **gate}

    old_marks: list[str] = []
    if current is not None:
        if current["status"] == status.lower():
            return {"changed": False, "wo": int(wo), "status": status,
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
        # instead of aborting with SyncError on a stale index.
        old_marks = [
            mark for key, mark in gdocs.STATUS_MARK.items()
            if key != status.lower()
        ]

    creds = gdocs.load_credentials(interactive=False, client_secret=client_secret,
                                   scopes=gdocs.WRITE_SCOPES)
    _drive, _sheets, docs = gdocs.build_services(creds)

    sources = [s for s in gdocs.known_sources(conn) if s["mimeType"] == gdocs.DOC_MIME]
    samples: list[str] = []
    for source in sources:
        gdocs.assert_writable(conn, source["id"])
        try:
            document = docs.documents().get(
                documentId=source["id"], includeTabsContent=True
            ).execute()
        except TypeError:  # older client version does not support `includeTabsContent`
            document = docs.documents().get(documentId=source["id"]).execute()
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
            if current is None and any(
                f"Work Order {int(wo)} {separator} {new_mark}" in haystack
                for separator in SEPARATORS
            ):
                return {
                    "changed": False,
                    "written": False,
                    "unchanged": True,
                    "wo": int(wo),
                    "status": status.lower(),
                    "reason": "Status is already set",
                    **gate,
                }
            samples.extend(_work_order_lines(tab_texts, wo))
            continue

        tab_id, tab_title = find_tab_with_text(document, old_text)
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

        changed, tab_locked = _replace_all(
            docs, source["id"], revision, old_text, new_text, tab_id
        )
        if changed:
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
            if payload["verified"] and (now is None or now["status"] != status.lower()):
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
            if payload["verified"] and status.lower() in {"done", "erledigt"}:
                verify.record_wo_symbols(root, int(wo), verify_symbols)
            return payload

        raise gdocs.SyncError(
            f"Line '{old_text}' is in tab '{tab_title}', but could not be "
            "replaced — likely split across multiple text runs. Check the doc manually."
        )

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


def plan(
    root: Path,
    conn,
    topic: str,
    steps: list[str] | None = None,
    wo: int | None = None,
    section: str = DEFAULT_SECTION,
    verify_symbols: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Create a new `⬜ OPEN` Work Order linked to the matching spec section (`atlas_where`) and checklist steps."""
    from . import brief as brief_mod, localwrite, search as search_mod, status as status_mod

    existing_wos = status_mod.extract_work_orders(conn)
    if wo is None:
        wo_num = max((item["wo"] for item in existing_wos), default=0) + 1
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
    if not dry_run and result.get("verified") and verify_symbols:
        verify.record_wo_symbols(root, wo_num, verify_symbols)

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

