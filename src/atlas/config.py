"""Root-Auflösung und Datei-Discovery."""

from __future__ import annotations

import os
from pathlib import Path

CACHE_DIR = ".atlas"
# Projekte, die vor der Umbenennung angelegt wurden, haben `.gdd/`. Sie werden
# weiter erkannt — ein Werkzeug, das beim Umbenennen die Indizes seiner Nutzer
# verliert, ist kein Werkzeug, sondern ein Umzug.
LEGACY_CACHE_DIR = ".gdd"
DOC_DIRS = ("docs", "specs", "atlas", "gdd")
DEFAULT_EXCLUDES = ("_legacy_archive", "_archive", "node_modules", ".git", ".godot", ".venv", "art_source")


def cache_dir(root: Path) -> Path:
    """Der Konfigurationsordner: `.atlas/`, sonst das alte `.gdd/`, sonst der neue Pfad."""
    primary = root / CACHE_DIR
    if primary.is_dir():
        return primary
    legacy = root / LEGACY_CACHE_DIR
    if legacy.is_dir():
        return legacy
    return primary


def _home() -> Path:
    try:
        return Path.home().resolve()
    except RuntimeError:  # kein Home ermittelbar (exotische Umgebungen)
        return Path("/")


def find_root(explicit: str | None = None) -> Path:
    """Bestimmt das Projekt-Root.

    Reihenfolge: expliziter Pfad > ATLAS_ROOT/GDD_ROOT > Aufwärtssuche nach
    .atlas/ bzw. .gdd/ > docs/specs > cwd.

    **Das Home-Verzeichnis ist nie ein Projekt.** Dort liegt der globale
    Konfig-Ordner `~/.atlas` (bzw. `~/.gdd` mit Token und Client-Secret) — wer
    ihn als Marker akzeptiert, erklärt `C:\\Users\\<name>` zum Projekt und
    scannt bei `atlas init` das gesamte Benutzerverzeichnis. Die Suche endet
    deshalb am Home, nicht erst an der Laufwerkswurzel.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("ATLAS_ROOT") or os.environ.get("GDD_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    cwd = Path.cwd().resolve()
    home = _home()
    for candidate in (cwd, *cwd.parents):
        if candidate == home:
            break  # oberhalb des Homes gibt es kein Projekt
        if (candidate / CACHE_DIR).is_dir() or (candidate / LEGACY_CACHE_DIR).is_dir():
            return candidate
        if (candidate / "gdd").is_dir():
            return candidate
        if (candidate / "specs").is_dir() and any((candidate / "specs").glob("*.md")):
            return candidate
    return cwd


def index_path(root: Path) -> Path:
    return cache_dir(root) / "index.sqlite"


def doc_dirs(root: Path) -> list[Path]:
    """Doku-Ordner: docs/, specs/, atlas/, gdd/ — plus der Sync-Cache.

    Der Cache liegt unter `.atlas/cache` (oder `.gdd/cache` bei Altzprojekten)
    und wird nur gelesen, nie geschrieben.
    """
    found = [root / d for d in DOC_DIRS if (root / d).is_dir()]
    cache = cache_dir(root) / "cache"
    if cache.is_dir():
        found.append(cache)
    return found or [root]


def discover(root: Path, excludes: tuple[str, ...] = DEFAULT_EXCLUDES) -> list[Path]:
    """Alle Markdown-Dateien in den Doku-Ordnern, ohne Ausschlussmuster."""
    files: list[Path] = []
    for base in doc_dirs(root):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                d for d in dirnames
                if d not in excludes and not d.startswith(".")
            ]
            for name in filenames:
                if name.lower().endswith((".md", ".markdown")):
                    files.append(Path(dirpath) / name)
    return sorted(set(files))
