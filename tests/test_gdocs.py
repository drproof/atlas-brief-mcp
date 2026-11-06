"""Offline query building — regression test for the Drive filter bug."""

from atlas.gdocs import build_query


def test_mime_types_are_parenthesized():
    """Without parentheses, 'name contains' applies only to the second MIME type
    and the query returns ALL Google Docs of the account (happened on 2026-11-04)."""
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
