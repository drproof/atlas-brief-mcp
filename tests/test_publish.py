from atlas.publish import _insert_request, find_insert_index, format_line


def test_format_done_with_title_and_description():
    line = format_line(7, "done", "Mission 1 E2E", "Räume 1–9 implementiert.")
    assert line == "Work Order 7 — ✅ ERLEDIGT (Mission 1 E2E): Räume 1–9 implementiert."


def test_format_open_without_description():
    assert format_line(8, "open", "Kamera") == "Work Order 8 — ⬜ OFFEN (Kamera):"


def test_format_rejects_unknown_status():
    try:
        format_line(1, "vielleicht", "X")
    except ValueError as error:
        assert "Status" in str(error)
    else:
        raise AssertionError("ValueError erwartet")


def _doc():
    def para(start, end, style, text):
        return {
            "startIndex": start,
            "endIndex": end,
            "paragraph": {
                "paragraphStyle": {"namedStyleType": style},
                "elements": [{"textRun": {"content": text}}],
            },
        }

    return {"body": {"content": [
        para(1, 20, "HEADING_1", "Tab 5: Kampagne\n"),
        para(20, 60, "HEADING_2", "3. Prototyp-Meilenstein-Plan\n"),
        para(60, 100, "NORMAL_TEXT", "Intro\n"),
        para(100, 150, "NORMAL_TEXT", "Work Order 5 Ende\n"),
        para(150, 190, "HEADING_2", "4. Nächster Abschnitt\n"),
        para(190, 210, "NORMAL_TEXT", "Inhalt\n"),
    ]}}


def test_insert_point_is_end_of_section():
    # Der Index zeigt auf den Umbruch, der den Abschnitt beendet — nicht
    # dahinter. Eingefügt wird dort mit führendem "\n", damit die neue Zeile
    # ein eigener Absatz wird und vom Vorabsatz erbt (nicht von der folgenden
    # Überschrift, sonst würde sie zur HEADING_2).
    index, heading = find_insert_index(_doc()["body"], "Prototyp-Meilenstein-Plan")
    assert index == 149
    assert "Prototyp" in heading


def test_section_until_document_end():
    doc = {"body": {"content": [
        {"startIndex": 1, "endIndex": 30,
         "paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_2"},
                       "elements": [{"textRun": {"content": "Letzter Abschnitt\n"}}]}},
        {"startIndex": 30, "endIndex": 80,
         "paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                       "elements": [{"textRun": {"content": "Text\n"}}]}},
    ]}}
    index, _ = find_insert_index(doc["body"], "Letzter Abschnitt")
    assert index == 79


def test_missing_section_returns_none():
    index, heading = find_insert_index(_doc()["body"], "gibt es nicht")
    assert index is None and heading is None


# --------------------------------------------------------------------------
# set_status: Tab-Sperre (tabsCriteria)
# --------------------------------------------------------------------------

import json

import pytest

from atlas import gdocs, publish
from atlas import status as status_mod


class _Request:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class FakeDocs:
    """Zwei Tabs mit identischem Status-String — genau der Fall für tabsCriteria."""

    def __init__(self, flat: bool = False):
        self.flat = flat
        # Flaches Dokument ohne Tabs: der Text muss sich beim Write ändern,
        # sonst simuliert der Fake eine API, die Writes verschluckt.
        self.flat_text = "Work Order 9 — ✅ ERLEDIGT (flach)\n"
        self.tabs = [
            {"id": "t.1", "title": "Tab 5 Kampagne",
             "text": "Work Order 9 — ✅ ERLEDIGT (Headless-Tests)\n"},
            {"id": "t.2", "title": "Archiv",
             "text": "Work Order 9 — ✅ ERLEDIGT (alte Fassung)\n"},
        ]
        self.calls: list[list[str]] = []

    def documents(self):
        return self

    def get(self, documentId=None, includeTabsContent=None):
        if self.flat:
            return _Request({"revisionId": "rev-1", "body": {"content": [
                {"paragraph": {"elements": [
                    {"textRun": {"content": self.flat_text}}
                ]}}
            ]}})
        return _Request({"revisionId": "rev-1", "tabs": [
            {
                "tabProperties": {"tabId": tab["id"], "title": tab["title"]},
                "documentTab": {"body": {"content": [
                    {"paragraph": {"elements": [
                        {"textRun": {"content": tab["text"]}}
                    ]}}
                ]}},
            }
            for tab in self.tabs
        ]})

    def batchUpdate(self, documentId=None, body=None):
        request = body["requests"][0]["replaceAllText"]
        needle = request["containsText"]["text"]
        replacement = request["replaceText"]
        ids = (request.get("tabsCriteria") or {}).get("tabIds")
        self.calls.append(list(ids) if ids else [])

        changed = 0
        if self.flat:
            if needle in self.flat_text:
                self.flat_text = self.flat_text.replace(needle, replacement)
                changed = 1
        else:
            for tab in self.tabs:
                if ids and tab["id"] not in ids:
                    continue
                if needle in tab["text"]:
                    tab["text"] = tab["text"].replace(needle, replacement)
                    changed += 1
        return _Request({"replies": [{"replaceAllText": {"occurrencesChanged": changed}}]})


