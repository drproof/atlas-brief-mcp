from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from atlas.indexer import open_index

FIXTURES = Path(__file__).parent / "fixtures" / "gdd"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "proj"
    docs = target / "gdd"
    docs.mkdir(parents=True)
    for source in FIXTURES.glob("*.md"):
        shutil.copy(source, docs / source.name)

    long_body = "\n\n".join(
        f"Absatz {i}: " + ("Der Reaktor liefert Energie fuer die Hydraulik. " * 12)
        for i in range(12)
    )
    (docs / "03-lang.md").write_text(
        "---\nid: lang\n---\n\n# Langer Abschnitt\n\n" + long_body,
        encoding="utf-8",
    )
    return target


@pytest.fixture
def conn(project: Path):
    connection = open_index(project)
    yield connection
    connection.close()
