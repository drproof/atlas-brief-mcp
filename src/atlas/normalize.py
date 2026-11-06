"""Query normalization for natural-language search queries (German and English)."""

from __future__ import annotations

import re
from pathlib import Path

_FOLD = str.maketrans({
    "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
    "Ä": "a", "Ö": "o", "Ü": "u",
    "á": "a", "à": "a", "é": "e", "è": "e", "í": "i", "ó": "o", "ú": "u",
})

# Project aliases from .atlas/aliases.yml: what the agent types -> what the GDD calls it.
_ALIASES: dict[str, list[str]] = {}
_ALIAS_MTIME: float = -1.0


def load_aliases(path) -> int:
    """Load `.atlas/aliases.yml` (key: search term, value: list of target terms).

    The file belongs in the project, not the tool: only the project's GDD knows
    which synonyms make sense. Reloaded when modified (mtime check).
    """
    global _ALIASES, _ALIAS_MTIME
    path = Path(path)
    if not path.exists():
        _ALIASES = {}
        _ALIAS_MTIME = -1.0
        return 0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return len(_ALIASES)
    if mtime == _ALIAS_MTIME:
        return len(_ALIASES)

    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return len(_ALIASES)

    mapping = data.get("aliases") if isinstance(data, dict) else None
    loaded: dict[str, list[str]] = {}
    if isinstance(mapping, dict):
        for key, value in mapping.items():
            key_text = fold(str(key)).strip()
            if not key_text:
                continue
            if isinstance(value, str):
                targets = [value]
            elif isinstance(value, (list, tuple)):
                targets = [str(item) for item in value]
            else:
                continue
            cleaned = [fold(item).strip() for item in targets if str(item).strip()]
            if cleaned:
                loaded[key_text] = cleaned

    _ALIASES = loaded
    _ALIAS_MTIME = mtime
    return len(loaded)


def aliases() -> dict[str, list[str]]:
    return _ALIASES

TOKEN_RE = re.compile(r"[A-Za-z0-9ÄÖÜäöüß_]+(?:-[A-Za-z0-9]+)*")

# Question and filler words that are virtually useless as GDD search terms.
# German **and** English: agents often ask in English ("where is the combat
# system described?"), which would otherwise leave `where`, `the`, `described`
# as search terms and dilute BM25 ranking.
STOPWORDS = {
    # English
    "where", "what", "when", "which", "who", "whom", "whose", "why", "how",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "in", "of", "to", "for", "with", "on", "at", "by", "from", "into",
    "and", "or", "but", "not", "no", "does", "do", "did", "can", "could",
    "find", "locate", "located", "defined", "described", "describe",
    "documented", "docs", "document", "section", "please", "show", "tell",
    "there", "it", "its", "this", "that", "these", "those", "about",
    "wo", "finde", "findet", "finden", "ich", "du", "man", "es", "sich",
    "das", "der", "die", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer",
    "und", "oder", "aber", "auch", "noch", "nur", "so", "wie", "was", "wer", "wann", "warum",
    "ist", "sind", "war", "waren", "wird", "werden", "hat", "haben", "gibt", "geben",
    "in", "im", "an", "am", "auf", "aus", "bei", "mit", "nach", "von", "vom", "zu", "zum",
    "zur", "für", "fuer", "über", "ueber", "unter", "vor", "durch", "gegen", "ohne", "um",
    "welche", "welcher", "welches", "welchem", "welchen",
    "datei", "dateien", "dokument", "dokumente", "info", "infos", "information", "informationen",
    "steht", "stehen", "befindet", "befinden", "liegt", "liegen", "geregelt", "beschrieben",
    "mir", "mich", "mein", "meine", "bitte", "mal", "denn", "doch", "kann", "kannst",
    "soll", "sollte", "muss", "müssen", "hier", "dort", "dazu", "davon",
}


def fold(text: str) -> str:
    return text.translate(_FOLD).lower()


def terms(query: str) -> list[str]:
    """Return search terms with stop words removed, folded, deduplicated, longest first."""
    folded = fold(query)
    raw = TOKEN_RE.findall(folded)
    keep: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = token.strip("-_")
        if len(token) < 2:
            continue
        if token in STOPWORDS:
            continue
        # 'Gewölbestürmer-Perk' is a single token and never appears verbatim in a GDD.
        # Also include its hyphen-split parts so the hyphen does not block matching.
        candidates = [token]
        if "-" in token:
            candidates.extend(
                part for part in token.split("-")
                if len(part) >= 2 and part not in STOPWORDS
            )
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            keep.append(candidate)

        # Project synonyms: 'hitpoints' should also match 'max_hp'.
        for alias in _ALIASES.get(token, []):
            if len(alias) >= 2 and alias not in seen:
                seen.add(alias)
                keep.append(alias)
    keep.sort(key=len, reverse=True)
    return keep


def build_match(query: str) -> str | None:
    """Build an FTS5 MATCH expression: each term as a prefix, joined with OR.

    OR instead of AND because BM25 already ranks documents matching more terms
    higher — AND would too often return zero results on natural-language questions.
    """
    found = terms(query)
    if not found:
        return None
    clauses = []
    for token in found:
        safe = token.replace('"', "")
        clauses.append(f'"{safe}"*')
    return " OR ".join(clauses)
