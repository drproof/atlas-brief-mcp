"""Lint scope: only files managed by atlas determine the exit code."""

from __future__ import annotations

import json

from atlas import bootstrap, indexer
from atlas import lint as lint_mod

DUPLICATE_DOC = """# Alt-Doku

## Plot-Zusammenfassung

Alter Text.

## Plot-Zusammenfassung

Nochmal derselbe Abschnitt — Altbestand, nicht von atlas erzeugt.
"""


def _repo(tmp_path):
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "core").mkdir()
    (root / "core" / "engine.gd").write_text(
        "func start() -> void:\n\tpass\n", encoding="utf-8")
    (root / "docs" / "ALT.md").write_text(DUPLICATE_DOC, encoding="utf-8")
    return root


def test_without_managed_list_everything_counts(tmp_path):
    """Backwards-compatible: no managed.json = everything is managed = exit 1."""
    root = _repo(tmp_path)
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)

    data = json.loads(lint_mod.lint(conn))

    assert data["ok"] is False
    assert data["managed_issues"] > 0
    assert data["discovered_issues"] == 0
    assert all(issue["scope"] == "managed" for issue in data["issues"])


def test_managed_list_downgrades_foreign_docs(tmp_path):
    """With managed.json, legacy docs count as a hint rather than an error."""
    root = _repo(tmp_path)
    bootstrap.init_project(root)
    conn = indexer.open_index(root)
    indexer.refresh(conn, root, force=True)

    managed = bootstrap.load_managed(root)
    assert managed == {"docs/ARCHITECTURE.md"}

    data = json.loads(lint_mod.lint(conn, managed=managed))

    assert data["managed_issues"] == 0, data["issues"]
    assert data["discovered_issues"] >= 1
    assert data["ok"] is True
    assert any(issue["scope"] == "discovered" for issue in data["issues"])


def test_managed_json_uses_posix_relative_paths(tmp_path):
    root = _repo(tmp_path)
    bootstrap.init_project(root, out="docs/ARCHITEKTUR.md")

    payload = json.loads(bootstrap.managed_path(root).read_text(encoding="utf-8"))

    assert payload["paths"] == ["docs/ARCHITEKTUR.md"]
    assert "\\" not in json.dumps(payload["paths"])


def test_managed_list_accumulates_across_runs(tmp_path):
    """A second run with a different --out preserves the first path."""
    root = _repo(tmp_path)
    bootstrap.init_project(root, out="docs/A.md")
    bootstrap.init_project(root, out="docs/B.md")

    assert bootstrap.load_managed(root) == {"docs/A.md", "docs/B.md"}


def test_missing_managed_file_is_none_not_empty(tmp_path):
    """None (no managed concept) and set() (nothing managed) are not the same."""
    root = _repo(tmp_path)
    assert bootstrap.load_managed(root) is None
