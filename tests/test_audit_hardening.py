from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from atlas import (
    audit,
    brief,
    code,
    gdocs,
    health,
    indexer,
    lint,
    localwrite,
    normalize,
    publish,
    search,
    status as status_mod,
    verify,
)


# ---------------------------------------------------------------------------
# Domain 1: Hybrid Sync, Google Docs Consistency & Work Order Reconciliation
# ---------------------------------------------------------------------------


def test_invalidate_sync_state_clears_cooldown_for_immediate_auto_sync(tmp_path: Path, monkeypatch):
    conn = indexer.open_index(tmp_path)
    try:
        indexer.set_sync_state(
            conn,
            "doc-123",
            "Master GDD",
            gdocs.DOC_MIME,
            "2026-11-11T10:00:00Z",
            "now",
            120,
            ".atlas/cache/gdd.md",
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync_attempt', ?)",
            (str(time.time()),),
        )
        conn.commit()

        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")
        sync_calls: list[str] = []
        monkeypatch.setattr(gdocs, "TOKEN_PATH", fake_token)

        def fake_sync(root, c=None, **kwargs):
            sync_calls.append("called")
            return {"synced": 1, "skipped": 0, "written": []}

        monkeypatch.setattr(gdocs, "sync", fake_sync)

        # While cooldown is fresh, auto_sync is suppressed.
        assert gdocs.auto_sync(tmp_path, conn) is None
        assert sync_calls == []

        # After a write reports index_lag and invalidates sync state, cooldown must be cleared too.
        indexer.invalidate_sync_state(conn, "doc-123")
        row = conn.execute("SELECT modified FROM sync_state WHERE drive_id = 'doc-123'").fetchone()
        meta_row = conn.execute("SELECT value FROM meta WHERE key = 'last_sync_attempt'").fetchone()
        assert row["modified"] == ""
        assert meta_row is None

        res = gdocs.auto_sync(tmp_path, conn)
        assert res is not None
        assert sync_calls == ["called"]
    finally:
        conn.close()


def test_rescue_cached_work_orders_preserves_steps_and_blocks_stale_downgrade(tmp_path: Path):
    cached = (
        "# 3. Work Orders\n\n"
        "Work Order 1 — ✅ DONE (Combat Core): Implemented.\n"
        "- [x] Implement armor formula\n"
        "- [x] Add unit tests\n\n"
        "Work Order 2 — 🚧 IN PROGRESS (Turn Order): In flight.\n"
        "- [x] Sort initiative queue\n"
        "- [ ] Add tie-breaker UI\n\n"
        "Work Order 3 — ⬜ OPEN (Loot Table): Planned.\n"
        "- [ ] Roll rarity weights\n"
    )
    cache_file = tmp_path / ".atlas" / "cache" / "gdd.md"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(cached, encoding="utf-8")

    # Simulate a lagging Google Drive export:
    # - WO 1 is still exported as OPEN with unchecked steps
    # - WO 2 has its first step still unchecked in Drive
    # - WO 3 has not propagated to Drive export at all yet
    stale_export = (
        "# 3. Work Orders\n\n"
        "Work Order 1 — ⬜ OPEN (Combat Core): Implemented.\n"
        "- [ ] Implement armor formula\n"
        "- [ ] Add unit tests\n\n"
        "Work Order 2 — ⬜ OPEN (Turn Order): In flight.\n"
        "- [ ] Sort initiative queue\n"
        "- [ ] Add tie-breaker UI\n"
    )
    audit.log(tmp_path, "set_status", wo=1, status="done", verified=True)
    audit.log(tmp_path, "set_task_status", wo=2, task="Sort initiative queue", status="done", verified=True)

    merged = gdocs._rescue_cached_work_orders(tmp_path, "gdd.md", stale_export)

    # WO 1 stays DONE and keeps checked steps despite stale export
    assert "Work Order 1 — ✅ DONE" in merged
    assert "- [x] Implement armor formula" in merged
    # WO 2 keeps checked step 1 and unchecked step 2
    assert "- [x] Sort initiative queue" in merged
    assert "- [ ] Add tie-breaker UI" in merged
    # WO 3 is rescued together with its checklist step
    assert "Work Order 3 — ⬜ OPEN (Loot Table): Planned." in merged
    assert "- [ ] Roll rarity weights" in merged

    # Explicit reopen in mutations.log IS honored and not blocked as a stale downgrade
    audit.log(tmp_path, "set_status", wo=1, status="open", verified=True)
    reopened_export = (
        "# 3. Work Orders\n\n"
        "Work Order 1 — ⬜ OPEN (Combat Core): Reopened.\n"
        "- [ ] Implement armor formula\n"
    )
    merged_reopen = gdocs._rescue_cached_work_orders(tmp_path, "gdd.md", reopened_export)
    assert "Work Order 1 — ⬜ OPEN (Combat Core): Reopened." in merged_reopen


class _Req:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FragmentedDocsService:
    """Simulates Google Docs API with UTF-16 indices, split textRuns, and an Archive tab."""

    def __init__(self):
        self.revision = "rev-42"
        self.tabs = {
            "t.active": {
                "title": "Active Spec",
                "runs": [
                    "Intro 🚀\n",
                    "Work Order 9 — ",
                    "🚧 IN PROGRESS",
                    " (Fragmented Header)\n",
                    "- [ ] Shared step\n",
                    "Work Order 10 — ",
                    "⬜ OPEN",
                    " (Second WO)\n",
                    "- [ ] Shared step\n",
                ],
            },
            "t.archive": {
                "title": "Archive",
                "runs": [
                    "Work Order 9 — 🚧 IN PROGRESS (Old Archive Copy)\n",
                    "- [ ] Shared step\n",
                ],
            },
        }
        self.requests_seen: list[dict] = []

    @staticmethod
    def _utf16_len(s: str) -> int:
        return len(s.encode("utf-16-le")) // 2

    @staticmethod
    def _slice_utf16(text: str, start_u16: int, end_u16: int) -> tuple[str, str]:
        raw = text.encode("utf-16-le")
        b_start = start_u16 * 2
        b_end = end_u16 * 2
        return raw[:b_start].decode("utf-16-le"), raw[b_end:].decode("utf-16-le")

    def _build_body(self, runs: list[str]) -> dict:
        elements = []
        cursor = 1
        content = []
        # Group runs into paragraphs at '\n' so Google Docs structure is realistic.
        para_elems = []
        para_start = cursor
        for r in runs:
            u_len = self._utf16_len(r)
            para_elems.append({
                "startIndex": cursor,
                "endIndex": cursor + u_len,
                "textRun": {"content": r},
            })
            cursor += u_len
            if r.endswith("\n"):
                content.append({
                    "startIndex": para_start,
                    "endIndex": cursor,
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        "elements": para_elems,
                    },
                })
                para_elems = []
                para_start = cursor
        if para_elems:
            content.append({
                "startIndex": para_start,
                "endIndex": cursor,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": para_elems,
                },
            })
        return {"content": content}

    def documents(self):
        return self

    def get(self, documentId=None, includeTabsContent=None):
        return _Req({
            "revisionId": self.revision,
            "tabs": [
                {
                    "tabProperties": {"tabId": tid, "title": tdata["title"]},
                    "documentTab": {"body": self._build_body(tdata["runs"])},
                }
                for tid, tdata in self.tabs.items()
            ],
        })

    def batchUpdate(self, documentId=None, body=None):
        self.requests_seen.append(body)
        reqs = body.get("requests", [])
        if "replaceAllText" in reqs[0]:
            # Simulate Google Docs replaceAllText failing across formatted run boundaries
            # when needle spans multiple runs in t.active, while refusing to touch t.archive
            # if tabsCriteria is properly locked to t.active!
            rat = reqs[0]["replaceAllText"]
            needle = rat["containsText"]["text"]
            repl = rat["replaceText"]
            tab_ids = (rat.get("tabsCriteria") or {}).get("tabIds")
            changed = 0
            for tid, tdata in self.tabs.items():
                if tab_ids and tid not in tab_ids:
                    continue
                for idx, run in enumerate(tdata["runs"]):
                    if needle in run:
                        tdata["runs"][idx] = run.replace(needle, repl)
                        changed += 1
            return _Req({"replies": [{"replaceAllText": {"occurrencesChanged": changed}}]})

        # Range deleteContentRange + insertText execution using exact UTF-16 offsets
        write_ctrl = body.get("writeControl") or {}
        assert write_ctrl.get("requiredRevisionId") == self.revision
        for req in reqs:
            if "deleteContentRange" in req:
                rng = req["deleteContentRange"]["range"]
                tid = rng["tabId"]
                full = "".join(self.tabs[tid]["runs"])
                before, after = self._slice_utf16(full, rng["startIndex"] - 1, rng["endIndex"] - 1)
                self.tabs[tid]["runs"] = [before + after]
            elif "insertText" in req:
                ins = req["insertText"]
                loc = ins["location"]
                tid = loc["tabId"]
                full = "".join(self.tabs[tid]["runs"])
                before, after = self._slice_utf16(full, loc["index"] - 1, loc["index"] - 1)
                self.tabs[tid]["runs"] = [before + ins["text"] + after]
        return _Req({"replies": [{} for _ in reqs]})


