"""Code symbol search: signature instead of a 125k-token file."""

from __future__ import annotations

from atlas import code


def _project(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "unit.gd").write_text(
        "extends Node\n"
        "\n"
        "func swap_positions_with_ally(ally: Unit) -> bool:\n"
        "\treturn true\n"
        "\n"
        "static func has_perk(perk_id: String) -> bool:\n"
        "\treturn false\n"
        "\n"
        "func unrelated() -> void:\n"
        "\tpass\n",
        encoding="utf-8",
    )
    (scripts / "grid.gd").write_text(
        "class_name GridManager\n"
        "\n"
        "func find_path(from: Vector2i, to: Vector2i) -> Array:\n"
        "\treturn []\n",
        encoding="utf-8",
    )
    ignored = tmp_path / ".godot"
    ignored.mkdir()
    (ignored / "cache.gd").write_text("func swap_positions_with_ally():\n\tpass\n", encoding="utf-8")
    return tmp_path


def test_finds_function_with_signature(tmp_path):
    root = _project(tmp_path)
    hits = code.find(root, "swap_positions")

    assert len(hits) == 1
    assert hits[0]["path"] == "scripts/unit.gd"
    assert hits[0]["line"] == 3
    assert hits[0]["signature"] == "func swap_positions_with_ally(ally: Unit) -> bool"


def test_finds_class_name(tmp_path):
    root = _project(tmp_path)
    hits = code.find(root, "GridManager")
    assert hits[0]["kind"] == "class"
    assert hits[0]["signature"] == "class_name GridManager"


def test_ignores_godot_cache(tmp_path):
    root = _project(tmp_path)
    paths = [hit["path"] for hit in code.find(root, "swap_positions")]
    assert all(not path.startswith(".godot") for path in paths)


def test_unknown_symbol_returns_empty(tmp_path):
    root = _project(tmp_path)
    assert code.find(root, "gibt_es_nicht") == []


def test_render_is_compact(tmp_path):
    root = _project(tmp_path)
    text = code.render(root, "has_perk")
    assert "scripts/unit.gd:6" in text
    assert "static func has_perk(perk_id: String) -> bool" in text
    # Response is one line per hit, not the full file content.
    assert len(text) < 600


# --------------------------------------------------------------------------
# Definition vs. call site — the check that was missing in WO 8
# --------------------------------------------------------------------------

def test_usages_exclude_the_definition_itself(tmp_path):
    root = _project(tmp_path)
    (root / "scripts" / "turn_manager.gd").write_text(
        "func do_swap(hero, ally):\n"
        "\tvar ok: bool = hero.swap_positions_with_ally(ally)\n"
        "\treturn ok\n",
        encoding="utf-8",
    )
    usages = code.find_usages(root, "swap_positions_with_ally")

    assert len(usages) == 1
    assert usages[0]["path"] == "scripts/turn_manager.gd"
    assert usages[0]["line"] == 2
    assert not any(u["path"] == "scripts/unit.gd" for u in usages)


def test_render_warns_about_dead_function(tmp_path):
    root = _project(tmp_path)
    text = code.render(root, "swap_positions_with_ally", with_usage=True)

    assert "1 definition(s)" in text
    assert "NEVER called" in text
    assert "dead code" in text
    # The wiring summary line is always included, not only on request.
    assert "0 call(s), 0 reference(s)" in text


def test_render_shows_call_sites(tmp_path):
    root = _project(tmp_path)
    (root / "scripts" / "turn_manager.gd").write_text(
        "func do_swap(hero, ally):\n"
        "\treturn hero.swap_positions_with_ally(ally)\n",
        encoding="utf-8",
    )
    text = code.render(root, "swap_positions_with_ally", with_usage=True)

    assert "Calls (1):" in text
    assert "scripts/turn_manager.gd:2" in text
    assert "NEVER called" not in text


def test_scene_connection_counts_as_usage(tmp_path):
    """`.tscn` signals are half the wiring in Godot — and were previously missed by scans."""
    root = _project(tmp_path)
    scenes = root / "scenes"
    scenes.mkdir()
    (scenes / "fight.tscn").write_text(
        "[gd_scene format=3]\n"
        "\n"
        '[connection signal="pressed" from="Button" to="." '
        'method="swap_positions_with_ally"]\n',
        encoding="utf-8",
    )

    refs = code.references(root, "swap_positions_with_ally")
    assert refs["call_count"] == 0
    assert refs["ref_count"] == 1
    assert refs["refs"][0]["path"] == "scenes/fight.tscn"

    text = code.render(root, "swap_positions_with_ally")
    assert "1 reference(s)" in text
    assert "NEVER called" not in text


