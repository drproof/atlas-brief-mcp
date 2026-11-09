"""Project root resolution and file discovery."""

from __future__ import annotations

import os
from pathlib import Path

CACHE_DIR = ".atlas"
# Projects created before the rename use `.gdd/`. Continue recognizing them —
# a tool that loses its users' indexes on a rename is a migration headache,
# not a tool.
LEGACY_CACHE_DIR = ".gdd"
DOC_DIRS = ("docs", "specs", "atlas", "gdd")
DEFAULT_EXCLUDES = ("_legacy_archive", "_archive", "node_modules", ".git", ".godot", ".venv", "art_source")


def cache_dir(root: Path) -> Path:
    """Return the config directory: `.atlas/`, else legacy `.gdd/`, else the new path."""
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
    except RuntimeError:  # home directory cannot be determined (exotic environments)
        return Path("/")


def find_root(explicit: str | None = None) -> Path:
    """Determine the project root.

    Order: explicit path > ATLAS_ROOT/GDD_ROOT > upward search for
    .atlas/ or .gdd/ > docs/specs > cwd.

    **The home directory is never a project.** That is where the global config
    folder `~/.atlas` (or `~/.gdd` with token and client secret) lives — accepting
    it as a marker turns `C:\\Users\\<name>` into a project and scans the entire
    user directory on `atlas init`. The upward search therefore stops at home,
    not at the filesystem root.
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
            break  # no project exists above the home directory
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
    """Return documentation directories: docs/, specs/, atlas/, gdd/ — plus the sync cache.

    The cache lives under `.atlas/cache` (or `.gdd/cache` in legacy projects)
    and is only read, never written to directly.
    """
    found = [root / d for d in DOC_DIRS if (root / d).is_dir()]
    for cdir in (root / CACHE_DIR / "cache", root / LEGACY_CACHE_DIR / "cache"):
        if cdir.is_dir() and cdir not in found:
            found.append(cdir)
    return found or [root]


def discover(root: Path, excludes: tuple[str, ...] = DEFAULT_EXCLUDES) -> list[Path]:
    """Return all Markdown files in the documentation directories, excluding ignored patterns."""
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