def test_gdocs_fragmented_text_runs_surrogate_offsets_and_archive_tab_isolation(
    tmp_path: Path, monkeypatch
):
    fake_docs = _FragmentedDocsService()
    monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, fake_docs))
    monkeypatch.setattr(gdocs, "known_sources", lambda conn: [
        {"id": "doc-frag", "name": "Spec", "mimeType": gdocs.DOC_MIME}
    ])
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())
    monkeypatch.setattr(status_mod, "extract_work_orders", lambda conn: [
        {"wo": 9, "status": "open", "title": "Fragmented Header"},
        {"wo": 10, "status": "open", "title": "Second WO"},
    ])
    monkeypatch.setattr(status_mod, "status", lambda conn: json.dumps({
        "work_orders": [{"wo": 9, "status": "done", "title": "Fragmented Header"}]
    }))

    res = publish.set_status(tmp_path, None, 9, status="done")
    assert res["changed"] is True
    assert res["verified"] is True
    assert res["tab_locked"] is True

    active_text = "".join(fake_docs.tabs["t.active"]["runs"])
    archive_text = "".join(fake_docs.tabs["t.archive"]["runs"])
    assert "Work Order 9 — ✅ DONE (Fragmented Header)" in active_text
    # Emoji 🚀 before WO 9 must remain intact (proves UTF-16 surrogate-pair offset math!)
    assert "Intro 🚀\nWork Order 9 — ✅ DONE" in active_text
    # Archive tab was NEVER mutated!
    assert "Work Order 9 — 🚧 IN PROGRESS (Old Archive Copy)" in archive_text


def test_duplicate_step_text_across_work_orders_local_and_gdocs_wo_scoped(
    tmp_path: Path, monkeypatch
):
    arch = tmp_path / "docs" / "ARCHITECTURE.md"
    arch.parent.mkdir(parents=True)
    arch.write_text(
        "---\nid: architecture\n---\n\n# 3. Work Orders\n\n"
        "Work Order 1 — ✅ DONE (Core):\n"
        "- [x] Add unit tests\n\n"
        "Work Order 2 — ⬜ OPEN (Audio):\n"
        "- [ ] Add unit tests\n\n"
        "Work Order 3 — ⬜ OPEN (UI):\n"
        "- [ ] Add unit tests\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        # 1. Explicit wo=3 targets WO 3's duplicate step, leaving WO 2 unchecked!
        fake_docs = _FragmentedDocsService()
        monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, fake_docs))
        monkeypatch.setattr(gdocs, "known_sources", lambda c: [
            {"id": "doc-frag", "name": "Spec", "mimeType": gdocs.DOC_MIME}
        ])
        monkeypatch.setattr(gdocs, "assert_writable", lambda c, file_id: None)
        monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
        monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())

        res_wo3 = publish.set_status(tmp_path, conn, 3, status="done", task="Add unit tests")
        assert res_wo3["changed"] is True
        assert res_wo3["verified"] is True

        orders = {w["wo"]: w for w in status_mod.extract_work_orders(conn)}
        assert orders[1]["steps"][0]["done"] is True
        assert orders[2]["steps"][0]["done"] is False
        assert orders[3]["steps"][0]["done"] is True

        # Verify WO-scoped Google Docs mirror also works on WO 10 vs WO 9's identical "- [ ] Shared step"
        mirror = publish._mirror_task_to_gdocs(tmp_path, conn, "Shared step", "done", wo=10)
        assert mirror.get("gdocs_task_synced") is True
        assert mirror.get("gdocs_changed") == 1
        active_text = "".join(fake_docs.tabs["t.active"]["runs"])
        assert "Work Order 9 — 🚧 IN PROGRESS (Fragmented Header)\n- [ ] Shared step" in active_text
        assert "Work Order 10 — ⬜ OPEN (Second WO)\n- [x] Shared step" in active_text

        # 2. Unscoped task="Add unit tests" skips already-done WO 1 and checks open WO 2!
        monkeypatch.setattr(gdocs, "known_sources", lambda c: [])
        res_unscoped = publish.set_status(tmp_path, conn, None, status="done", task="Add unit tests")
        assert res_unscoped["changed"] is True
        orders_after = {w["wo"]: w for w in status_mod.extract_work_orders(conn)}
        assert orders_after[2]["steps"][0]["done"] is True
    finally:
        conn.close()


