from atlas.publish import _insert_request, find_insert_index, format_line


def test_format_done_with_title_and_description():
    line = format_line(7, "done", "Mission 1 E2E", "Räume 1–9 implementiert.")
    assert line == "Work Order 7 — ✅ DONE (Mission 1 E2E): Räume 1–9 implementiert."


def test_format_open_without_description():
    assert format_line(8, "open", "Kamera") == "Work Order 8 — ⬜ OPEN (Kamera):"


def test_format_rejects_unknown_status():
    try:
        format_line(1, "vielleicht", "X")
    except ValueError as error:
        assert "status" in str(error).lower()
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
    # The index points to the newline that terminates the section — not after it.
    # Text is inserted there with a leading "\n" so the new line becomes its own
    # paragraph and inherits the style of the preceding paragraph (not the
    # following heading, which would turn it into HEADING_2).
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
# set_status: tab locking (tabsCriteria)
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
    """Two tabs with identical status strings — the exact case for tabsCriteria."""

    def __init__(self, flat: bool = False):
        self.flat = flat
        # Flat document without tabs: text must change on write, otherwise the
        # fake would simulate an API that silently swallows writes.
        self.flat_text = "Work Order 9 — ✅ DONE (flach)\n"
        self.tabs = [
            {"id": "t.1", "title": "Tab 5 Kampagne",
             "text": "Work Order 9 — ✅ DONE (Headless-Tests)\n"},
            {"id": "t.2", "title": "Archiv",
             "text": "Work Order 9 — ✅ DONE (alte Fassung)\n"},
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
    # Only the target tab was modified; the archive remained untouched
    assert "⬜ OPEN" in wired.tabs[0]["text"]
    assert "✅ DONE" in wired.tabs[1]["text"]
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
    assert result["tab_locked"] is False     # Document without tabs
    assert result["verified"] is True


def test_set_status_swaps_whole_marker_pair():
    """'✅ DONE' -> '⬜ OPEN': the label text must switch along with the emoji."""
    assert publish.format_line(9, "open", "", "") == "Work Order 9 — ⬜ OPEN:"
    assert publish.format_line(9, "done", "", "") == "Work Order 9 — ✅ DONE:"


def test_newline_prefix_starts_a_new_paragraph():
    """Regression test for `line + "\n"` at the end of a paragraph.

    Simulates Google Docs insert semantics: inserting at the index of the
    trailing newline. Without a leading newline, the Work Order sticks to the
    last word ("…AbschnittsWork Order 7 — ✅").
    """
    text = "Text des Abschnitts\nNächster Abschnitt\n"
    index = len("Text des Abschnitts")  # Position of the newline
    line = "Work Order 7 — ✅ DONE (Test)"

    fixed = text[:index] + "\n" + line + text[index:]
    assert fixed.splitlines() == ["Text des Abschnitts", line, "Nächster Abschnitt"]

    broken = text[:index] + line + "\n" + text[index:]
    assert "Abschnitts" + line in broken, "der alte Weg klebte die Zeile an"


def test_insert_request_has_leading_newline():
    """The request itself: leading newline, tab lock, no text sticking."""
    body = _insert_request(14237, "Work Order 7 — ✅ DONE (Test)")

    request = body["requests"][0]["insertText"]
    assert request["text"].startswith("\n"), "ohne führenden Umbruch klebt die Zeile"
    assert request["text"].endswith("(Test)")
    assert request["location"] == {"index": 14237}


def test_insert_request_carries_tab_id():
    """For a tabbed document, the insert location must specify the tab ID."""
    body = _insert_request(500, "Work Order 8 — ⬜ OPEN (Test)", tab_id="t.5zmrz")

    assert body["requests"][0]["insertText"]["location"]["tabId"] == "t.5zmrz"


def test_publish_live_doc_guard_switches_status_when_index_lags(monkeypatch, tmp_path):
    """Index does not know WO 9 yet (`extract_work_orders == []`), but the live doc has it."""
    fake = FakeDocs()
    fake.tabs[0]["text"] = "Work Order 9 — ⬜ OPEN (Headless-Tests)\n"
    monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, fake))
    monkeypatch.setattr(gdocs, "known_sources", lambda conn: [
        {"id": "doc-1", "name": "Master GDD", "mimeType": gdocs.DOC_MIME}
    ])
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())
    monkeypatch.setattr(publish, "find_insert_index", lambda body, section: (10, section))
    monkeypatch.setattr(status_mod, "extract_work_orders", lambda conn: [])
    monkeypatch.setattr(status_mod, "status", lambda conn: json.dumps({
        "work_orders": [{"wo": 9, "status": "done", "title": "Headless-Tests"}]
    }))

    result = publish.publish(tmp_path, None, 9, status="done", title="Headless-Tests")

    assert result["changed"] is True
    assert result["reused_set_status"] is True
    assert result["verified"] is True
    assert "✅ DONE" in fake.tabs[0]["text"]