@pytest.fixture()
def wired(monkeypatch):
    fake = FakeDocs()
    monkeypatch.setattr(gdocs, "known_sources", lambda conn: [
        {"id": "doc-1", "name": "Master GDD", "mimeType": gdocs.DOC_MIME}
    ])
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())
    monkeypatch.setattr(status_mod, "extract_work_orders", lambda conn: [
        {"wo": 9, "status": "done", "title": "Headless-Tests"}
    ])
    monkeypatch.setattr(status_mod, "status", lambda conn: json.dumps({
        "work_orders": [{"wo": 9, "status": "open", "title": "Headless-Tests"}]
    }))
    return fake


def test_set_status_locks_replacement_to_one_tab(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, wired))

    result = publish.set_status(tmp_path, None, 9, status="open")

    assert result["changed"] is True
    assert result["tab_locked"] is True
    assert result["tab"] == "Tab 5 Kampagne"
    assert result["verified"] is True
    # nur der Ziel-Tab wurde angefasst, das Archiv blieb unberührt
    assert "⬜ OFFEN" in wired.tabs[0]["text"]
    assert "✅ ERLEDIGT" in wired.tabs[1]["text"]
    assert wired.calls == [["t.1"]]


def test_set_status_falls_back_without_tab_lock(monkeypatch, tmp_path):
    fake = FakeDocs(flat=True)
    monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, fake))
    monkeypatch.setattr(gdocs, "known_sources", lambda conn: [
        {"id": "doc-1", "name": "Master GDD", "mimeType": gdocs.DOC_MIME}
    ])
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())
    monkeypatch.setattr(status_mod, "extract_work_orders", lambda conn: [
        {"wo": 9, "status": "done", "title": "Test"}
    ])
    monkeypatch.setattr(status_mod, "status", lambda conn: json.dumps({
        "work_orders": [{"wo": 9, "status": "open", "title": "Test"}]
    }))

    result = publish.set_status(tmp_path, None, 9, status="open")

    assert result["changed"] is True
    assert result["tab_locked"] is False     # Dokument ohne Tabs
    assert result["verified"] is True


def test_set_status_swaps_whole_marker_pair():
    """'✅ ERLEDIGT' -> '⬜ OFFEN': der Text muss mitwechseln, nicht nur das Emoji."""
    assert publish.format_line(9, "open", "", "") == "Work Order 9 — ⬜ OFFEN:"
    assert publish.format_line(9, "done", "", "") == "Work Order 9 — ✅ ERLEDIGT:"


def test_newline_prefix_starts_a_new_paragraph():
    """Regression zu `line + "\n"` am Absatzende.

    Simuliert die Docs-Insert-Semantik: Einfügen an der Position des
    abschließenden Umbruchs. Ohne führenden Umbruch klebt die Work Order am
    letzten Wort ("…AbschnittsWork Order 7 — ✅").
    """
    text = "Text des Abschnitts\nNächster Abschnitt\n"
    index = len("Text des Abschnitts")  # Position des Umbruchs
    line = "Work Order 7 — ✅ ERLEDIGT (Test)"

    fixed = text[:index] + "\n" + line + text[index:]
    assert fixed.splitlines() == ["Text des Abschnitts", line, "Nächster Abschnitt"]

    broken = text[:index] + line + "\n" + text[index:]
    assert "Abschnitts" + line in broken, "der alte Weg klebte die Zeile an"


def test_insert_request_has_leading_newline():
    """Der Request selbst: führender Umbruch, Tab-Sperre, kein Ankleben."""
    body = _insert_request(14237, "Work Order 7 — ✅ ERLEDIGT (Test)")

    request = body["requests"][0]["insertText"]
    assert request["text"].startswith("\n"), "ohne führenden Umbruch klebt die Zeile"
    assert request["text"].endswith("(Test)")
    assert request["location"] == {"index": 14237}


def test_insert_request_carries_tab_id():
    """Bei einem Tab-Dokument muss die Location den Tab nennen."""
    body = _insert_request(500, "Work Order 8 — ⬜ OFFEN (Test)", tab_id="t.5zmrz")

    assert body["requests"][0]["insertText"]["location"]["tabId"] == "t.5zmrz"
