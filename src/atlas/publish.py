"""`atlas publish`: Work-Order-Statuszeile ins Master-Doc schreiben.

Drei Sicherungen, weil Docs-Schreiben fragiler ist als Lesen:
1. Append-only — es wird ausschließlich angehängt.
2. Revisionssperre (`requiredRevisionId`) — bei paralleler Änderung schlägt der
   Schreibvorgang fehl, statt an falscher Stelle zu landen.
3. Selbstverifikation — nach dem Schreiben wird die Zeile direkt über die Docs
   API (`documents.get`) gesucht. Der Drive-Export, aus dem der Index gefüllt
   wird, hinkt dem Write 0,5–2 s nach und taugt nicht als Urteil.
4. Anbindungs-Gate — eine Work Order mit `done` wird nur geschrieben, wenn ihre
   Kern-Symbole definiert **und** benutzt sind. Sonst: `written: false`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import audit, gdocs, verify

DEFAULT_SECTION = "Prototyp-Meilenstein-Plan"


def format_line(wo: int, status: str, title: str, description: str = "") -> str:
    """'Work Order 7 — ✅ ERLEDIGT (Titel): Beschreibung'"""
    mark = gdocs.STATUS_MARK.get(status.lower())
    if mark is None:
        raise ValueError(f"Unbekannter Status '{status}' (erlaubt: done, open)")
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
    """(tab_id, tab_title, body) für alle Tabs — inklusive verschachtelter.

    Google Docs Tabs werden von der API nur mit `includeTabsContent=True`
    geliefert. Ohne Tabs fällt alles auf `document["body"]` zurück.
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
    """Alle Text-Runs eines Tab-Bodys als ein String."""
    chunks: list[str] = []
    for element in (body or {}).get("content") or []:
        paragraph = element.get("paragraph") or {}
        for item in paragraph.get("elements") or []:
            run = item.get("textRun")
            if run:
                chunks.append(run.get("content", ""))
    return "".join(chunks)


def find_tab_with_text(document: dict, needle: str) -> tuple[str, str]:
    """(tab_id, tab_title) des Tabs, dessen Text `needle` enthält."""
    for tab_id, title, body in iter_tab_bodies(document):
        if needle in tab_text(body):
            return tab_id, title
    return "", ""


def _replace_all(docs, document_id: str, revision: str, old_text: str,
                 new_text: str, tab_id: str = "") -> tuple[int, bool]:
    """`replaceAllText`, wenn möglich auf einen Tab gesperrt.

    Ohne `tabsCriteria` durchsucht die Docs-API **alle** Tabs: derselbe
    Status-String in einem Archiv-Tab würde mitgeändert. Rückgabe:
    (Anzahl ersetzter Vorkommen, ob die Tab-Sperre gegriffen hat).
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
        # Zeile liegt evtl. über mehrere Text-Runs verteilt — dann greift die
        # Tab-Sperre nicht, der Treffer ist aber trotzdem eindeutig.
    return send(""), False


def _is_section_title(text: str, needle: str) -> bool:
    """True, wenn der Absatz den Begriff als Titel trägt — nicht nur erwähnt.

    „3. Prototyp-Meilenstein-Plan (5 Work Orders)" ist ein Titel.
    „Tab-ID: … | Fokus: … Prototyp-Meilenstein-Plan (5 Work Orders)." ist eine
    Navigationszeile und darf nicht als Anker dienen.
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
    """Sucht den Abschnitt in einem Body und liefert (einfüge_index, heading).

    Eingefügt wird nach dem letzten Absatz des Abschnitts — also vor dem nächsten
    Heading gleicher oder höherer Ebene.

    Zweistufig: zuerst echte `HEADING`-Absätze, dann manuell formatierte
    Überschriften (`NORMAL_TEXT`). In beiden Fällen muss der Begriff den Absatz
    *anführen* — Erwähnungen im Fließtext zählen nicht.
    """
    content = (body or {}).get("content") or []
    if not content:
        return None, None

    start = None
    ref_level = None

    # Pass 1: echte Überschriften, Pass 2: nur fett formatierte "Überschriften".
    for headings_only in (True, False):
        for index, element in enumerate(content):
            style = _paragraph_style(element)
            is_heading = style.startswith("HEADING")
            if headings_only and not is_heading:
                continue
            if not headings_only and is_heading:
                continue  # schon in Pass 1 geprüft
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
            # `endIndex` liegt HINTER dem abschließenden Umbruch. Eingefügt
            # wird davor, damit der neue Absatz vom Vorabsatz erbt und nicht
            # von der folgenden Überschrift (sonst wird die Work Order zur
            # HEADING_2).
            return max(1, end - 1), _paragraph_text(content[start]).strip()

    ends = [el.get("endIndex") for el in content[start:] if el.get("endIndex")]
    if not ends:
        return None, None
    # Die Position des abschließenden `\n` des letzten Absatzes: davor wird
    # eingefügt, mit führendem Umbruch im Text. Das frühere `line + "\n"`
    # setzte den Text VOR den Umbruch und klebte ihn ans letzte Wort
    # ("…TextWork Order 7 — ✅").
    return max(1, max(ends) - 1), _paragraph_text(content[start]).strip()