def test_callable_without_parens_counts_as_reference(tmp_path):
    """`pressed.connect(handler)` and `call("handler")` have no parentheses after the name."""
    root = _project(tmp_path)
    (root / "scripts" / "turn_manager.gd").write_text(
        "func _ready() -> void:\n"
        "\tbutton.pressed.connect(swap_positions_with_ally)\n"
        '\tcall("swap_positions_with_ally")\n',
        encoding="utf-8",
    )

    refs = code.references(root, "swap_positions_with_ally")
    assert refs["call_count"] == 0
    assert refs["ref_count"] == 2


def test_definition_and_comment_are_not_usage(tmp_path):
    root = _project(tmp_path)
    (root / "scripts" / "turn_manager.gd").write_text(
        "# swap_positions_with_ally wird hier nur erwähnt\n"
        "func swap_positions_with_ally(ally: Unit) -> bool:\n"
        "\treturn true\n",
        encoding="utf-8",
    )

    refs = code.references(root, "swap_positions_with_ally")
    assert refs["call_count"] == 0
    assert refs["ref_count"] == 0


# --------------------------------------------------------------------------
# Language parity: same patterns as `atlas init`
# --------------------------------------------------------------------------

def test_finds_python_function(tmp_path):
    root = tmp_path / "py"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "server.py").write_text(
        "def run_godot_headless(project: str, timeout: int = 30) -> dict:\n"
        "    return {}\n\n"
        "def caller():\n"
        "    run_godot_headless('x')\n",
        encoding="utf-8",
    )

    hits = code.find(root, "run_godot_headless")

    assert len(hits) == 1
    assert hits[0]["path"] == "backend/server.py"
    assert hits[0]["signature"] == "def run_godot_headless(project: str, timeout: int = 30) -> dict"


def test_finds_typescript_arrow_function(tmp_path):
    root = tmp_path / "ts"
    (root / "src").mkdir(parents=True)
    (root / "src" / "api.ts").write_text(
        "export const fetchTasks = async (limit: number) => {\n"
        "  return [];\n"
        "};\n",
        encoding="utf-8",
    )

    hits = code.find(root, "fetchTasks")

    assert len(hits) == 1
    assert hits[0]["symbol"] == "fetchTasks"


def test_python_definition_is_not_counted_as_reference(tmp_path):
    """The `def` line is not a usage — otherwise every symbol would pass the gate."""
    root = tmp_path / "py"
    (root / "mod").mkdir(parents=True)
    (root / "mod" / "lonely.py").write_text(
        "def never_called_anywhere():\n"
        "    return 1\n",
        encoding="utf-8",
    )

    result = code.references(root, "never_called_anywhere")

    assert result["call_count"] == 0
    assert result["ref_count"] == 0


