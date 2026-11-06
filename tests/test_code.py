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
