"""`atlas init`: Ersterfassung eines Repos ohne Doku."""

from __future__ import annotations

import json

from atlas import bootstrap, indexer, lint as lint_mod
from atlas import status as status_mod


def _dummy_repo(tmp_path):
    """Zwei Domänen, drei Code-Dateien: eine benutzte, eine tote Funktion, ein Hotspot."""
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "ui").mkdir()

    (root / "core" / "engine.gd").write_text(
        "extends Node\n\n"
        "func used_helper(value: int) -> int:\n"
        "\treturn value * 2\n\n"
        "func entry_point() -> int:\n"
        "\treturn used_helper(21)\n",
        encoding="utf-8",
    )
    # Tote Funktion: 18 Zeilen, wird nirgends erwähnt.
    (root / "core" / "orphans.gd").write_text(
        "extends Node\n\n"
        "func orphan_function(input: int) -> int:\n"
        + "".join(f"\tvar step_{i} := input + {i}\n" for i in range(15))
        + "\treturn step_14\n",
        encoding="utf-8",
    )
    # Hotspot: > 10.000 Tokens
    (root / "ui" / "huge.gd").write_text(
        "extends Control\n\n" + ("# filler line for size\n" * 2500), encoding="utf-8"
    )
    return root


def test_scan_finds_domains_hotspot_and_unwired(tmp_path):
    root = _dummy_repo(tmp_path)
    scan = bootstrap.scan_repository(root)

    assert set(scan["domains"]) == {"core", "ui"}
    assert scan["files"] == 3
    assert any(item["path"].endswith("huge.gd") for item in scan["hotspots"])
    symbols = {item["symbol"] for item in scan["unwired"]}
    assert "orphan_function" in symbols
    assert "used_helper" not in symbols
    assert "entry_point" not in symbols


def test_init_creates_lint_clean_document_and_work_order(tmp_path):
    root = _dummy_repo(tmp_path)
    report = bootstrap.init_project(root)

    assert "docs/ARCHITECTURE.md" in report["created"]
    document = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Work Order 1 — ⬜ OFFEN" in document
    assert "orphan_function" in document          # Risiko-Abschnitt
    assert "core/engine.gd" in document           # Dateipfad für atlas_links

    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)
    result = json.loads(lint_mod.lint(conn))
    assert result["issues"] == [], result["issues"]


def test_init_status_sees_open_work_order(tmp_path):
    root = _dummy_repo(tmp_path)
    bootstrap.init_project(root)
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)

    payload = json.loads(status_mod.status(conn))
    assert payload["counts"]["total"] == 1
    assert payload["work_orders"][0]["wo"] == 1
    assert payload["work_orders"][0]["status"] == "open"


def test_init_indexes_domain_for_search(tmp_path):
    root = _dummy_repo(tmp_path)
    bootstrap.init_project(root)
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)

    rows = conn.execute("SELECT heading, path, body FROM sections").fetchall()
    assert rows, "Index muss Abschnitte enthalten"
    blob = " ".join(f"{r['heading']} {r['path']} {r['body']}" for r in rows)
    assert "core" in blob


def test_second_init_keeps_existing_files(tmp_path):
    root = _dummy_repo(tmp_path)
    bootstrap.init_project(root)
    doc = root / "docs" / "ARCHITECTURE.md"
    before = doc.read_text(encoding="utf-8")

    second = bootstrap.init_project(root)

    assert "docs/ARCHITECTURE.md" in second["kept"]
    assert second["created"] == [] or all("gitignore" in c for c in second["created"])
    assert doc.read_text(encoding="utf-8") == before


def test_init_never_overwrites_own_agents_md(tmp_path):
    root = _dummy_repo(tmp_path)
    own = "# Meine Regeln\n\nHandgeschrieben.\n"
    (root / "AGENTS.md").write_text(own, encoding="utf-8")

    report = bootstrap.init_project(root)

    assert "AGENTS.md" in report["kept"]
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == own


def test_init_adds_atlas_dir_to_gitignore(tmp_path):
    root = _dummy_repo(tmp_path)
    (root / ".gitignore").write_text(".godot/\n", encoding="utf-8")

    bootstrap.init_project(root)

    content = (root / ".gitignore").read_text(encoding="utf-8")
    assert ".atlas/" in content
    assert ".godot/" in content


def test_find_root_stops_at_home(tmp_path, monkeypatch):
    """Das Home ist kein Projekt — dort liegt der globale Konfig-Ordner."""
    from atlas import config

    home = tmp_path / "home"
    (home / ".gdd").mkdir(parents=True)
    project = home / "work" / "proj"
    project.mkdir(parents=True)

    monkeypatch.setattr(config, "_home", lambda: home.resolve())
    monkeypatch.chdir(project)

    assert config.find_root() == project.resolve()


def test_find_root_ignores_global_config_dir(tmp_path, monkeypatch):
    """`~/.atlas` ist Token-Ablage, kein Projekt-Marker."""
    from atlas import config

    home = tmp_path / "home"
    (home / ".atlas").mkdir(parents=True)
    project = home / "code" / "thing"
    project.mkdir(parents=True)
    (project / ".atlas").mkdir()

    monkeypatch.setattr(config, "_home", lambda: home.resolve())
    monkeypatch.chdir(project)

    assert config.find_root() == project.resolve()
