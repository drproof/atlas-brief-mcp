"""Query normalization for natural-language search queries (German and English)."""

from __future__ import annotations

import re
from pathlib import Path

_FOLD = str.maketrans({
    "ä": "a", "ö": "o", "ü": "u", "ß": "ss", "ẞ": "ss",
    "Ä": "a", "Ö": "o", "Ü": "u",
    "á": "a", "à": "a", "é": "e", "è": "e", "í": "i", "ó": "o", "ú": "u",
})

# SQLite FTS5 `unicode61 remove_diacritics 2` strips umlauts (`ä -> a`) but keeps
# literal `ß` (`"Größe" -> "große"`). Retain `ß` as an additional search variant.
_FOLD_FTS_ESZETT = str.maketrans({
    "ä": "a", "ö": "o", "ü": "u", "ẞ": "ß",
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


TOKEN_RE = re.compile(r"[A-Za-z0-9ÄÖÜäöüßẞ_]+(?:-[A-Za-z0-9ÄÖÜäöüßẞ_]+)*")
_CAMEL_BOUNDARY_1 = re.compile(r"([a-z0-9äöüß])([A-ZÄÖÜ])")
_CAMEL_BOUNDARY_2 = re.compile(r"([A-ZÄÖÜ]+)([A-ZÄÖÜ][a-zäöüß])")

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
    "im", "am", "auf", "aus", "bei", "mit", "nach", "von", "vom", "zu", "zum",
    "zur", "für", "fuer", "fur", "über", "ueber", "uber", "unter", "vor", "durch", "gegen", "ohne", "um",
    "welche", "welcher", "welches", "welchem", "welchen",
    "datei", "dateien", "dokument", "dokumente", "info", "infos", "information", "informationen",
    "steht", "stehen", "befindet", "befinden", "liegt", "liegen", "geregelt", "beschrieben",
    "mir", "mich", "mein", "meine", "bitte", "mal", "denn", "doch", "kann", "kannst",
    "soll", "sollte", "muss", "müssen", "mussen", "hier", "dort", "dazu", "davon",
}

# German-only function words that disambiguate bilingual homographs ("war", "die"):
# in "Wo sind die Hitpoints?" -> "die" is a German article (drop);
# in "Where is fog of war?" or "die roll" -> "war"/"die" are English GDD nouns (keep).
_GERMAN_CONTEXT_WORDS = {
    "wo", "wie", "wann", "warum", "ist", "sind", "waren", "wird", "werden",
    "das", "der", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer",
    "und", "oder", "für", "fuer", "fur", "über", "ueber", "uber", "mit", "bei",
    "nach", "vom", "zum", "zur", "welche", "welcher", "welches", "steht", "stehen",
    "finde", "findet", "finden", "beschrieben", "geregelt",
}
_BILINGUAL_GDD_NOUNS = {"war", "die", "hat", "man"}
_ENGLISH_DIE_NEIGHBORS = {"roll", "rolls", "pool", "pools", "hit", "damage"}
_ENGLISH_MAN_HAT_NEIGHBORS = {"wizard", "iron", "last", "standing", "magic", "mage"}


def fold(text: str) -> str:
    return text.translate(_FOLD).lower()


def fold_fts(text: str) -> str:
    """Fold diacritics like SQLite FTS5 `unicode61 remove_diacritics 2` (retaining `ß`)."""
    return text.translate(_FOLD_FTS_ESZETT).lower()


def _split_camel(raw_token: str) -> list[str]:
    spaced = _CAMEL_BOUNDARY_2.sub(r"\1 \2", _CAMEL_BOUNDARY_1.sub(r"\1 \2", raw_token))
    parts = spaced.split()
    return parts if len(parts) > 1 else []


def terms(query: str) -> list[str]:
    """Return search terms with stop words removed, folded, deduplicated, longest first."""
    raw_tokens = TOKEN_RE.findall(query or "")
    lowered_raw = {t.strip("-_").casefold() for t in raw_tokens if t.strip("-_")}
    is_german = bool(lowered_raw & _GERMAN_CONTEXT_WORDS)

    keep: list[str] = []
    seen: set[str] = set()

    def _add(cand: str) -> None:
        if len(cand) >= 2 and cand not in seen:
            seen.add(cand)
            keep.append(cand)

    def _is_stop(tok: str, idx: int = -1, raw_tok_str: str = "") -> bool:
        if tok in _BILINGUAL_GDD_NOUNS:
            if not is_german:
                return False
            prev_low = raw_tokens[idx - 1].strip("-_").casefold() if idx > 0 else ""
            next_low = (
                raw_tokens[idx + 1].strip("-_").casefold()
                if 0 <= idx + 1 < len(raw_tokens)
                else ""
            )
            if tok == "war" and (prev_low == "of" or (idx > 0 and raw_tok_str[:1].isupper())):
                return False
            if tok == "die" and (
                prev_low in _ENGLISH_DIE_NEIGHBORS or next_low in _ENGLISH_DIE_NEIGHBORS
            ):
                return False
            if tok in {"hat", "man"} and (
                prev_low in _ENGLISH_MAN_HAT_NEIGHBORS
                or next_low in _ENGLISH_MAN_HAT_NEIGHBORS
                or (idx > 0 and raw_tok_str[:1].isupper())
            ):
                return False
        return tok in STOPWORDS

    for idx, raw_tok in enumerate(raw_tokens):
        stripped_raw = raw_tok.strip("-_")
        if len(stripped_raw) < 2:
            continue
        token = fold(stripped_raw)
        if len(token) < 2 or _is_stop(token, idx, stripped_raw):
            continue

        candidates: list[str] = [token]
        fts_tok = fold_fts(stripped_raw)
        if fts_tok != token:
            candidates.append(fts_tok)

        if "-" in stripped_raw:
            closed_raw = stripped_raw.replace("-", "").replace("_", "")
            if len(closed_raw) >= 6:
                closed_fold = fold(closed_raw)
                candidates.append(closed_fold)
                closed_fts = fold_fts(closed_raw)
                if closed_fts != closed_fold:
                    candidates.append(closed_fts)
            for raw_part in stripped_raw.split("-"):
                raw_part = raw_part.strip("_")
                if not raw_part:
                    continue
                folded_part = fold(raw_part)
                if len(folded_part) >= 2 and not _is_stop(folded_part, idx, raw_part):
                    candidates.append(folded_part)
                    fts_part = fold_fts(raw_part)
                    if fts_part != folded_part:
                        candidates.append(fts_part)
                for camel_sub in _split_camel(raw_part):
                    sub_folded = fold(camel_sub)
                    if len(sub_folded) >= 2 and not _is_stop(sub_folded, idx, camel_sub):
                        candidates.append(sub_folded)
        else:
            for camel_sub in _split_camel(stripped_raw):
                sub_folded = fold(camel_sub)
                if len(sub_folded) >= 2 and not _is_stop(sub_folded, idx, camel_sub):
                    candidates.append(sub_folded)

        for candidate in candidates:
            _add(candidate)

        # Project synonyms: 'hitpoints' should also match 'max_hp'.
        for alias in _ALIASES.get(token, []):
            _add(alias)

    if not keep:
        for raw_tok in raw_tokens:
            fallback = fold(raw_tok.strip("-_"))
            if len(fallback) >= 3:
                _add(fallback)

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
