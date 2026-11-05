"""Anbindungs-Nachweis: definiert *und* benutzt — sonst keine Abnahme."""

from __future__ import annotations

import pytest

from atlas import publish, verify


def _project(tmp_path, body: str, caller: str = "") -> object:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "unit.gd").write_text(body, encoding="utf-8")
    if caller:
        (scripts / "turn_manager.gd").write_text(caller, encoding="utf-8")
    return tmp_path


def _wo_symbols(root, text: str) -> None:
    atlas_dir = root / ".atlas"
    atlas_dir.mkdir(exist_ok=True)
    (atlas_dir / "wo-symbols.yml").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# check()
# --------------------------------------------------------------------------

def test_bound_symbol_passes(tmp_path):
    root = _project(
        tmp_path,
        "func dispatch_skill(id: String) -> void:\n\tpass\n",
        "func turn() -> void:\n\tdispatch_skill('x')\n",
    )
    result = verify.check(root, ["dispatch_skill"])

    assert result["ok"] is True
    assert result["symbols"][0]["calls"] == 1
    assert result["symbols"][0]["definition"] == "scripts/unit.gd:1"


def test_defined_but_unused_fails(tmp_path):
    root = _project(tmp_path, "func dispatch_skill(id: String) -> void:\n\tpass\n")
    result = verify.check(root, ["dispatch_skill"])

    assert result["ok"] is False
    assert "nirgends aufgerufen" in result["failed"][0]["reason"]
    assert result["failed"][0]["defined"] is True


def test_missing_symbol_fails_with_other_reason(tmp_path):
    root = _project(tmp_path, "func other() -> void:\n\tpass\n")
    result = verify.check(root, ["dispatch_skill"])

    assert result["ok"] is False
    assert result["failed"][0]["reason"] == "nicht definiert"


def test_signal_binding_satisfies_check(tmp_path):
    """Ein Signal-Handler ohne Klammer-Aufruf ist angebunden, nicht tot."""
    root = _project(
        tmp_path,
        "func _on_skill() -> void:\n\tpass\n",
        "func _ready() -> void:\n\tbutton.pressed.connect(_on_skill)\n",
    )
    result = verify.check(root, ["_on_skill"])

    assert result["ok"] is True
    assert result["symbols"][0]["calls"] == 0
    assert result["symbols"][0]["refs"] == 1


def test_substring_is_not_accepted_as_definition(tmp_path):
    """`vault` darf nicht durch `can_vault_charger_ram` bestätigt werden."""
    root = _project(tmp_path, "func can_vault_charger_ram() -> bool:\n\treturn true\n")
    result = verify.check(root, ["vault"])

    assert result["ok"] is False
    assert result["failed"][0]["reason"] == "nicht definiert"


def test_refusal_names_the_symbol_and_the_line(tmp_path):
    root = _project(tmp_path, "func dispatch_skill(id: String) -> void:\n\tpass\n")
    result = verify.check(root, ["dispatch_skill"])
    text = verify.refusal(result)

    assert "Abnahme verweigert" in text
    assert "dispatch_skill" in text
    assert "scripts/unit.gd:1" in text
    assert "WO 8" in text


# --------------------------------------------------------------------------
# Gate in publish / set_status
# --------------------------------------------------------------------------

def test_publish_refuses_before_touching_the_network(tmp_path):
    """Das Gate läuft vor `load_credentials` — kein Token, kein Netz, kein Write."""
    root = _project(tmp_path, "func dispatch_skill(id: String) -> void:\n\tpass\n")

    result = publish.publish(root, None, 8, status="done",
                             title="Skills", verify_symbols=["dispatch_skill"])

    assert result["written"] is False
    assert result["refused"] is True
    assert "Abnahme verweigert" in result["refusal"]
    # Kein Google-Zugriff versucht: kein document_id im Payload.
    assert "document_id" not in result


def test_open_status_skips_the_gate(tmp_path):
    """Eine offene Work Order darf unfertig sein — sie wird nicht geprüft."""
    root = _project(tmp_path, "func dispatch_skill(id: String) -> void:\n\tpass\n")
    gate, refusal = publish._attachment_gate(root, 8, "open", ["dispatch_skill"], False)

    assert gate == {}
    assert refusal == ""


def test_gate_uses_wo_symbols_file(tmp_path):
    """Hinterlegte Symbole werden auch ohne explizite Angabe geprüft."""
    root = _project(tmp_path, "func dispatch_skill(id: String) -> void:\n\tpass\n")
    _wo_symbols(root, "8:\n  - dispatch_skill\n")

    gate, refusal = publish._attachment_gate(root, 8, "done", None, False)

    assert refusal.startswith("Abnahme verweigert")
    assert gate["verify"]["failed"][0]["symbol"] == "dispatch_skill"


