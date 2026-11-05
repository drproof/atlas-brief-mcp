"""Query-Normalisierung: Ein Bindestrich darf die Suche nicht blockieren.

Praxis-Fall: Das GDD schreibt „Gewölbe-Stürmer", der Agent fragt nach
„Gewölbestürmer-Perk". Der FTS5-Tokenizer zerlegt den Dokumenttext am
Bindestrich in zwei Wörter — die Query mit Bindestrich trifft dann nie.
"""

from __future__ import annotations

from atlas.normalize import build_match, terms


def test_hyphenated_query_keeps_whole_term_and_parts():
    result = terms("Wo ist der Gewölbestürmer-Perk?")
    assert "gewolbesturmer-perk" in result
    assert "gewolbesturmer" in result
    assert "perk" in result


def test_hyphenated_query_produces_or_clauses():
    match = build_match("AP-Farben")
    assert '"ap-farben"*' in match
    assert '"farben"*' in match
    assert '"ap"*' in match


def test_plain_query_stays_single_term():
    assert terms("Kampfsystem") == ["kampfsystem"]


def test_stopwords_still_dropped():
    result = terms("Wo ist das Kampfsystem?")
    assert result == ["kampfsystem"]


# --------------------------------------------------------------------------
# Projekt-Aliase (.atlas/aliases.yml)
# --------------------------------------------------------------------------

import pytest

from atlas import normalize


@pytest.fixture(autouse=True)
def _reset_aliases():
    yield
    normalize._ALIASES = {}
    normalize._ALIAS_MTIME = -1.0


def test_aliases_expand_query_terms(tmp_path):
    path = tmp_path / "aliases.yml"
    path.write_text("aliases:\n  hitpoints: [max_hp]\n  hp: [max_hp, lebenspunkte]\n",
                    encoding="utf-8")
    assert normalize.load_aliases(path) == 2

    result = terms("Wo sind die Hitpoints?")
    assert "hitpoints" in result
    assert "max_hp" in result


def test_aliases_are_optional(tmp_path):
    assert normalize.load_aliases(tmp_path / "fehlt.yml") == 0
    assert terms("Hitpoints") == ["hitpoints"]


def test_alias_file_is_reread_on_change(tmp_path):
    path = tmp_path / "aliases.yml"
    path.write_text("aliases:\n  hp: [max_hp]\n", encoding="utf-8")
    assert normalize.load_aliases(path) == 1

    path.write_text("aliases:\n  hp: [max_hp]\n  geld: [gold_pouch]\n", encoding="utf-8")
    import os
    os.utime(path, (0, 0))  # mtime erzwingen, sonst greift der Cache
    assert normalize.load_aliases(path) == 2