def _tab_text_of(docs, document_id: str, tab_id: str = "") -> str:
    """Text des Ziel-Tabs — direkt aus der Docs API, nicht aus dem Export."""
    try:
        document = docs.documents().get(
            documentId=document_id, includeTabsContent=True
        ).execute()
    except TypeError:  # ältere Client-Version kennt den Parameter nicht
        document = docs.documents().get(documentId=document_id).execute()

    texts = {tid: tab_text(body) for tid, _title, body in iter_tab_bodies(document)}
    if tab_id and tab_id in texts:
        return texts[tab_id]
    return "\n".join(texts.values())


def _attachment_gate(root: Path, wo: int, status: str,
                     verify_symbols: list[str] | None,
                     dry_run: bool) -> tuple[dict, str]:
    """Prüft die Anbindung, bevor geschrieben wird. Liefert (payload, refusal).

    Symbole kommen aus zwei Quellen: explizit übergeben und — wichtiger — aus
    `.atlas/wo-symbols.yml`. Hinterlegte Symbole werden **immer** geprüft, auch
    wenn der Aufrufer sie nicht nennt: Der Agent, der den Aufrufer vergisst,
    vergisst auch den Nachweis.
    """
    if status.lower() not in {"done", "erledigt"}:
        return {}, ""

    symbols = list(dict.fromkeys(
        [str(s) for s in (verify_symbols or []) if str(s).strip()]
        + verify.load_wo_symbols(root).get(int(wo), [])
    ))
    if not symbols:
        return {
            "verify": {
                "checked": 0,
                "ok": True,
                "warning": (
                    "Abnahme ohne Anbindungsnachweis: keine Symbole geprüft. "
                    "Für erledigte Work Orders in .atlas/wo-symbols.yml hinterlegen, "
                    "sonst ist keine spätere Regression erkennbar."
                ),
            }
        }, ""

    result = verify.check(root, symbols)
    if not result["ok"] and not dry_run:
        return {"verify": result}, verify.refusal(result)
    return {"verify": result}, ""


