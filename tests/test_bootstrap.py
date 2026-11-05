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


def test_dry_run_writes_nothing(tmp_path):
    root = _dummy_repo(tmp_path)
    report = bootstrap.init_project(root, dry_run=True)

    assert report["dry_run"] is True
    assert report["created"] == []
    assert "docs/ARCHITECTURE.md" in report["would_create"]
    assert "AGENTS.md" in report["would_create"]
    assert not (root / "docs").exists()
    assert not (root / "AGENTS.md").exists()


def test_unwired_ignores_test_functions(tmp_path):
    """pytest ruft `test_*` per Namenskonvention — kein fehlender Aufrufer."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "tests").mkdir()
    body = "".join(f"    line_{i} = {i}\n" for i in range(16))
    (root / "tests" / "test_feature.py").write_text(
        f"def test_something():\n{body}    return True\n", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text(
        f"def fixture_like_setup():\n{body}    return True\n", encoding="utf-8")

    scan = bootstrap.scan_repository(root)

    symbols = {item["symbol"] for item in scan["unwired"]}
    assert "test_something" not in symbols
    assert "fixture_like_setup" in symbols


def test_unwired_ignores_decorated_functions(tmp_path):
    """`@mcp.tool()` bindet eine Funktion an ein Framework, ohne Aufruf."""
    root = tmp_path / "proj"
    root.mkdir()
    body = "".join(f"    line_{i} = {i}\n" for i in range(16))
    (root / "server.py").write_text(
        "mcp = object()\n\n"
        f"@mcp.tool()\ndef registered_tool():\n{body}    return True\n\n"
        f"def plain_function():\n{body}    return True\n",
        encoding="utf-8",
    )

    symbols = {item["symbol"] for item in bootstrap.scan_repository(root)["unwired"]}

    assert "registered_tool" not in symbols
    assert "plain_function" in symbols


def test_core_symbols_are_the_big_ones_not_the_first(tmp_path):
    """`_ready` und Getter sind keine Kern-Symbole, auch wenn sie oben stehen."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "game.gd").write_text(
        "extends Node\n\n"
        "func _ready() -> void:\n\tpass\n\n"
        "func get_int(key: String) -> int:\n\treturn 0\n\n"
        "func get_float(key: String) -> float:\n\treturn 0.0\n\n"
        "func resolve_combat_round(attacker, defender) -> Dictionary:\n"
        + "".join(f"\tvar value_{i} := {i}\n" for i in range(12))
        + "\treturn {}\n",
        encoding="utf-8",
    )

    scan = bootstrap.scan_repository(root)
    symbols = scan["domains"]["(Wurzel)"]["symbols"]

    assert symbols[0] == "resolve_combat_round"
    assert "_ready" not in symbols
    assert "get_int" not in symbols


def test_class_methods_have_correct_length(tmp_path):
    """Eine Methode endet an der nächsten Methode, nicht am Klassenende."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "service.py").write_text(
        "class Service:\n"
        "    def short_a(self):\n"
        "        return 1\n"
        "\n"
        "    def short_b(self):\n"
        "        return 2\n"
        "\n"
        "    def long_one(self):\n"
        + "".join(f"        step_{i} = {i}\n" for i in range(20))
        + "        return 3\n",
        encoding="utf-8",
    )

    scan = bootstrap.scan_repository(root)
    texts = {f["path"]: f for f in scan["domains"]["(Wurzel)"]["files"]}

    # Nur die lange Methode ist ein Kern-Symbol, nicht die zwei kurzen.
    assert scan["domains"]["(Wurzel)"]["symbols"] == ["long_one"]
    # Und nur sie ist lang genug für die Unwired-Prüfung.
    assert [u["symbol"] for u in scan["unwired"]] == ["long_one"]
    assert scan["unwired"][0]["lines"] == 22


def test_multiline_decorator_counts_as_framework_binding(tmp_path):
    """`@app.post(...)` über drei Zeilen: die Zeile über `def` ist `)`."""
    root = tmp_path / "proj"
    root.mkdir()
    body = "".join(f"    line_{i} = {i}\n" for i in range(16))
    (root / "api.py").write_text(
        "app = object()\n\n"
        "@app.post(\n"
        '    "/api/run",\n'
        "    response_model=dict,\n"
        ")\n"
        f"def run_endpoint():\n{body}    return True\n",
        encoding="utf-8",
    )

    assert bootstrap.scan_repository(root)["unwired"] == []


def test_subproject_is_marked_in_document(tmp_path):
    """Ein Ordner mit eigenem project.godot ist eine Teilkopie, kein Teil des Ganzen."""
    root = tmp_path / "proj"
    (root / "games" / "dungeon").mkdir(parents=True)
    (root / "games" / "dungeon" / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (root / "games" / "dungeon" / "main.gd").write_text("func start() -> void:\n\tpass\n", encoding="utf-8")

    scan = bootstrap.scan_repository(root)

    assert scan["domains"]["games"]["subproject"] == "dungeon/project.godot"
    document = bootstrap.generate_architecture_skeleton(scan)
    assert "Sub-Projekt" in document
    assert "dungeon/project.godot" in document


TS_APP = """import React from "react";

