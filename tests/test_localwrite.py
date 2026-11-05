"""Lokaler Schreibpfad: Work Orders ohne Google-Konto abhaken.

Der Weg, den `atlas init` beschreibt („Status ist ein Tool-Call, keine
Textänderung"), endete bisher in „Kein gültiges Google-Token". Diese Tests
halten den geschlossenen Kreis fest: init -> status -> set_status -> status.
"""

from __future__ import annotations

import json

from atlas import bootstrap, indexer, localwrite
from atlas import publish as publish_mod
from atlas import status as status_mod


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "engine.gd").write_text(
        "func start() -> void:\n\tpass\n", encoding="utf-8")
    bootstrap.init_project(root)
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)
    return root, conn


def test_local_set_status_closes_the_loop(tmp_path):
    root, conn = _project(tmp_path)
    doc = root / "docs" / "ARCHITECTURE.md"
    assert "⬜ OFFEN" in doc.read_text(encoding="utf-8")

    payload = publish_mod.set_status(root, conn, 1, "done")

    assert payload["verified"] is True
    assert payload["source"] == "local"
    # Die Statuszeile selbst prüfen, nicht das ganze Dokument: der
    # Erklärtext darunter nennt "⬜ OFFEN" als Formatbeispiel.
    found = localwrite.find_line(root, 1)
    assert found is not None
    assert "✅ ERLEDIGT" in found[2].group(0)
    assert "⬜ OFFEN" not in found[2].group(0)


def test_local_set_status_survives_index_rebuild(tmp_path):
    """Nach dem Write muss `status` den neuen Stand lesen — aus der Datei."""
    root, conn = _project(tmp_path)
    publish_mod.set_status(root, conn, 1, "done")

    data = json.loads(status_mod.status(conn))

    assert data["counts"]["done"] == 1
    assert data["counts"]["open"] == 0


def test_local_set_status_is_idempotent(tmp_path):
    root, conn = _project(tmp_path)
    publish_mod.set_status(root, conn, 1, "done")

    second = publish_mod.set_status(root, conn, 1, "done")

    # Der Index ist aktuell, also antwortet schon der Vorab-Check — ohne
    # zweiten Schreibvorgang auf die Datei.
    assert second.get("changed") is False
    assert second.get("written") is not True


def test_local_set_status_keeps_the_title(tmp_path):
    """Nur der Marker wandert, der Titel bleibt."""
    root, conn = _project(tmp_path)
    before = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    publish_mod.set_status(root, conn, 1, "done")

    after = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Architektur-Doku vervollständigen" in before
    assert "Architektur-Doku vervollständigen" in after


def test_local_publish_appends_a_new_work_order(tmp_path):
    root, conn = _project(tmp_path)

    payload = localwrite.publish(
        root, conn, 2,
        "Work Order 2 — ⬜ OFFEN (Zweiter Schritt): Test-Eintrag",
        "3. Work Orders",
    )

    assert payload["verified"] is True
    found = localwrite.find_line(root, 2)
    assert found is not None
    assert "Zweiter Schritt" in found[2].group(0)


def test_local_publish_refuses_duplicates(tmp_path):
    root, conn = _project(tmp_path)
    localwrite.publish(root, conn, 2, "Work Order 2 — ⬜ OFFEN (A)", "3. Work Orders")

    second = localwrite.publish(root, conn, 2, "Work Order 2 — ⬜ OFFEN (B)", "3. Work Orders")

    assert second["written"] is False
    assert "existiert bereits" in second["reason"]


def test_unknown_work_order_explains_the_next_step(tmp_path):
    root, conn = _project(tmp_path)

    payload = localwrite.set_status(root, conn, 99, "done")

    assert payload["verified"] is False
    assert payload["changed"] is False
    assert "atlas publish" in payload["reason"]


def test_local_set_status_carries_changed_and_verify_gate(tmp_path):
    root, conn = _project(tmp_path)

    payload = publish_mod.set_status(root, conn, 1, "done")

    assert payload["changed"] is True
    assert payload["written"] is True
    assert "verify" in payload
