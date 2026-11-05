"""Query-Normalisierung für deutsche Freitext-Fragen."""

from __future__ import annotations

import re
from pathlib import Path

_FOLD = str.maketrans({
    "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
    "Ä": "a", "Ö": "o", "Ü": "u",
    "á": "a", "à": "a", "é": "e", "è": "e", "í": "i", "ó": "o", "ú": "u",
})

# Projekt-Aliase aus .atlas/aliases.yml: was der Agent tippt -> was im GDD steht.
_ALIASES: dict[str, list[str]] = {}
_ALIAS_MTIME: float = -1.0


def load_aliases(path) -> int:
    """Lädt `.atlas/aliases.yml` (Schlüssel: Suchbegriff, Wert: Liste von Zielen).

    Die Datei gehört ins Projekt, nicht ins Tool: welche Synonyme sinnvoll sind,
    weiß nur das jeweilige GDD. Wird bei Änderung neu gelesen (mtime-Vergleich).
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

# Frage- und Füllwörter, die in GDDs praktisch nie als Suchbegriff taugen.
STOPWORDS = {
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
    """Suchbegriffe ohne Stoppwörter, gefaltet, dedupliziert, längste zuerst."""
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
        # 'Gewölbestürmer-Perk' ist EIN Token und steht so in keinem GDD.
        # Die Einzelteile werden zusätzlich aufgenommen, damit der Bindestrich
        # die Suche nicht komplett blockiert.
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

        # Projekt-Synonyme: 'hitpoints' soll 'max_hp' mitfinden.
        for alias in _ALIASES.get(token, []):
            if len(alias) >= 2 and alias not in seen:
                seen.add(alias)
                keep.append(alias)
    keep.sort(key=len, reverse=True)
    return keep


def build_match(query: str) -> str | None:
    """Baut einen FTS5-MATCH-Ausdruck: jeder Term als Präfix, OR-verknüpft.

    OR statt AND, weil BM25 Dokumente mit mehr Treffern ohnehin besser rankt —
    AND würde bei natürlichen Fragen zu oft leer ausgehen.
    """
    found = terms(query)
    if not found:
        return None
    clauses = []
    for token in found:
        safe = token.replace('"', "")
        clauses.append(f'"{safe}"*')
    return " OR ".join(clauses)
