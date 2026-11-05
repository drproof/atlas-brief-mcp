"""Query-Bau ohne Netzwerk — Regressionstest für den Drive-Filter-Bug."""

from atlas.gdocs import build_query


def test_mime_types_are_parenthesized():
    """Ohne Klammern gilt 'name contains' nur für den zweiten Mime-Typ und die
    Query liefert ALLE Google-Docs des Kontos (real passiert am 04.11.2026)."""
    query = build_query("MYPROJECT")
    assert query.startswith("(mimeType")
    assert "or mimeType" in query.split(")")[0]
    assert ") and trashed = false and name contains 'MYPROJECT'" in query


def test_without_filter_no_name_clause():
    query = build_query(None)
    assert "name contains" not in query
    assert query.count("(") == query.count(")")


def test_single_quote_is_escaped():
    query = build_query("Projekt's GDD")
    assert r"Projekt\'s GDD" in query