def test_multipart_chunk_split_preserves_all_work_order_steps(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    filler = "\n\n".join(
        f"Work Order {i} — ✅ DONE (Feature {i}): " + ("Detailed architecture notes for subsystem. " * 4)
        for i in range(1, 9)
    )
    straddling_wo = (
        "\n\nWork Order 9 — 🚧 IN PROGRESS (Boundary Straddler): Crossing the 1500-char chunk split.\n"
        + "\n".join(f"- [{'x' if s == 1 else ' '}] Boundary step {s} with implementation detail" for s in range(1, 7))
    )
    (docs / "ARCHITECTURE.md").write_text(
        f"---\nid: architecture\n---\n\n# 3. Work Orders\n\n{filler}{straddling_wo}\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        parts = conn.execute(
            "SELECT anchor, part, parts FROM sections WHERE doc_id = 'architecture' ORDER BY part"
        ).fetchall()
        assert len(parts) >= 2, "Fixture must split #3-work-orders across multiple chunks"
        orders = {w["wo"]: w for w in status_mod.extract_work_orders(conn)}
        assert 9 in orders
        assert orders[9]["steps_total"] == 6
        assert orders[9]["steps_done"] == 1
    finally:
        conn.close()


def test_status_word_boundaries_parenthetical_titles_and_step_wo_mentions(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ARCHITECTURE.md").write_text(
        "---\nid: architecture\n---\n\n# 3. Work Orders\n\n"
        "Work Order 5 — ✅ DONE (Open World & OpenGL Rohstoffen): Balanced drops (see §2).\n"
        "- [x] Fix Work Order 2: edge case in loot table\n"
        "- [x] Verify OpenGL shader cache\n\n"
        "Work Order 6 — ✅ DONE: Unparenthesized title with note (see §4)\n\n"
        "Work Order 7 — 🚧 IN PROGRESS (HUD): Working.\n\n"
        "Work Order 8 — ABGESCHLOSSEN (Localization): Fertig.\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        orders = {w["wo"]: w for w in status_mod.extract_work_orders(conn)}
        assert orders[5]["status"] == "done"
        assert orders[5]["title"] == "Open World & OpenGL Rohstoffen"
        assert orders[5]["steps_total"] == 2
        assert 2 not in orders  # Mention inside "- [x] Fix Work Order 2: ..." must NOT spawn WO 2!
        assert orders[6]["status"] == "done"
        assert orders[6]["title"] == "Unparenthesized title with note (see §4)"
        assert orders[7]["status"] == "open"
        assert orders[8]["status"] == "done"

        lint_report = json.loads(lint.lint(conn))
        wo_issues = [i for i in lint_report["issues"] if i["type"] == "work_order_format"]
        assert wo_issues == []
    finally:
        conn.close()


def test_reconcile_syncs_checked_steps_handles_german_offen_and_batches_git_log(
    tmp_path: Path, monkeypatch
):
    arch = tmp_path / "docs" / "ARCHITECTURE.md"
    arch.parent.mkdir(parents=True)
    arch.write_text(
        "---\nid: architecture\n---\n\n# 3. Work Orders\n\n"
        "Work Order 1 — ⬜ OPEN (Sync Steps):\n"
        "- [ ] Step Alpha\n"
        "- [ ] Step Beta\n",
        encoding="utf-8",
    )
    # Audit log has German 'offen' on WO 2..6 without titles -> triggers git log recovery
    for wo_num in range(2, 7):
        audit.log(tmp_path, "publish", wo=wo_num, status="offen", verified=True)

    git_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        git_calls.append(list(cmd))

        class _Proc:
            returncode = 0
            stdout = "abc1234 feat: implement Work Order 2 inventory grid\n"

        return _Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    conn = indexer.open_index(tmp_path)
    try:
        unindexed = status_mod._recover_unindexed_work_orders(tmp_path, {1})
        by_wo = {u["wo"]: u for u in unindexed}
        assert by_wo[2]["status"] == "open"
        assert len(git_calls) == 1, "git log must be batched to a single subprocess call"

        # Now simulate remote/index having Step Alpha checked off and reconcile into local ARCHITECTURE.md
        monkeypatch.setattr(gdocs, "known_sources", lambda c: [])
        monkeypatch.setattr(
            status_mod,
            "extract_work_orders",
            lambda c: [
                {
                    "wo": 1,
                    "status": "open",
                    "title": "Sync Steps",
                    "line": "Work Order 1 — ⬜ OPEN (Sync Steps):",
                    "path": ".atlas/cache/gdd.md",
                    "steps": [
                        {"text": "Step Alpha", "done": True},
                        {"text": "Step Beta", "done": False},
                    ],
                    "steps_done": 1,
                    "steps_total": 2,
                }
            ],
        )
        rec = status_mod.reconcile_work_orders(tmp_path, conn)
        assert 1 in rec["synced_to_local"]
        updated_md = arch.read_text(encoding="utf-8")
        assert "- [x] Step Alpha" in updated_md
        assert "- [ ] Step Beta" in updated_md
    finally:
        conn.close()


def test_find_duplicate_wo_umlaut_digraph_folding_and_disjoint_steps(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ARCHITECTURE.md").write_text(
        "---\nid: architecture\n---\n\n# 3. Work Orders\n\n"
        "Work Order 1 — ⬜ OPEN (Rüstungs-Mitigation):\n"
        "- [ ] Compute armor curve in combat.py\n"
        "- [ ] Add unit tests\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        existing = status_mod.extract_work_orders(conn)
        # ASCII digraph spelling "Ruestungs Mitigation" must match existing "Rüstungs-Mitigation"
        dup = publish._find_duplicate_wo(existing, "Ruestungs Mitigation")
        assert dup is not None and dup["wo"] == 1

        # Different topic sharing ONLY generic boilerplate "Add unit tests" must NOT false-match WO 1!
        non_dup = publish._find_duplicate_wo(
            existing,
            "Dialogue Camera Framing",
            steps=["Interpolate camera zoom in dialogue.py", "Add unit tests"],
        )
        assert non_dup is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Domain 2: Multi-Language Static Code Scanner & Wiring Gate
# ---------------------------------------------------------------------------


def test_code_references_ignores_docstrings_and_comments_including_rust_lifetimes(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "engine.py").write_text(
        "def compute_mitigation(raw: float) -> float:\n"
        '    """Example:\n'
        "    >>> compute_mitigation(10.0)\n"
        '    """\n'
        "    return raw * 0.5\n\n"
        "# compute_mitigation(99.0) should not count in comments\n",
        encoding="utf-8",
    )
    (src / "combat.ts").write_text(
        "/*\n"
        " * Legacy call: compute_mitigation(25)\n"
        " */\n"
        "// compute_mitigation(5)\n"
        "export const x = 1;\n",
        encoding="utf-8",
    )
    (src / "lifetime.rs").write_text(
        "pub struct View<'a> {\n"
        "    pub name: &'a str,\n"
        "}\n"
        "// compute_mitigation(42) after Rust 'a lifetime must still be stripped!\n"
        "pub fn label<'a>(v: &'a View<'a>) -> &'a str { v.name }\n",
        encoding="utf-8",
    )
    refs = code.references(tmp_path, "compute_mitigation")
    assert refs["call_count"] == 0
    assert refs["ref_count"] == 0
    gate = verify.check(tmp_path, ["compute_mitigation"])
    assert gate["ok"] is False

    # Dynamic string dispatch in GDScript (`call("compute_mitigation")`) MUST still count as a reference!
    (src / "unit.gd").write_text(
        "extends Node\nfunc _ready() -> void:\n\tcall(\"compute_mitigation\", 12.0)\n",
        encoding="utf-8",
    )
    gate_wired = verify.check(tmp_path, ["compute_mitigation"])
    assert gate_wired["ok"] is True
    assert gate_wired["symbols"][0]["refs"] >= 1


def test_code_find_ts_class_methods_multiline_arrows_and_kotlin_trailing_lambdas(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "turn_controller.ts").write_text(
        "export class TurnController {\n"
        "  private async computePath(\n"
        "    startX: number,\n"
        "    startY: number,\n"
        "  ): Promise<number[]> {\n"
        "    return [startX, startY];\n"
        "  }\n\n"
        "  resolveTurn(actorId: string): void {\n"
        "    return;\n"
        "  }\n\n"
        "  resetState() {\n"
        "    return;\n"
        "  }\n"
        "}\n\n"
        "export const buildGrid = (\n"
        "  width: number,\n"
        "  height: number,\n"
        ") => {\n"
        "  return width * height;\n"
        "};\n",
        encoding="utf-8",
    )
    (src / "runner.kt").write_text(
        "fun withLock(lock: Any, block: () -> Unit) {\n"
        "    block()\n"
        "}\n\n"
        "fun executeTurn(mutex: Any) {\n"
        "    withLock(mutex) {\n"
        "        println(\"locked\")\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    for sym in ("computePath", "resolveTurn", "resetState", "buildGrid"):
        found = code.find(tmp_path, sym)
        assert any(h["symbol"] == sym for h in found), f"Missing TS definition for {sym}"
        # Uncalled TS definitions must NOT count their own multiline header as a caller!
        usage = code.references(tmp_path, sym)
        assert usage["call_count"] == 0, f"{sym} self-counted as caller: {usage}"

    # Kotlin trailing-lambda invocation `withLock(mutex) {` MUST count as a real call!
    kt_refs = code.references(tmp_path, "withLock")
    assert kt_refs["call_count"] == 1


def test_code_find_swift_protocol_gdscript4_constructs_and_tscn_method_tracks(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Damageable.swift").write_text(
        "public protocol Damageable {\n"
        "    func applyHit(amount: Int)\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "actor.gd").write_text(
        "extends Node2D\n"
        "signal turn_ended(actor_id: String)\n"
        "@onready var health_bar: Control = $HealthBar\n"
        "var on_crit := func(mult: float):\n"
        "\treturn mult * 2.0\n\n"
        "func _spawn_vfx() -> void:\n"
        "\tpass\n",
        encoding="utf-8",
    )
    (src / "battle.tscn").write_text(
        '[gd_scene format=3]\n'
        '[sub_resource type="Animation" id="Animation_1"]\n'
        'tracks/0/type = "method"\n'
        'tracks/0/keys = {\n'
        '"times": PackedFloat32Array(0.2),\n'
        '"values": [{\n'
        '"args": [],\n'
        '"method": &"_spawn_vfx"\n'
        '}]\n'
        '}\n',
        encoding="utf-8",
    )

    assert any(h["symbol"] == "Damageable" for h in code.find(tmp_path, "Damageable"))
    assert any(h["symbol"] == "turn_ended" for h in code.find(tmp_path, "turn_ended"))
    assert any(h["symbol"] == "health_bar" for h in code.find(tmp_path, "health_bar"))
    assert any(h["symbol"] == "on_crit" for h in code.find(tmp_path, "on_crit"))

    vfx_refs = code.references(tmp_path, "_spawn_vfx")
    assert vfx_refs["ref_count"] == 1
    assert vfx_refs["refs"][0]["path"].endswith("battle.tscn")


def test_code_multiline_signatures_decorators_rust_where_and_docstring_body_end(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "service.py").write_text(
        "@register_handler(\n"
        '    route="/damage",\n'
        "    version=2,\n"
        ")\n"
        "def calculate_total(\n"
        "    base: int,\n"
        "    bonus: int,\n"
        ") -> int:\n"
        '    """Compute total.\n'
        "Unindented line inside multiline docstring must NOT end body early!\n"
        '    """\n'
        "    total = base + bonus\n"
        "    return total\n",
        encoding="utf-8",
    )
    (src / "storage.rs").write_text(
        "pub struct Repo;\n\n"
        "impl Repo {\n"
        "    pub fn load_items<T>(\n"
        "        &self,\n"
        "        key: &str,\n"
        "    ) -> Result<Vec<T>, String>\n"
        "    where\n"
        "        T: Clone + Default,\n"
        "    {\n"
        "        Ok(Vec::new())\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    py_hits = code.find(tmp_path, "calculate_total", include_body=True)
    assert len(py_hits) == 1
    assert "bonus: int" in py_hits[0]["signature"]
    assert "return total" in py_hits[0]["body"]
    assert "@register_handler(" in py_hits[0]["body"]

    rs_hits = code.find(tmp_path, "load_items", include_body=True)
    assert len(rs_hits) == 1
    assert "Ok(Vec::new())" in rs_hits[0]["body"]


# ---------------------------------------------------------------------------
# Domain 3: Bilingual Search, Orthography, Stopwords & Scale Budgets
# ---------------------------------------------------------------------------


def test_fts5_german_eszett_umlaut_digraphs_and_fugenlaut_compounds(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "combat.md").write_text(
        "---\nid: combat\n---\n\n"
        "# Treffer-Größe und Stoßschaden\n\n"
        "Schwere Waffen verursachen zusätzlichen Stoßschaden abhängig von der Größe.\n\n"
        "# Rüstung und Mitigation\n\n"
        "Jeder Punkt Rüstung reduziert physischen Schaden vor der Mitigation.\n\n"
        "# Krieg und Horn Signal\n\n"
        "Das antike Horn eröffnet den Krieg für alle Einheiten.\n\n"
        "# Turn Order & Tiebreaker\n\n"
        "Bei gleicher Initiative entscheidet der Tiebreaker über die Turn Order.\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        for q, expected_slug in [
            ("Stoßschaden", "combat#treffer-groesse-und-stossschaden"),
            ("Stossschaden", "combat#treffer-groesse-und-stossschaden"),
            ("Groesse", "combat#treffer-groesse-und-stossschaden"),
            ("Rüstungs-Mitigation", "combat#ruestung-und-mitigation"),
            ("Ruestungs-Mitigation", "combat#ruestung-und-mitigation"),
            ("Kriegshorn", "combat#krieg-und-horn-signal"),
            ("TurnOrderTiebreaker", "combat#turn-order-tiebreaker"),
        ]:
            res = json.loads(search.where(conn, q))
            assert res["found"] is True, f"Query {q!r} returned no match: {res}"
            assert res["anchor"] == expected_slug, f"Query {q!r} matched {res['anchor']}"
    finally:
        conn.close()


def test_bilingual_stopword_disambiguation_preserves_english_gdd_terms():
    assert "war" in normalize.terms("Where is fog of war described?")
    assert "die" in normalize.terms("How does die roll probability work?")
    # Pure German queries still drop German article/auxiliary stopwords
    de_tokens = normalize.terms("Wo sind die Trefferpunkte und was war geplant?")
    assert "die" not in de_tokens
    assert "war" not in de_tokens
    assert "trefferpunkte" in de_tokens


def test_verify_checks_beyond_30_work_orders_queue_budget_and_large_audit_tail(
    tmp_path: Path, monkeypatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "core.py").write_text("def dead_symbol_35():\n    return 35\n", encoding="utf-8")

    done_wos = "\n\n".join(
        f"Work Order {i} — ✅ DONE (Completed {i}): Done." for i in range(1, 36)
    )
    open_wos = "\n\n".join(
        f"Work Order {i} — ⬜ OPEN (Queued Feature {i}): Needs work.\n- [ ] Implement part {i}"
        for i in range(36, 81)
    )
    (docs / "ARCHITECTURE.md").write_text(
        f"---\nid: architecture\n---\n\n# 3. Work Orders\n\n{done_wos}\n\n{open_wos}\n",
        encoding="utf-8",
    )
    (docs / "spec.md").write_text(
        "---\nid: spec\nimplements:\n  - src/core.py\n---\n\n# Queued Feature\n\n"
        + ("Detailed combat specification paragraph. " * 25),
        encoding="utf-8",
    )
    verify.record_wo_symbols(tmp_path, 35, ["dead_symbol_35"])
    verify.record_wo_symbols(tmp_path, 36, ["dead_symbol_35"])

    conn = indexer.open_index(tmp_path)
    try:
        # 1. WO 35 (> 30!) must NOT be truncated by status(limit=30) in verify.check_project
        report = verify.check_project(tmp_path, conn)
        assert report["ok"] is False
        assert any(r["wo"] == 35 and r["symbol"] == "dead_symbol_35" for r in report["regressions"])

        # 2. build_queue with 45 open WOs enforces max_chars budget and calls symbol_guards once
        guard_calls = 0
        orig_guards = verify.symbol_guards

        def counted_guards(*args, **kwargs):
            nonlocal guard_calls
            guard_calls += 1
            return orig_guards(*args, **kwargs)

        monkeypatch.setattr(verify, "symbol_guards", counted_guards)
        queue_md = brief.build_queue(conn, tmp_path, max_chars=4200)
        assert len(queue_md) <= 4200
        assert guard_calls == 1
        assert "Work Order 36" in queue_md

        # Also check brief.build calls symbol_guards at most once
        guard_calls = 0
        _ = brief.build(conn, tmp_path, goal="Queued Feature", work_order=36)
        assert guard_calls == 1
    finally:
        conn.close()

    # 3. Large >120 KB mutations.log reverse-seek tail
    log_path = tmp_path / ".atlas" / "mutations.log"
    lines = [
        json.dumps({"ts": f"2026-11-11T10:00:{i % 60:02d}Z", "tool": "publish", "wo": i, "pad": "x" * 120})
        for i in range(1, 950)
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert log_path.stat().st_size > 120_000
    last_five = audit.tail(tmp_path, limit=5)
    assert [e["wo"] for e in last_five] == [945, 946, 947, 948, 949]


# ---------------------------------------------------------------------------
# Domain 4: MCP Ergonomics, Anchor Suggestions, @owner & Health Bucketing
# ---------------------------------------------------------------------------


def test_ergonomics_anchor_suggestions_owner_at_prefix_lint_markers_and_health_task_keys(
    tmp_path: Path,
):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "combat.md").write_text(
        "---\nid: combat\n---\n\n# Damage Model\n\n- [ ] Tune crit multiplier @Design\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        # 1. Unknown anchor returns suggestions in both read() and links()
        miss_read = json.loads(search.read(conn, "combat#damage-modl"))
        assert "error" in miss_read
        assert "combat#damage-model" in miss_read.get("suggestions", [])

        miss_links = json.loads(search.links(conn, "combat#damage-modl"))
        assert "error" in miss_links
        assert "combat#damage-model" in miss_links.get("suggestions", [])

        # 2. @owner prefix & case-insensitivity in search.tasks
        by_at = json.loads(search.tasks(conn, status="open", owner="@design"))
        assert by_at["count"] == 1
        assert "Tune crit multiplier" in by_at["tasks"][0]["text"]
    finally:
        conn.close()

    # 3. Health warnings bucket distinct set_task_status tasks separately
    audit.log(tmp_path, "set_task_status", wo=8, task="Step One", status="done", verified=False)
    audit.log(tmp_path, "set_task_status", wo=8, task="Step Two", status="done", verified=True)
    warns = health.warnings(tmp_path)
    assert any("Step One" in w for w in warns), f"Step Two success must not mask Step One failure: {warns}"

    # Later verified success on Step One clears Step One's warning
    audit.log(tmp_path, "set_task_status", wo=8, task="Step One", status="done", verified=True)
    assert not any("Step One" in w for w in health.warnings(tmp_path))


def test_def_patterns_linear_time_on_deeply_indented_multiline_calls():
    from atlas.bootstrap import iter_def_patterns

    adversarial = "\n".join(
        [
            "            helper_call("
            + "".join(f"\n                arg_{k}=value_{k}," for k in range(10))
            + "\n            )"
            for _ in range(25)
        ]
    )
    t0 = time.perf_counter()
    for pat in iter_def_patterns(""):
        list(pat.finditer(adversarial))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25, f"DEF_PATTERNS regressed into backtracking ({elapsed:.3f}s)"


# ---------------------------------------------------------------------------
# Work Order 9: Round-2 Skeptical Audit Hardening Regression Suite
# ---------------------------------------------------------------------------


def test_rescue_cached_work_orders_isolates_duplicate_steps_across_wos_and_bilingual_wip(
    tmp_path: Path,
):
    docs = tmp_path / "docs"
    docs.mkdir()
    arch = docs / "ARCHITECTURE.md"
    arch.write_text(
        "---\nid: architecture\n---\n\n# 3. Work Orders\n\n"
        "Work Order 1 — ☐ AUSSTEHEND (Core Engine):\n"
        "- [ ] Add unit tests\n\n"
        "Work Order 2 — ⬜ AUSSTEHEND (Audio Engine):\n"
        "- [ ] Add unit tests\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        # Unscoped task checkoff in WO 1 must record effective wo=1 in mutations.log
        res_t1 = localwrite.set_task_status(tmp_path, conn, "Add unit tests", "done", wo=None)
        assert res_t1["changed"] is True
        assert res_t1["wo"] == 1
        entries = audit.tail(tmp_path, limit=5)
        assert entries[-1]["wo"] == 1

        # Bilingual status normalization in localwrite ('abgeschlossen', 'ausstehend', 'in progress')
        res_done = localwrite.set_status(tmp_path, conn, 1, "abgeschlossen")
        assert res_done["changed"] is True
        text_after = arch.read_text(encoding="utf-8")
        assert "Work Order 1 — ✅ DONE (Core Engine):" in text_after
        assert "AUSSTEHEND" not in text_after.splitlines()[6]
    finally:
        conn.close()

    # Simulate cached Google Doc with WO 1 done + WO 3 done, while lagging Drive export has
    # WO 1 open, WO 2 open with identical "- [ ] Add unit tests", and WO 3 as "🚧 IN PROGRESS".
    audit.log(tmp_path, "set_status", wo=3, status="done", verified=True)
    cached_md = (
        "# Architecture\n\n## Work Orders\n\n"
        "Work Order 1 — ✅ DONE (Core Engine):\n"
        "- [x] Add unit tests\n\n"
        "Work Order 2 — ⬜ AUSSTEHEND (Audio Engine):\n"
        "- [ ] Add unit tests\n\n"
        "Work Order 3 — ✅ DONE (Shader Pipeline):\n"
        "- [x] Compile shaders\n\n"
        "Work Order 4 — ⬜ OPEN (Netcode):\n"
        "Context prose line preserved across rescue.\n"
        "- [ ] Sync state snapshots\n"
    )
    cache_file = tmp_path / ".atlas" / "cache" / "gdd.md"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(cached_md, encoding="utf-8")

    stale_export_md = (
        "# Architecture\n\n## Work Orders\n\n"
        "Work Order 1 — ⬜ AUSSTEHEND (Core Engine):\n"
        "- [ ] Add unit tests\n\n"
        "Work Order 2 — ⬜ AUSSTEHEND (Audio Engine):\n"
        "- [ ] Add unit tests\n\n"
        "Work Order 3 — 🚧 IN PROGRESS (Shader Pipeline):\n"
        "- [ ] Compile shaders\n"
    )
    merged = gdocs._rescue_cached_work_orders(tmp_path, "gdd.md", stale_export_md)
    assert "Work Order 1 — ✅ DONE (Core Engine):" in merged
    assert "DONE AUSSTEHEND" not in merged
    assert "Work Order 3 — ✅ DONE (Shader Pipeline):" in merged
    assert "🚧 IN PROGRESS" not in merged
    assert "Context prose line preserved across rescue." in merged
    assert "- [ ] Sync state snapshots" in merged

    # Crucial: WO 1's checked "- [x] Add unit tests" is preserved, while WO 2's
    # "- [ ] Add unit tests" MUST remain unchecked!
    wo2_slice = merged.split("Work Order 2", 1)[1].split("Work Order 3", 1)[0]
    assert "- [ ] Add unit tests" in wo2_slice
    assert "- [x] Add unit tests" not in wo2_slice


def test_wo_close_auto_check_never_mutates_other_wo_or_archive_tab_and_resets_heading_scope(
    tmp_path: Path, monkeypatch
):
    fake_docs = _FragmentedDocsService()
    fake_docs.tabs["t.active"]["runs"] = [
        "Work Order 9 — ",
        "⬜ OPEN",
        " (Target WO)\n",
        "- [x] Shared step\n",
        "Work Order 10 — ",
        "⬜ OPEN",
        " (Sibling Open WO)\n",
        "- [ ] Shared step\n",
    ]
    fake_docs.tabs["t.archive"]["runs"] = [
        "Work Order 9 — ⬜ OPEN (Archive Copy)\n",
        "- [ ] Shared step\n",
    ]

    monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, fake_docs))
    monkeypatch.setattr(
        gdocs,
        "known_sources",
        lambda conn: [{"id": "doc-frag", "name": "Spec", "mimeType": gdocs.DOC_MIME}],
    )
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())
    monkeypatch.setattr(
        status_mod,
        "extract_work_orders",
        lambda conn: [
            {
                "wo": 9,
                "status": "open",
                "title": "Target WO",
                "steps": [{"text": "Shared step", "done": True}],
            },
            {
                "wo": 10,
                "status": "open",
                "title": "Sibling Open WO",
                "steps": [{"text": "Shared step", "done": False}],
            },
        ],
    )
    monkeypatch.setattr(
        status_mod,
        "status",
        lambda conn: json.dumps({"work_orders": [{"wo": 9, "status": "done", "title": "Target WO"}]}),
    )

    res = publish.set_status(tmp_path, None, 9, status="done")
    assert res["changed"] is True
    assert res["verified"] is True

    active_after = "".join(fake_docs.tabs["t.active"]["runs"])
    archive_after = "".join(fake_docs.tabs["t.archive"]["runs"])
    assert "Work Order 9 — ✅ DONE" in active_after
    # WO 10's open step in the same tab and Archive tab's step MUST stay unchecked!
    wo10_part = active_after.split("Work Order 10", 1)[1]
    assert "- [ ] Shared step" in wo10_part
    assert "- [ ] Shared step" in archive_after

    # Verify non-WO HEADING_* resets WO scope in _replace_in_body_ranges
    body_with_heading = {
        "content": [
            {
                "startIndex": 1,
                "endIndex": 32,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"startIndex": 1, "endIndex": 32, "textRun": {"content": "Work Order 9 — ✅ DONE (Test):\n"}}],
                },
            },
            {
                "startIndex": 32,
                "endIndex": 52,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "HEADING_2"},
                    "elements": [{"startIndex": 32, "endIndex": 52, "textRun": {"content": "4. General Backlog\n"}}],
                },
            },
            {
                "startIndex": 52,
                "endIndex": 71,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"startIndex": 52, "endIndex": 71, "textRun": {"content": "- [ ] Shared step\n"}}],
                },
            },
        ]
    }
    changed = publish._replace_in_body_ranges(
        fake_docs,
        "doc-frag",
        fake_docs.revision,
        body_with_heading,
        "- [ ] Shared step",
        "- [x] Shared step",
        tab_id="t.active",
        wo=9,
        max_replacements=1,
    )
    assert changed == 0


