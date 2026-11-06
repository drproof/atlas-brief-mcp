"""Anchor slugs: stable, ASCII, collision-free."""

from __future__ import annotations

import re

_FOLD = {
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
    "á": "a", "à": "a", "â": "a", "é": "e", "è": "e", "ê": "e",
    "í": "i", "ì": "i", "î": "i", "ó": "o", "ò": "o", "ô": "o",
    "ú": "u", "ù": "u", "û": "u", "ñ": "n", "ç": "c",
}


def slugify(text: str, max_len: int = 64) -> str:
    """'Akt 1: Die Kryo-Bucht' -> 'akt-1-die-kryo-bucht'."""
    folded = "".join(_FOLD.get(ch, ch) for ch in text.strip())
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug.rfind("-", 0, max_len)
        slug = slug[:cut] if cut > max_len // 2 else slug[:max_len]
        slug = slug.rstrip("-")
    return slug or "section"
