"""`atlas measure`: token savings measured on the project itself, not claimed."""

from __future__ import annotations

import json

import pytest

from atlas import indexer, measure


@pytest.fixture
def small_project(tmp_path):
    """A project with enough text so that the baseline carries weight."""
    docs = tmp_path / "docs"
    docs.mkdir()
    body = "Der Reaktor liefert Energie fuer die Hydraulik. " * 40
    for index, name in enumerate(("01-core", "02-combat", "03-lore"), start=1):
        (docs / f"{name}.md").write_text(
            f"---\nid: part{index}\ntitle: Part {index}\n---\n\n"
            f"# Part {index}\n\n{body}\n\n## Detail {index}\n\n{body}\n",
            encoding="utf-8",
        )
    conn = indexer.open_index(tmp_path)
    indexer.refresh(conn, tmp_path, force=True)
    yield tmp_path, conn
    conn.close()


def test_baseline_counts_all_documents(small_project):
    root, conn = small_project

    tokens, docs = measure.document_tokens(conn)

    assert docs == 3
    assert tokens > 2_000


def test_measure_reports_a_saving_per_question(small_project):
    root, conn = small_project

    payload = measure.measure(root, conn, sample=3)

    assert payload["answered"] == 3
    assert payload["missed"] == 0
    assert payload["saved"] > 0.5
    assert payload["naive_total"] == payload["baseline_tokens"] * 3


def test_measure_does_not_count_a_miss_as_a_saving(small_project):
    root, conn = small_project

    payload = measure.measure(root, conn, questions=["zxqv nonexistent topic"])

    assert payload["missed"] == 1
    assert payload["atlas_total"] == 0
    assert payload["saved"] == 0.0
    assert payload["rows"][0]["found"] is False


def test_generated_questions_are_flagged_as_such(small_project):
    """Auto-generated questions contain the section heading — this must be
    stated in the report so the number is not mistaken for an independent benchmark."""
    root, conn = small_project

    payload = measure.measure(root, conn, sample=2)

    assert payload["generated_questions"] is True
    assert "heading" in payload["caveat"]


def test_explicit_questions_have_no_caveat(small_project):
    root, conn = small_project

    payload = measure.measure(root, conn, questions=["Where is Detail 1 described?"])

    assert payload["generated_questions"] is False
    assert payload["caveat"] == ""
    assert payload["answered"] == 1


def test_questions_are_unique(small_project):
    """Two sections with the same heading must not produce duplicate questions."""
    root, conn = small_project

    questions = measure.auto_questions(conn, limit=10)

    assert len(questions) == len(set(questions))


def test_render_shows_the_total(small_project):
    root, conn = small_project

    text = measure.render(measure.measure(root, conn, sample=2))

    assert "Total" in text
    assert "naive" in text and "atlas" in text
    assert "%" in text


def test_json_payload_is_serialisable(small_project):
    root, conn = small_project

    payload = measure.measure(root, conn, sample=2)

    json.dumps(payload)  # must not raise


def test_measure_includes_code_symbol_benchmark(small_project):
    """`atlas measure` also benchmarks `atlas_code_find` against reading full source files."""
    root, conn = small_project
    src = root / "src"
    src.mkdir()
    (src / "monolith.py").write_text(
        ("# large header comment line for realistic file token count\n" * 300)
        + "def compute_critical_damage(base: int, mult: float) -> int:\n"
        + "".join(f"    val_{i} = base + {i}\n" for i in range(15))
        + "    return int(val_14 * mult)\n",
        encoding="utf-8",
    )

    payload = measure.measure(root, conn, sample=2)
    assert "code" in payload
    assert payload["code"]["files"] == 1
    assert payload["code"]["symbols"] == 1
    assert payload["code"]["saved"] > 0.8

    rendered = measure.render(payload)
    assert "Code files: 1" in rendered
    assert "compute_critical_damage" in rendered
    assert "Code Total" in rendered