def test_finds_all_nine_advertised_languages_and_checks_wiring(tmp_path):
    """Verify definition and caller/reference detection across all 9 supported languages."""
    root = tmp_path / "polyglot"
    src = root / "src"
    src.mkdir(parents=True)

    # 1. Python async def
    (src / "worker.py").write_text(
        "async def process_job_queue(batch: int) -> int:\n"
        "    return batch\n",
        encoding="utf-8",
    )
    # 2. Rust pub async fn + struct
    (src / "engine.rs").write_text(
        "pub struct DamageReport {\n"
        "    pub total: i32,\n"
        "}\n"
        "pub(crate) async fn compute_blast_radius<T>(power: i32) -> i32 {\n"
        "    // compute_blast_radius(0) in a comment must not count\n"
        "    power * 2\n"
        "}\n",
        encoding="utf-8",
    )
    # 3. Go receiver method + struct
    (src / "server.go").write_text(
        "package main\n"
        "type CombatServer struct {}\n"
        "func (s *CombatServer) DispatchTurn(id int) error {\n"
        "    return nil\n"
        "}\n",
        encoding="utf-8",
    )
    # 4. C# method + class
    (src / "UnitController.cs").write_text(
        "public class UnitController {\n"
        "    public async Task<int> ApplyArmorMitigation(int rawDamage) {\n"
        "        return rawDamage;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    # 5. Java method
    (src / "QuestService.java").write_text(
        "public class QuestService {\n"
        "    protected static boolean evaluateQuestStage(String id) {\n"
        "        return true;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    # 6. Kotlin suspend fun
    (src / "DialogueRunner.kt").write_text(
        "class DialogueRunner {\n"
        "    suspend fun resolveBranchCondition(nodeId: String): Boolean {\n"
        "        return true\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    # 7. Swift public func
    (src / "HudPresenter.swift").write_text(
        "public struct HudPresenter {\n"
        "    public mutating func refreshHealthBar(current: Int) {\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    expected_symbols = [
        "process_job_queue",
        "DamageReport",
        "compute_blast_radius",
        "CombatServer",
        "DispatchTurn",
        "UnitController",
        "ApplyArmorMitigation",
        "evaluateQuestStage",
        "resolveBranchCondition",
        "HudPresenter",
        "refreshHealthBar",
    ]
    for sym in expected_symbols:
        hits = code.find(root, sym, exact=True)
        assert len(hits) == 1, f"Expected to find {sym}, got {hits}"
        refs = code.references(root, sym)
        assert refs["call_count"] == 0, f"Expected 0 calls for unwired {sym}, got {refs}"
        if sym == "CombatServer":
            # Referenced as receiver `(s *CombatServer)` on DispatchTurn
            assert refs["ref_count"] == 1
        else:
            assert refs["ref_count"] == 0, f"Expected 0 refs for unwired {sym}, got {refs}"


def test_find_and_render_with_include_body(tmp_path):
    """`include_body=True` extracts the function body plus file context (header, enclosing class, sibling signatures)."""
    root = tmp_path / "pybody"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text(
        "MAX_BONUS = 50\n\n"
        "# Important invariant: a must be non-negative\n"
        "def helper_one(a: int) -> int:\n"
        "    step = a + 10\n"
        "    return step * 2\n\n"
        "def helper_two(b: int) -> int:\n"
        "    return b - 1\n",
        encoding="utf-8",
    )

    hits = code.find(root, "helper_one", include_body=True)
    assert len(hits) == 1
    assert hits[0]["line"] == 4
    assert hits[0]["context_start_line"] == 3
    assert hits[0]["end_line"] == 6
    assert "Important invariant" in hits[0]["body"]
    assert "step = a + 10" in hits[0]["body"]
    assert "return b - 1" not in hits[0]["body"]
    assert "MAX_BONUS = 50" in hits[0]["file_header"]
    assert any("helper_two" in s for s in hits[0]["siblings"])

    rendered = code.render(root, "helper_one", include_body=True)
    assert "src/calc.py:4-6" in rendered
    assert "step = a + 10" in rendered
    assert "MAX_BONUS = 50" in rendered
    assert "Sibling symbols in src/calc.py" in rendered
    assert "def helper_two(b: int) -> int" in rendered
    assert "return b - 1" not in rendered


def test_render_shows_guarded_by_wo_shield(tmp_path):
    """`code.render` displays a `🛡️ Guarded by WO <N>` warning when a symbol is monitored in `wo-symbols.yml`."""
    from atlas import verify
    from atlas.indexer import open_index

    root = tmp_path / "shield_proj"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "src" / "combat.py").write_text(
        "def resolve_combat_zone(x: int) -> int:\n    return x\n\n"
        "def tick() -> int:\n    return resolve_combat_zone(1)\n",
        encoding="utf-8",
    )
    (root / "docs" / "ARCHITECTURE.md").write_text(
        "# Architecture\n\n## 3. Work Orders\n\n"
        "Work Order 16 — ✅ DONE (Kampf-Auflösung & Zonen): Done.\n",
        encoding="utf-8",
    )
    conn = open_index(root)
    conn.close()
    verify.record_wo_symbols(root, 16, ["resolve_combat_zone"])

    rendered = code.render(root, "resolve_combat_zone")
    assert "🛡️ Guarded by WO 16 (Kampf-Auflösung & Zonen) — keep signature & callers wired" in rendered