def test_gate_warns_when_no_symbols_are_known(tmp_path):
    """Ohne hinterlegte Symbole wird nicht blockiert — aber auch nicht geschwiegen."""
    root = _project(tmp_path, "func other() -> void:\n\tpass\n")
    gate, refusal = publish._attachment_gate(root, 99, "done", None, False)

    assert refusal == ""
    assert gate["verify"]["checked"] == 0
    assert "Anbindungsnachweis" in gate["verify"]["warning"]


def test_dry_run_reports_but_does_not_refuse(tmp_path):
    root = _project(tmp_path, "func dispatch_skill(id: String) -> void:\n\tpass\n")

    gate, refusal = publish._attachment_gate(root, 8, "done", ["dispatch_skill"], True)

    assert refusal == ""
    assert gate["verify"]["ok"] is False


# --------------------------------------------------------------------------
# Regressions-Wächter über erledigte Work Orders
# --------------------------------------------------------------------------

def test_regression_found_for_done_work_order(tmp_path):
    root = _project(tmp_path, "func other() -> void:\n\tpass\n")
    _wo_symbols(root, "8:\n  - dispatch_skill\n")
    work_orders = [{"wo": 8, "status": "done", "title": "Skills"}]

    findings = verify.regressions(root, work_orders)

    assert len(findings) == 1
    assert findings[0]["wo"] == 8
    assert findings[0]["symbol"] == "dispatch_skill"
    assert "nicht definiert" in findings[0]["reason"]


def test_open_work_order_is_not_checked(tmp_path):
    root = _project(tmp_path, "func other() -> void:\n\tpass\n")
    _wo_symbols(root, "13:\n  - dispatch_skill\n")

    assert verify.regressions(root, [{"wo": 13, "status": "open", "title": "Neu"}]) == []


# --------------------------------------------------------------------------
# publish ist idempotent: keine zweite Zeile für dieselbe Work Order
# --------------------------------------------------------------------------

def test_publish_skips_duplicate_with_same_status(tmp_path, monkeypatch):
    """Der Bug vom 05.11.: vier WOs standen doppelt, `status` nahm den ersten."""
    import json

    from atlas import status as status_mod

    root = _project(tmp_path, "func other() -> void:\n\tpass\n")
    monkeypatch.setattr(status_mod, "extract_work_orders", lambda conn: [
        {"wo": 14, "status": "open", "title": "SkillExecutor"}
    ])

    result = publish.publish(root, None, 14, status="open", title="SkillExecutor")

    assert result["written"] is False
    assert result["unchanged"] is True
    assert "nicht doppelt" in result["message"]


def test_publish_switches_status_instead_of_appending(tmp_path, monkeypatch):
    from atlas import status as status_mod

    root = _project(tmp_path, "func other() -> void:\n\tpass\n")
    monkeypatch.setattr(status_mod, "extract_work_orders", lambda conn: [
        {"wo": 14, "status": "open", "title": "SkillExecutor"}
    ])
    calls = []

    def fake_set_status(*args, **kwargs):
        calls.append(kwargs)
        return {"changed": True, "verified": True, "old": "open", "new": "done"}

    monkeypatch.setattr(publish, "set_status", fake_set_status)

    result = publish.publish(root, None, 14, status="done", title="SkillExecutor")

    assert calls, "set_status muss gerufen werden statt einer zweiten Zeile"
    assert result["reused_set_status"] is True
    assert result["changed"] is True


def test_short_symbol_survives_substring_noise(tmp_path):
    """`vault` ist definiert, auch wenn drei `can_vault_*` davor liegen."""
    root = _project(tmp_path, (
        "func can_vault_a() -> void:\n\tpass\n\n"
        "func can_vault_b() -> void:\n\tpass\n\n"
        "func can_vault_c() -> void:\n\tpass\n\n"
        "func vault() -> void:\n\tpass\n\n"
        "func use_it() -> void:\n\tvault()\n"
    ))

    result = verify.check(root, ["vault"])

    assert result["ok"] is True, result
    assert result["symbols"][0]["defined"] is True


def test_dead_class_does_not_pass_the_gate(tmp_path):
    """`class_name Foo` deklariert die Klasse, es benutzt sie nicht."""
    root = _project(tmp_path, "class_name LonelyClass\n\nextends Node\n")

    result = verify.check(root, ["LonelyClass"])

    assert result["symbols"][0]["calls"] == 0
    assert result["symbols"][0]["refs"] == 0
    assert result["ok"] is False