def test_gdocs_nested_child_tabs_table_cells_and_revision_conflict_retry(
    tmp_path: Path, monkeypatch
):
    class _NestedTableConflictDocs:
        def __init__(self):
            self.revision = "rev-1"
            self.cell_text = "Work Order 12 — ⬜ OPEN (Table Child Tab)\n"
            self.prefix = ""
            self.conflict_triggered = False

        @staticmethod
        def _u16(s: str) -> int:
            return len(s.encode("utf-16-le")) // 2

        def documents(self):
            return self

        def get(self, documentId=None, includeTabsContent=None):
            p_len = self._u16(self.prefix)
            cell_start = 2 + p_len
            cell_end = cell_start + self._u16(self.cell_text)
            child_body = {
                "content": [
                    *(
                        [
                            {
                                "startIndex": 1,
                                "endIndex": 1 + p_len,
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": 1 + p_len,
                                            "textRun": {"content": self.prefix},
                                        }
                                    ],
                                },
                            }
                        ]
                        if self.prefix
                        else []
                    ),
                    {
                        "startIndex": cell_start - 1,
                        "endIndex": cell_end + 1,
                        "table": {
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {
                                            "content": [
                                                {
                                                    "startIndex": cell_start,
                                                    "endIndex": cell_end,
                                                    "paragraph": {
                                                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                                        "elements": [
                                                            {
                                                                "startIndex": cell_start,
                                                                "endIndex": cell_start + 16,
                                                                "textRun": {"content": "Work Order 12 — "},
                                                            },
                                                            {
                                                                "startIndex": cell_start + 16,
                                                                "endIndex": cell_end,
                                                                "textRun": {
                                                                    "content": self.cell_text[16:]
                                                                },
                                                            },
                                                        ],
                                                    },
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        },
                    },
                ]
            }
            return _Req(
                {
                    "revisionId": self.revision,
                    "tabs": [
                        {
                            "tabProperties": {"tabId": "t.root", "title": "Root"},
                            "documentTab": {"body": {"content": []}},
                            "childTabs": [
                                {
                                    "tabProperties": {"tabId": "t.child.table", "title": "Milestone Table"},
                                    "documentTab": {"body": child_body},
                                }
                            ],
                        }
                    ],
                }
            )

        def batchUpdate(self, documentId=None, body=None):
            reqs = body.get("requests", [])
            req_rev = (body.get("writeControl") or {}).get("requiredRevisionId")
            if "replaceAllText" in reqs[0]:
                # Fragmented across runs -> 0 occurrences
                return _Req({"replies": [{"replaceAllText": {"occurrencesChanged": 0}}]})
            if not self.conflict_triggered:
                self.conflict_triggered = True
                # Concurrent collaborator inserts emoji text, bumping revision & shifting UTF-16 indices!
                self.prefix = "Concurrent edit 🚀\n"
                self.revision = "rev-2"
                raise RuntimeError(
                    f"HttpError 400: The document has been modified since the revision "
                    f"specified in the WriteControl (requiredRevisionId={req_rev})."
                )
            assert req_rev == "rev-2"
            p_len = self._u16(self.prefix)
            cell_base = 2 + p_len
            for r in reqs:
                if "deleteContentRange" in r:
                    rng = r["deleteContentRange"]["range"]
                    assert rng["tabId"] == "t.child.table"
                    rel_s = rng["startIndex"] - cell_base
                    rel_e = rng["endIndex"] - cell_base
                    before, after = _FragmentedDocsService._slice_utf16(self.cell_text, rel_s, rel_e)
                    self.cell_text = before + after
                elif "insertText" in r:
                    loc = r["insertText"]["location"]
                    assert loc["tabId"] == "t.child.table"
                    rel_i = loc["index"] - cell_base
                    before, after = _FragmentedDocsService._slice_utf16(self.cell_text, rel_i, rel_i)
                    self.cell_text = before + r["insertText"]["text"] + after
            return _Req({"replies": [{} for _ in reqs]})

    svc = _NestedTableConflictDocs()
    monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, svc))
    monkeypatch.setattr(
        gdocs,
        "known_sources",
        lambda conn: [{"id": "doc-tbl", "name": "Master", "mimeType": gdocs.DOC_MIME}],
    )
    monkeypatch.setattr(gdocs, "assert_writable", lambda conn, file_id: None)
    monkeypatch.setattr(gdocs, "sync", lambda *args, **kwargs: {"written": []})
    monkeypatch.setattr(gdocs, "load_credentials", lambda **kwargs: object())
    monkeypatch.setattr(
        status_mod,
        "extract_work_orders",
        lambda conn: [{"wo": 12, "status": "open", "title": "Table Child Tab"}],
    )
    monkeypatch.setattr(
        status_mod,
        "status",
        lambda conn: json.dumps(
            {"work_orders": [{"wo": 12, "status": "done", "title": "Table Child Tab"}]}
        ),
    )

    res = publish.set_status(tmp_path, None, 12, status="done")
    assert svc.conflict_triggered is True
    assert res["changed"] is True
    assert res["verified"] is True
    assert res["tab_id"] == "t.child.table"
    assert svc.cell_text == "Work Order 12 — ✅ DONE (Table Child Tab)\n"


