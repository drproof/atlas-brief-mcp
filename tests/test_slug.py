from atlas.slug import slugify


def test_basic_slug():
    assert slugify("Akt 1: Die Kryo-Bucht") == "akt-1-die-kryo-bucht"


def test_umlauts_fold():
    assert slugify("Kämpfer & Öl") == "kaempfer-oel"
    assert slugify("Straße") == "strasse"


def test_length_cut_at_word_boundary():
    long_title = "Zweites Kapitel Dramaturgiestruktur Die Fünf Akte und mehr"
    slug = slugify(long_title, max_len=40)
    assert len(slug) <= 40
    assert not slug.endswith("-")


def test_empty_falls_back():
    assert slugify("!!! ???") == "section"
