from atlas.chunker import chunk, parse_frontmatter


def test_frontmatter_is_parsed():
    meta, body, dropped = parse_frontmatter("---\nid: test\ntags: [a, b]\n---\n\n# Titel\nText")
    assert meta["id"] == "test"
    assert dropped == 4
    assert body.lstrip().startswith("# Titel")


def test_broken_frontmatter_does_not_crash():
    meta, body, dropped = parse_frontmatter("---\nid: [unbalanced\n---\n# X")
    assert isinstance(meta, dict)
    assert dropped >= 0


def test_heading_in_codeblock_is_ignored():
    text = "# Echt\n\n```\n# Falsch\n```\n\n## Zweit\nText"
    doc = chunk(text, "doc", "gdd/doc.md")
    headings = [s.heading for s in doc.sections]
    assert "Falsch" not in headings
    assert headings == ["Echt", "Zweit"]


def test_long_section_is_split_with_shared_base_anchor():
    body = "\n\n".join("Wort " * 200 for _ in range(6))
    doc = chunk(f"# Lang\n\n{body}", "doc", "gdd/doc.md")
    assert len(doc.sections) > 1
    assert doc.sections[0].anchor == "doc#lang"
    assert doc.sections[1].anchor == "doc#lang~2"
    assert all(s.heading == "Lang" for s in doc.sections)


def test_duplicate_headings_get_unique_anchors():
    doc = chunk("# A\nx\n# A\ny", "doc", "gdd/doc.md")
    anchors = [s.anchor for s in doc.sections]
    assert len(anchors) == len(set(anchors))


def test_breadcrumb_tracks_hierarchy():
    text = "# Top\nText\n## Mitte\nText\n### Tief\nText"
    doc = chunk(text, "doc", "gdd/doc.md")
    deep = [s for s in doc.sections if s.heading == "Tief"][0]
    assert deep.breadcrumb == "Top > Mitte"