def test_prose_wo_reference_preserves_checklist_and_reconcile_syncs_existing_gdocs_status(
    tmp_path: Path, monkeypatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    arch = docs / "ARCHITECTURE.md"
    arch.write_text(
        "---\nid: architecture\n---\n\n# 3. Work Orders\n\n"
        "Work Order 1 — ✅ DONE (Combat Core): Base formulas.\n\n"
        "Work Order 2 — ✅ DONE (Combat HUD):\n"
        "Builds on damage API from Work Order 1 — see combat spec.\n"
        "- [x] Wire HUD signal\n"
        "- [x] Add unit tests\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        wos = status_mod.extract_work_orders(conn)
        wo2 = next(item for item in wos if item["wo"] == 2)
        assert [s["text"] for s in wo2["steps"]] == ["Wire HUD signal", "Add unit tests"]

        # Bound Google Doc ALREADY has Work Order 2 as `⬜ OFFEN` (`gdocs_existing: True`).
        # `reconcile_work_orders` must update the Google Doc header to `✅ DONE`!
        fake = _FragmentedDocsService()
        fake.tabs = {
            "t.1": {
                "title": "Plan",
                "runs": [
                    "Work Order 2 — ⬜ OFFEN (Combat HUD):\n- [ ] Wire HUD signal\n- [ ] Add unit tests\n"
                ],
            }
        }
        monkeypatch.setattr(gdocs, "build_services", lambda creds: (None, None, fake))
        monkeypatch.setattr(
            gdocs,
            "known_sources",
            lambda c: [{"id": "doc-rec", "name": "Plan", "mimeType": gdocs.DOC_MIME}],
        )
        monkeypatch.setattr(gdocs, "assert_writable", lambda c, fid: None)
        monkeypatch.setattr(gdocs, "sync", lambda *a, **kw: {"written": []})
        monkeypatch.setattr(gdocs, "load_credentials", lambda **kw: object())

        # Default local-first reconcile does NOT mutate bound Google Docs (`synced_to_gdocs == []`)
        rec_local_only = status_mod.reconcile_work_orders(tmp_path, conn, dry_run=False)
        assert rec_local_only["synced_to_gdocs"] == []
        assert "⬜ OFFEN" in "".join(fake.tabs["t.1"]["runs"])

        # Explicit opt-in `sync_gdocs=True` (`--gdocs`) mirrors WO 2 status & steps to Google Docs
        rec = status_mod.reconcile_work_orders(tmp_path, conn, dry_run=False, sync_gdocs=True)
        assert 2 in rec["synced_to_gdocs"]
        gdoc_text = "".join(fake.tabs["t.1"]["runs"])
        assert "Work Order 2 — ✅ DONE" in gdoc_text
        assert "- [x] Wire HUD signal" in gdoc_text
        assert "- [x] Add unit tests" in gdoc_text
    finally:
        conn.close()


def test_wiring_gate_rejects_unused_imports_and_ignores_docstring_ghost_definitions(
    tmp_path: Path,
):
    src = tmp_path / "src"
    src.mkdir()
    (src / "combat.py").write_text(
        '"""Combat module.\n\nExample:\n    def ghost_docstring_fn(x):\n        return x\n"""\n'
        "# def ghost_comment_fn(x): return x\n\n"
        "def dead_imported_fn(dmg: int) -> int:\n"
        "    return max(0, dmg)\n",
        encoding="utf-8",
    )
    (src / "caller.py").write_text(
        "from .combat import (\n    dead_imported_fn,\n)\n",
        encoding="utf-8",
    )
    (src / "combat.ts").write_text(
        "export function deadTsFn(x: number): number {\n  return x * 2;\n}\n",
        encoding="utf-8",
    )
    (src / "consumer.ts").write_text(
        'import {\n  deadTsFn,\n} from "./combat";\n',
        encoding="utf-8",
    )
    (src / "combat.rs").write_text(
        "pub fn dead_rs_fn(x: i32) -> i32 {\n    x + 1\n}\n",
        encoding="utf-8",
    )
    (src / "main.rs").write_text(
        "use crate::combat::{\n    dead_rs_fn,\n};\n",
        encoding="utf-8",
    )

    # 1. Ghost definitions in docstrings/comments must never be returned by code.find
    assert code.find(tmp_path, "ghost_docstring_fn") == []
    assert code.find(tmp_path, "ghost_comment_fn") == []

    # 2. Pure imports across Python, TypeScript, and Rust must NOT pass verify.check
    for sym in ("dead_imported_fn", "deadTsFn", "dead_rs_fn"):
        refs = code.references(tmp_path, sym)
        assert refs["call_count"] == 0 and refs["ref_count"] == 0, f"{sym} leaked via import: {refs}"
        gate = verify.check(tmp_path, [sym])
        assert gate["ok"] is False

    # 3. Unindexed completed WO in wo-symbols.yml + mutations.log must still fail verify.check_project
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ARCHITECTURE.md").write_text(
        "---\nid: architecture\n---\n\n# 3. Work Orders\n\n"
        "Work Order 1 — ✅ DONE (Setup): Done.\n",
        encoding="utf-8",
    )
    verify.record_wo_symbols(tmp_path, 42, ["dead_imported_fn"])
    audit.log(tmp_path, "set_status", wo=42, status="done", verified=True)
    conn = indexer.open_index(tmp_path)
    try:
        report = verify.check_project(tmp_path, conn)
        assert report["ok"] is False
        assert any(r["wo"] == 42 and r["symbol"] == "dead_imported_fn" for r in report["regressions"])
    finally:
        conn.close()


def test_multilang_raw_strings_arrow_generics_ts_destructured_tscn_callable_and_backtick_body(
    tmp_path: Path,
):
    src = tmp_path / "src"
    src.mkdir()
    # Rust raw string `r"\"` + arrow generic `Fn(&Unit) -> bool`
    (src / "engine.rs").write_text(
        'pub fn map_units<F: Fn(&Unit) -> bool>(f: F) -> usize {\n'
        '    let sep = r"\\"; // dead_raw_target()\n'
        '    let doc = r#"\n'
        '        dead_raw_target()\n'
        '    "#;\n'
        '    sep.len() + doc.len()\n'
        '}\n'
        'pub fn dead_raw_target() -> usize { 0 }\n',
        encoding="utf-8",
    )
    # C# verbatim string `@"C:\"` + trailing comment
    (src / "Paths.cs").write_text(
        'public class Paths {\n'
        '    public static string Root() {\n'
        '        var p = @"C:\\"; // dead_raw_target()\n'
        '        return p;\n'
        '    }\n'
        '}\n',
        encoding="utf-8",
    )
    # Go raw backtick string `` `C:\` `` + trailing comment
    (src / "paths.go").write_text(
        'package main\n'
        'func winSep() string {\n'
        '    return `C:\\` // dead_raw_target()\n'
        '}\n',
        encoding="utf-8",
    )
    # Kotlin `"""..."""` raw multiline string
    (src / "Spec.kt").write_text(
        'fun describe(): String {\n'
        '    return """\n'
        '        dead_raw_target()\n'
        '    """\n'
        '}\n',
        encoding="utf-8",
    )
    # TS arrow-type generic, callback-typed multiline method, destructured bare method, & multiline template literal
    (src / "store.ts").write_text(
        "export function createStore<T = () => number>(init: T): T {\n"
        "  const tpl = `\n"
        "unindented line inside template literal\n"
        "still inside createStore\n"
        "`;\n"
        "  return init;\n"
        "}\n\n"
        "export class CardView {\n"
        "  onTurn(\n"
        "    cb: (n: number) => void,\n"
        "  ): void {\n"
        "    cb(1);\n"
        "  }\n"
        "  renderCard({ title, cost = 1 }) {\n"
        "    return `${title}:${cost}`;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    # Godot 4 .tscn StringName `method=&"..."` and SubResource `Callable(..., "_on_callable_hit")`
    (src / "hud.gd").write_text(
        "extends Control\n"
        "func _on_stringname_hit() -> void:\n\tpass\n"
        "func _on_callable_hit() -> void:\n\tpass\n",
        encoding="utf-8",
    )
    (src / "hud.tscn").write_text(
        '[gd_scene format=3]\n'
        '[sub_resource type="Resource" id="1"]\n'
        'callback = Callable(NodePath("."), "_on_callable_hit")\n'
        '[connection signal="pressed" from="Btn" to="." method=&"_on_stringname_hit"]\n',
        encoding="utf-8",
    )

    # Raw/verbatim/triple strings did NOT leak false-positive calls to dead_raw_target
    raw_refs = code.references(tmp_path, "dead_raw_target")
    assert raw_refs["call_count"] == 0 and raw_refs["ref_count"] == 0

    # Arrow generics, callback-param multiline TS method, and destructured method are all found
    for sym in ("map_units", "createStore", "onTurn", "renderCard"):
        found = code.find(tmp_path, sym, exact=True, include_body=True)
        assert len(found) == 1, f"Missing definition for {sym}"

    # Multiline backtick template literal did not truncate `createStore` before `return init;`
    store_hit = code.find(tmp_path, "createStore", exact=True, include_body=True)[0]
    assert "return init;" in store_hit["body"]

    # Godot 4 .tscn StringName & Callable bindings register as references
    assert code.references(tmp_path, "_on_stringname_hit")["ref_count"] == 1
    assert code.references(tmp_path, "_on_callable_hit")["ref_count"] == 1


def test_mixed_de_en_queries_closed_compounds_3char_stems_streaming_audit_and_mcp_schemas(
    tmp_path: Path,
):
    from atlas import lint as lint_mod, mcp_server

    # 1. Mixed German-English stopword disambiguation
    assert "war" in normalize.terms("Wo ist Fog of War beschrieben?")
    assert "die" in normalize.terms("Wie funktioniert der Hit Die Roll?")
    assert "hat" in normalize.terms("Where is the wizard hat item?")
    assert "man" in normalize.terms("Last man standing rules")
    # Pure German function words remain filtered
    de_pure = normalize.terms("Wo war die Waffe und wer hat man gefragt?")
    for stop in ("war", "die", "hat", "man"):
        assert stop not in de_pure

    # 2. Closed compound ('Rüstungs-Mitigation' -> '# Rüstungsmitigation') & 3-char stems ('Turn-Order-Tiebreaker', 'Eisschaden', 'Hexfeld')
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "combat.md").write_text(
        "---\nid: combat\nimplements:\n  - src/combat.py\ntags:\n  - core\n---\n\n"
        "# Rüstungsmitigation\n\nReduziert eingehenden physischen Schaden.\n\n"
        "# Tie-Breaker\n\nBei gleicher Initiative entscheidet Agility über die Turn Order.\n\n"
        "# Eis & Schaden\n\nFrost verlangsamt Ziele und verursacht Zusatzschaden.\n\n"
        "# Hex-Feld\n\nJede Einheit besetzt genau ein Feld auf dem Taktikgitter.\n"
        "- [ ] Balance frost scaling @combat\n",
        encoding="utf-8",
    )
    conn = indexer.open_index(tmp_path)
    try:
        w_armor = json.loads(search.where(conn, "Rüstungs-Mitigation"))
        assert w_armor["found"] is True
        assert "ruestungsmitigation" in w_armor["anchor"]

        w_tie = json.loads(search.where(conn, "Turn-Order-Tiebreaker"))
        assert w_tie["found"] is True
        assert "tie-breaker" in w_tie["anchor"]

        w_ice = json.loads(search.where(conn, "Eisschaden"))
        assert w_ice["found"] is True
        assert "eis" in w_ice["anchor"]

        w_hex = json.loads(search.where(conn, "Hexfeld"))
        assert w_hex["found"] is True
        assert "hex-feld" in w_hex["anchor"]

        # 3. Verify MCP tool docstrings match actual JSON payload keys (0 phantom keys)
        read_payload = json.loads(search.read(conn, w_armor["anchor"], max_chars=500))
        for k in ("anchor", "heading", "breadcrumb", "path", "line", "chars", "total_chars", "offset", "truncated", "body"):
            assert k in read_payload
            assert k in (mcp_server.atlas_read.__doc__ or "")

        outline_payload = json.loads(search.outline(conn))
        for k in ("outline", "docs", "truncated"):
            assert k in outline_payload
            assert k in (mcp_server.atlas_outline.__doc__ or "")
        assert "{doc," not in (mcp_server.atlas_outline.__doc__ or "")

        tasks_payload = json.loads(search.tasks(conn, status="open"))
        assert tasks_payload["count"] == 1
        for k in ("anchor", "path", "line", "text", "owner", "tag", "done"):
            assert k in tasks_payload["tasks"][0]
            assert k in (mcp_server.atlas_tasks.__doc__ or "")
        assert "outgoing_links" not in (mcp_server.atlas_links.__doc__ or "")

        links_payload = json.loads(search.links(conn, w_armor["anchor"]))
        for k in ("anchor", "heading", "path", "implements", "tags", "siblings"):
            assert k in links_payload
            assert k in (mcp_server.atlas_links.__doc__ or "")

        lint_payload = json.loads(lint_mod.lint(conn))
        for k in ("ok", "sections", "counts", "issues", "managed_issues", "discovered_issues", "truncated"):
            assert k in lint_payload
        assert ">5,000 chars" in (mcp_server.atlas_lint.__doc__ or "")
    finally:
        conn.close()

    # 4. Streaming audit.tail(limit=0, only_wo=True) skips non-WO entries
    audit.log(tmp_path, "sheet_update", tab="Units", row_key="goblin", verified=True)
    audit.log(tmp_path, "publish", wo=77, status="done", verified=True)
    wo_only = audit.tail(tmp_path, limit=0, only_wo=True)
    assert [e["wo"] for e in wo_only] == [77]