def _insert_request(insert_index: int, line: str, tab_id: str = "") -> dict:
    """Der Schreibvorgang: eigene Zeile, nicht am Vorabsatz klebend.

    Der führende Umbruch ist der Kern. Ohne ihn landet der Text VOR dem
    abschließenden Umbruch des Abschnitts und hängt am letzten Wort
    ("…AbschnittsWork Order 7 — ✅"). Mit ihm entsteht ein eigener Absatz, der
    den Stil des Vorabsatzes erbt — nicht den der Folge-Überschrift.
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

    # Anbindungs-Gate: läuft lokal, vor jedem Credential- oder Netz-Zugriff.
    # Eine Abnahme ohne Aufrufer wird nicht geschrieben — sie wäre eine
    # dokumentierte Absicht, kein Ergebnis (WO 8).
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

    # Idempotenz: eine Work Order existiert nur einmal im Dokument.
    # Ohne diese Sperre hängt ein zweiter `publish` eine zweite Zeile an — und
    # `status` nimmt den *ersten* Eintrag, also gewinnt der veraltete. Genau das
    # ist am 05.11. passiert: vier WOs standen doppelt (⬜ OFFEN + ✅ ERLEDIGT),
    # und `atlas status` meldete "4 offen", obwohl alle vier fertig waren.
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
                    f"Work Order {wo} steht bereits als '{status}' im Dokument — "
                    "nichts angehängt. Eine Work Order wird nicht doppelt geführt."
                ),
                **gate,
            }
        # Anderer Status: gezielt umstellen statt eine zweite Zeile anlegen.
        result = set_status(root, conn, wo, status=status, dry_run=dry_run,
                            client_secret=client_secret, verify_symbols=verify_symbols)
        result["reused_set_status"] = True
        result["message"] = (
            f"Work Order {wo} existierte bereits als '{existing['status']}' — "
            "Status umgestellt statt zweite Zeile angelegt."
        )
        return result

    # Kein Google-Dokument an dieses Projekt gebunden: lokal in die verwaltete
    # Markdown-Doku schreiben. Ohne diesen Zweig endete der Weg, den
    # `atlas init` beschreibt („Status ist ein Tool-Call"), für jeden Nutzer
    # ohne Google-Konto in einer Fehlermeldung.
    if not gdocs.known_sources(conn):
        from . import localwrite

        result = localwrite.publish(
            root, conn, int(wo), format_line(wo, status, title, description),
            section, dry_run=dry_run,
        )
        return {**result, **gate}

    creds = gdocs.load_credentials(interactive=False, client_secret=client_secret,
                                   scopes=gdocs.WRITE_SCOPES)
    drive, _sheets, docs = gdocs.build_services(creds)

    sources = [s for s in gdocs.known_sources(conn) if s["mimeType"] == gdocs.DOC_MIME]
    if not sources:
        raise gdocs.SyncError(
            "Kein Google-Doc als Quelle bekannt. Erst `atlas sync --query <Name>` ausführen."
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
            # Ältere Client-Version kennt den Parameter nicht.
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
            f"Abschnitt '{section}' in keinem Quell-Dokument gefunden. "
            "Mit `atlas outline` die Überschriften prüfen."
        )

    document_id = target["source"]["id"]
    gdocs.assert_writable(conn, document_id)

    # Zweite Sperre — diesmal gegen das LIVE-Dokument, nicht gegen den Index.
    # Der Index kann hinterherhinken (Drive-Export, siehe `index_lag`), das
    # Dokument nicht. Ohne diese Prüfung hängt ein zweiter `publish` im
    # Lag-Fenster eine zweite Zeile an dieselbe Work Order.
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
        "tab": tab_title or "(flach)",
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

    # --- Selbstverifikation (Docs API, strong consistent) -------------------
    # `gdocs.sync` exportiert über die Drive-API, und dieser Export hinkt dem
    # Write um 0,5–2 s nach (Eventual Consistency). Deshalb wird die Zeile
    # direkt über `documents.get` geprüft — nach `batchUpdate` liefert die Docs
    # API sofort die neue Revision. Kein `sleep`, kein Rateversuch.
    meta = drive.files().get(fileId=document_id, fields="modifiedTime").execute()
    mark = gdocs.STATUS_MARK.get(status.lower(), "")
    doc_text = _tab_text_of(docs, document_id, tab_id)
    verified = any(
        f"Work Order {int(wo)} {separator} {mark}" in doc_text
        for separator in SEPARATORS
    )

    # Index und Cache ziehen nach — für Suche und `atlas_status`, nicht fürs Urteil.
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
        # Das Dokument hat die Zeile, der Index noch nicht: der Export lag.
        payload["index_lag"] = True
        # Der Drive-Export hinkt: `sync` hat den ALTEN Text in den Cache
        # geschrieben, aber die NEUE modifiedTime vermerkt. Ohne diesen Schritt
        # hielte jeder spätere auto_sync das Dokument für unverändert und die
        # Zeile bliebe dauerhaft unsichtbar.
        from .indexer import invalidate_sync_state
        invalidate_sync_state(conn, document_id)
        payload["warning"] = (
            "Zeile steht im Dokument, ist aber noch nicht im Suchindex "
            "(Drive-Export hinkt nach). `atlas status` zeigt sie gleich."
        )
    if not verified:
        payload["warning"] = (
            "Zeile wurde geschrieben, steht aber nicht im Dokument. "
            f"Erwartet: 'Work Order {int(wo)} — {mark}'. Format im Doc prüfen."
        )
    audit.log(root, "publish", wo=wo, status=status, title=title,
              document=source["name"], tab=tab_title, index=insert_index,
              verified=verified, modified=meta.get("modifiedTime", ""))
    return payload


SEPARATORS = ["—", "–", "-", ":"]


def _status_line_re(wo: int):
    """Eine Statuszeile für diese Work Order — nicht jede Erwähnung.

    "Work Order 9 Sync (Tab 1): …" ist keine Statuszeile, "Work Order 9 — ✅ …"
    schon. Der Trenner direkt nach der Nummer ist das Unterscheidungsmerkmal.
    """
    separators = "|".join(re.escape(item) for item in SEPARATORS)
    return re.compile(rf"(?m)^\s*Work Order\s*{int(wo)}\s*(?:{separators})",
                      re.IGNORECASE)


def set_status(
    root: Path,
    conn,
    wo: int,
    status: str = "done",
    dry_run: bool = False,
    client_secret: str | None = None,
    verify_symbols: list[str] | None = None,
) -> dict:
    """Ändert den Status einer bereits dokumentierten Work Order (✅ ⇄ ⬜).

    Nutzt `replaceAllText` statt Zeichen-Indizes: gesucht wird der vollständige
    Status-String ('Work Order 9 — ✅ ERLEDIGT'), der im Dokument eindeutig ist.
    Der bloße Name 'Work Order 9' stünde auch in der Navigation und in 7.6.
    """
    from . import status as status_mod

    new_mark = gdocs.STATUS_MARK.get(status.lower())
    if new_mark is None:
        raise ValueError(f"Unbekannter Status '{status}' (erlaubt: done, open)")

    # Auch das Hochsetzen auf ✅ ist eine Abnahme — gleiche Prüfung wie oben.
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
    # Kein Google-Dokument an dieses Projekt gebunden: lokal in die verwaltete
    # Markdown-Doku schreiben. Ohne diesen Zweig endete der Weg, den
    # `atlas init` beschreibt („Status ist ein Tool-Call"), für jeden Nutzer
    # ohne Google-Konto in einer Fehlermeldung.
    if not gdocs.known_sources(conn):
        from . import localwrite

        if current is not None and current["status"] == status.lower():
            return {"changed": False, "wo": int(wo), "status": status,
                    "reason": "Status ist bereits gesetzt", **gate}
        result = localwrite.set_status(root, conn, int(wo), status, dry_run=dry_run)
        return {**result, **gate}

    old_marks: list[str] = []
    if current is not None:
        if current["status"] == status.lower():
            return {"changed": False, "wo": int(wo), "status": status,
                    "reason": "Status ist bereits gesetzt"}
        old_mark = gdocs.STATUS_MARK.get(current["status"])
        if old_mark is None:
            raise gdocs.SyncError(
                f"Work Order {wo} hat keinen erkennbaren Status ('{current['status']}') — "
                "im Doc von Hand prüfen."
            )
        old_marks = [old_mark]
    else:
        # Der Index kann hinter dem Live-Dokument herhinken (`index_lag` nach
        # `publish`). Deshalb bei bekannten Google-Quellen erst im Live-Doc
        # nachsehen, statt blind am veralteten Index mit SyncError abzubrechen.
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
        except TypeError:  # ältere Client-Version kennt den Parameter nicht
            document = docs.documents().get(documentId=source["id"]).execute()
        revision = document.get("revisionId", "")

        tab_texts = [
            (tab_id, tab_title, tab_text(body))
            for tab_id, tab_title, body in iter_tab_bodies(document)
        ]
        haystack = "\n".join(text for _, _, text in tab_texts)

        # Nur ein Separator, der im Dokument wirklich vorkommt. Sonst hätte der
        # Dry-Run beim ersten Kandidaten abgebrochen und "(nicht eingegrenzt)"
        # gemeldet, obwohl die Zeile einfach anders formatiert ist.
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
                    "reason": "Status ist bereits gesetzt",
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
            "tab": tab_title or "(nicht eingegrenzt)",
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
            # Urteil über die Docs API: der Index kann noch die alte Revision
            # führen, weil der Drive-Export nachhinkt.
            live_text = _tab_text_of(docs, source["id"], tab_id)
            payload.update({
                "changed": True,
                "occurrences": changed,
                "tab_locked": tab_locked,
                "verified": new_text in live_text,
                "verified_via": "documents.get",
                "current_status": now["status"] if now else "unbekannt",
            })
            if payload["verified"] and (now is None or now["status"] != status.lower()):
                payload["index_lag"] = True
                from .indexer import invalidate_sync_state

                invalidate_sync_state(conn, source["id"])
                shown = now["status"] if now else "unbekannt"
                payload["warning"] = (
                    f"Dokument ist umgestellt, der Index zeigt noch '{shown}' "
                    "(Drive-Export hinkt nach)."
                )
            audit.log(root, "set_status", wo=wo, old=old_text, new=new_text,
                      document=source["name"], tab=tab_title,
                      occurrences=changed, tab_locked=tab_locked,
                      verified=payload["verified"])
            return payload

        raise gdocs.SyncError(
            f"Zeile '{old_text}' steht im Tab '{tab_title}', ließ sich aber nicht "
            "ersetzen — vermutlich über mehrere Text-Runs verteilt. Im Doc prüfen."
        )

    if current is None:
        raise gdocs.SyncError(
            f"Work Order {wo} ist nicht dokumentiert. Erst `atlas publish --wo {wo} …`."
        )
    hint = f" Gefunden stattdessen: {'; '.join(samples)}" if samples else ""
    raise gdocs.SyncError(
        f"Statuszeile für Work Order {wo} in keinem Dokument gefunden "
        f"(erwartet: 'Work Order {wo} — {old_marks[0]}').{hint}"
    )


def _work_order_lines(tab_texts: list[tuple[str, str, str]], wo: int,
                      limit: int = 3) -> list[str]:
    """Zeilen, die 'Work Order N' erwähnen — als Hinweis für die Fehlermeldung."""
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