export function calculateRevenue(
  studio: StudioData,
  year: number,
): RevenueResult {
  const base = studio.rate * year;
  const bonus = studio.bonus ?? 0;
  return { total: base + bonus };
}

export const Modal = React.memo(({ isOpen }: Props) => {
  if (!isOpen) return null;
  return <div />;
});

export const Input = forwardRef<HTMLInputElement, Props>((props, ref) => {
  return <input ref={ref} {...props} />;
});

function internalHelper(value: number) {
  const a = value * 1;
  const b = a + 2;
  const c = b * 3;
  const d = c - 4;
  const e = d / 5;
  const f = e + 6;
  const g = f * 7;
  const h = g - 8;
  const i = h + 9;
  const j = i * 10;
  const k = j - 11;
  const l = k + 12;
  const m = l * 13;
  const n = m - 14;
  const o = n + 15;
  return Math.round(o);
}
"""


def _ts_project(tmp_path, files: dict[str, str]):
    root = tmp_path / "tsproj"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_typescript_multiline_signature_is_detected(tmp_path):
    """`export function x(\n …\n): T {` — die Klammer steht auf einer anderen Zeile."""
    root = _ts_project(tmp_path, {"src/services.ts": TS_APP})
    names = {name for name, _start, _line in bootstrap._iter_defs(TS_APP)}
    assert "calculateRevenue" in names


def test_react_wrappers_are_detected(tmp_path):
    """`React.memo((…) =>` und `forwardRef<…>((…) =>` sind Arrow-Functions."""
    root = _ts_project(tmp_path, {"src/components.tsx": TS_APP})
    names = {name for name, _start, _line in bootstrap._iter_defs(TS_APP)}
    assert "Modal" in names
    assert "Input" in names


def test_unused_ts_helper_is_reported(tmp_path):
    """Der Wrapper-Erkennung sei Dank: nur der wirklich ungenutzte Helfer bleibt."""
    root = _ts_project(tmp_path, {"src/services.ts": TS_APP})
    unwired = {item["symbol"] for item in bootstrap.scan_repository(root)["unwired"]}

    assert "internalHelper" in unwired
    assert "calculateRevenue" not in unwired   # export function = angebunden? nein: nur Definition
    assert "Modal" not in unwired


def test_control_structures_are_not_functions(tmp_path):
    """`if (x) {` sieht wie eine Methode aus, ist keine."""
    root = _ts_project(tmp_path, {"src/flow.ts": (
        "export function decide(value: number) {\n"
        "  if (value > 0) {\n"
        "    return 'positive';\n"
        "  }\n"
        "  for (let i = 0; i < 3; i++) {\n"
        "    value += i;\n"
        "  }\n"
        "  return 'negative';\n"
        "}\n"
    )})
    names = {item["symbol"] for item in bootstrap.scan_repository(root)["unwired"]}
    symbols = bootstrap.scan_repository(root)["domains"]["src"]["symbols"]

    assert "if" not in symbols
    assert "for" not in symbols
    assert "decide" in symbols


def test_entry_file_export_default_is_not_unwired(tmp_path):
    """`App.tsx` wird vom Bundler gemountet — `export default` braucht keinen Aufrufer."""
    root = _ts_project(tmp_path, {"src/App.tsx": (
        "export default function Dashboard() {\n"
        + "".join(f"  const row_{i} = {i};\n" for i in range(16))
        + "  return <div />;\n"
        "}\n"
    )})
    unwired = {item["symbol"] for item in bootstrap.scan_repository(root)["unwired"]}

    assert "Dashboard" not in unwired