def test_invalidate_sync_state_preserves_known_source(tmp_path):
    from atlas.indexer import get_sync_state, invalidate_sync_state, open_index, set_sync_state

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "SPEC.md").write_text("# Spec\n\nText.\n", encoding="utf-8")
    conn = open_index(tmp_path)
    set_sync_state(
        conn, "doc-123", "Master GDD", gdocs.DOC_MIME,
        "2026-11-09T12:00:00Z", "2026-11-09T12:00:05Z", 500, "cache.md",
    )
    invalidate_sync_state(conn, "doc-123")

    sources = gdocs.known_sources(conn)
    assert len(sources) == 1
    assert sources[0]["id"] == "doc-123"
    state = get_sync_state(conn, "doc-123")
    assert state is not None
    assert state["modified"] == ""


def test_recover_sources_from_cache_heals_empty_sync_state(tmp_path):
    from atlas.indexer import open_index

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "SPEC.md").write_text("# Spec\n\nText.\n", encoding="utf-8")
    conn = open_index(tmp_path)
    cache_dir = tmp_path / ".atlas" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_doc = cache_dir / "master-gdd.md"
    cached_doc.write_text(
        "---\n"
        "id: master-gdd\n"
        "title: Master GDD\n"
        "source: gdrive\n"
        "kind: doc\n"
        "drive_id: gdoc-777\n"
        "synced_at: 2026-11-09T15:00:00+00:00\n"
        "tags: [atlas, gdrive]\n"
        "---\n\n"
        "# Master GDD\n\nBody text.\n",
        encoding="utf-8",
    )

    assert conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 0
    sources = gdocs.known_sources(conn)
    assert len(sources) == 1
    assert sources[0] == {
        "id": "gdoc-777",
        "name": "Master GDD",
        "mimeType": gdocs.DOC_MIME,
    }
    assert conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 1


def test_hybrid_set_status_updates_local_when_gdocs_bound(monkeypatch, tmp_path):
    """When a Work Order exists in local docs/ARCHITECTURE.md and Google Docs is bound,
    set_status updates the local file and attempts to mirror to Google Docs."""
    from atlas.indexer import open_index

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    arch = docs_dir / "ARCHITECTURE.md"
    arch.write_text(
        "# Architecture\n\n## 3. Work Orders\n\n"
        "Work Order 4 — ⬜ OPEN (Cross-Agent Sync): Local work order.\n",
        encoding="utf-8",
    )
    conn = open_index(tmp_path)

    fake = FakeDocs()
    monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, fake))
    monkeypatch.setattr(gdocs, "known_sources", lambda conn: [
        {"id": "doc-1", "name": "Master GDD", "mimeType": gdocs.DOC_MIME}
    ])
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())

    result = publish.set_status(tmp_path, conn, 4, status="done")
    assert result["verified"] is True
    assert "✅ DONE" in arch.read_text(encoding="utf-8")


def test_publish_default_section_falls_back_to_prototyp_meilenstein_plan():
    """When `section` is the default ('Work Orders'), `publish` must automatically
    find '3. Prototyp-Meilenstein-Plan' in German Google Docs."""
    body = _doc()["body"]
    index = None
    heading = None
    for sec in publish.FALLBACK_SECTIONS:
        index, heading = find_insert_index(body, sec)
        if index is not None:
            break
    assert index == 149
    assert "Prototyp-Meilenstein-Plan" in heading


def test_set_status_task_mirrors_to_gdocs_and_wo_close_flips_gdoc_steps(monkeypatch, tmp_path):
    """By default Google Docs stays read-only when `docs/ARCHITECTURE.md` exists;
    explicit `sync_gdocs=True` (`--gdocs`) mirrors task checkoffs and WO status to Google Docs."""
    from atlas.indexer import open_index

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    arch = docs_dir / "ARCHITECTURE.md"
    arch.write_text(
        "# Architecture\n\n## 3. Work Orders\n\n"
        "Work Order 34 — ⬜ OPEN (Save Fix):\n"
        "- [x] Step alpha already checked locally\n",
        encoding="utf-8",
    )
    conn = open_index(tmp_path)

    fake = FakeDocs()
    fake.tabs[0]["text"] = (
        "Work Order 34 — ⬜ OPEN (Save Fix):\n"
        "- [ ] Step alpha already checked locally\n"
    )
    monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, fake))
    monkeypatch.setattr(gdocs, "known_sources", lambda conn: [
        {"id": "doc-1", "name": "Master GDD", "mimeType": gdocs.DOC_MIME}
    ])
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())

    # 0. Default (`sync_gdocs=False`) keeps Google Docs 100% untouched!
    res_default = publish.set_status(tmp_path, conn, status="done", task="Step alpha")
    assert "gdocs_task_synced" not in res_default
    assert "- [ ] Step alpha already checked locally" in fake.tabs[0]["text"]

    # 1. Opt-in `sync_gdocs=True`: task is `[x]` locally, `[ ]` in Google Docs -> mirrors `[x]` to Google Docs
    res_task = publish.set_status(
        tmp_path, conn, status="done", task="Step alpha", sync_gdocs=True
    )
    assert res_task["gdocs_task_synced"] is True
    assert "- [x] Step alpha already checked locally" in fake.tabs[0]["text"]

    # 2. Reset Google Doc step to `[ ]` to simulate lag, then close WO 34 with `sync_gdocs=True`
    fake.tabs[0]["text"] = (
        "Work Order 34 — ⬜ OPEN (Save Fix):\n"
        "- [ ] Step alpha already checked locally\n"
    )
    res_wo = publish.set_status(
        tmp_path, conn, wo=34, status="done", sync_gdocs=True
    )
    assert res_wo["verified"] is True
    assert "Work Order 34 — ✅ DONE" in fake.tabs[0]["text"]
    assert "- [x] Step alpha already checked locally" in fake.tabs[0]["text"]
    assert "Work Order 34 — ✅ DONE" in arch.read_text(encoding="utf-8")


def test_local_first_default_never_calls_gdocs_and_promotes_legacy_gdd_work_orders(
    monkeypatch, tmp_path
):
    """When `docs/ARCHITECTURE.md` exists, `plan`, `publish`, and `set_status` must never
    call Google Docs APIs by default, and legacy GDD-only Work Orders in `.atlas/cache/`
    must automatically promote to `docs/ARCHITECTURE.md` on task/WO status updates."""
    from atlas.indexer import open_index, refresh

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    arch = docs_dir / "ARCHITECTURE.md"
    arch.write_text(
        "---\nid: architecture\n---\n\n# Architecture\n\n## 3. Work Orders\n\n"
        "Work Order 1 — ✅ DONE (Core Unit): Initial setup.\n",
        encoding="utf-8",
    )
    cache_dir = tmp_path / ".atlas" / "cache"
    cache_dir.mkdir(parents=True)
    gdd_cache = cache_dir / "master-gdd.md"
    gdd_cache.write_text(
        "---\nid: master-gdd\nsource: gdrive\nkind: doc\ndrive_id: doc-ro-1\n---\n\n"
        "# Prototyp-Meilenstein-Plan\n\n"
        "Work Order 34 — ⬜ OPEN (Imprisoned Heroes Save Fix):\n"
        "- [ ] Persist imprisoned_heroes in save payload\n"
        "- [ ] Add save/load roundtrip test\n",
        encoding="utf-8",
    )

    conn = open_index(tmp_path)
    refresh(conn, tmp_path, force=True)

    monkeypatch.setattr(gdocs, "known_sources", lambda c: [
        {"id": "doc-ro-1", "name": "Master GDD", "mimeType": gdocs.DOC_MIME}
    ])
    monkeypatch.setattr(
        gdocs,
        "load_credentials",
        lambda **kw: pytest.fail("Google Docs credentials/API must never be called in local-first mode"),
    )

    # 1. Toggling step 1 of legacy GDD-only WO 34 promotes WO 34 into ARCHITECTURE.md locally
    step1 = publish.set_status(
        tmp_path, conn, task="Persist imprisoned_heroes", status="done"
    )
    assert step1["verified"] is True
    assert step1["promoted_from_gdd"] is True
    assert step1["wo"] == 34
    assert "Work Order 34 — ⬜ OPEN (Imprisoned Heroes Save Fix)" in arch.read_text(encoding="utf-8")
    assert "- [x] Persist imprisoned_heroes in save payload" in arch.read_text(encoding="utf-8")

    # 2. Closing WO 34 while step 2 is still open is refused by the local step gate
    refused = publish.set_status(tmp_path, conn, wo=34, status="done")
    assert refused["refused"] is True
    assert refused["open_steps"] == ["Add save/load roundtrip test"]

    # 3. Checking step 2 and closing WO 34 succeeds purely locally despite stale `.atlas/cache/master-gdd.md`
    step2 = publish.set_status(
        tmp_path, conn, wo=34, task="Add save/load roundtrip test", status="done"
    )
    assert step2["verified"] is True
    closed = publish.set_status(tmp_path, conn, wo=34, status="done")
    assert closed["verified"] is True
    assert "Work Order 34 — ✅ DONE" in arch.read_text(encoding="utf-8")
    # Read-only GDD cache was never mutated directly
    assert "Work Order 34 — ⬜ OPEN" in gdd_cache.read_text(encoding="utf-8")

    # 4. Planning and publishing new Work Orders also stay 100% local (zero Google Docs calls)
    planned = publish.plan(tmp_path, conn, "Camp Dialogue Triggers", steps=["Wire camp signal"])
    assert planned["wo"] == 35
    assert planned["verified"] is True
    assert "gdocs_synced" not in planned

    published = publish.publish(tmp_path, conn, wo=36, status="open", title="Post-Release Polish")
    assert published["verified"] is True
    assert published["source"] == "local"


def test_plan_deduplicates_matching_wo_and_respects_audit_numbers(tmp_path):
    """`plan` reuses an existing Work Order when called again with the same topic/steps
    and skips Work Order numbers already recorded in `wo-symbols.yml` / `mutations.log`."""
    from atlas import verify
    from atlas.indexer import open_index

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    arch = docs_dir / "ARCHITECTURE.md"
    arch.write_text(
        "# Architecture\n\n## 3. Work Orders\n\n"
        "Work Order 1 — ✅ DONE (Core Unit): Done.\n",
        encoding="utf-8",
    )
    conn = open_index(tmp_path)

    # Record WO 25 in wo-symbols.yml (unindexed in doc) -> next free WO must be 26, not 2!
    verify.record_wo_symbols(tmp_path, 25, ["trigger_tar_ignition"])

    first = publish.plan(
        tmp_path,
        conn,
        "Hero Imprisonment & Recruitment Flow",
        steps=["Persist imprisoned heroes", "Add recruitment dialogue"],
    )
    assert first["wo"] == 26
    assert first["written"] is True

    # Calling plan again with the same topic and overlapping steps deduplicates to WO 26
    second = publish.plan(
        tmp_path,
        conn,
        "Hero Imprisonment & Recruitment Flow",
        steps=["Persist imprisoned heroes", "Add recruitment dialogue"],
    )
    assert second["deduplicated"] is True
    assert second["wo"] == 26
    assert second["written"] is False




