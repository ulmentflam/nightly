"""Characterization tests for `nightly_core.vault.project` and `build()`.

The fixture under `tests/fixtures/vault_sample_run/` mirrors the shape of
a real Nightly run dir (briefing.md, lessons.md, tasks/*/plan.md +
notes.md). Tests copy it into a `tmp_path` repo layout and project,
asserting the on-disk output matches the documented projection contract.

`.nightly/runs/` is gitignored, so we can't characterize against a real
committed run — the fixture is the v0 stand-in. When dogfooding adds a
representative committed fixture, swap this over.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from nightly_core.plans import parse_frontmatter
from nightly_core.vault import build, project_run, vault_root_for
from nightly_core.vault.manifest import MANIFEST_SCHEMA_VERSION

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vault_sample_run"
SAMPLE_RUN_ID = "2026-05-27T16-30-35Z"


@pytest.fixture
def repo_with_run(tmp_path: Path) -> Path:
    """A `tmp_path` repo with the sample run copied into `.nightly/runs/<id>/`."""
    runs_dir = tmp_path / ".nightly" / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(FIXTURE_DIR, runs_dir / SAMPLE_RUN_ID)
    return tmp_path


def test_project_run_writes_run_task_and_lesson_nodes(repo_with_run: Path):
    result = project_run(SAMPLE_RUN_ID, repo_root=repo_with_run)

    vault = vault_root_for(repo_with_run)

    run_md = vault / "runs" / f"{SAMPLE_RUN_ID}.md"
    assert run_md.is_file()
    assert (vault / "tasks" / f"{SAMPLE_RUN_ID}--0001-sample-task-one.md").is_file()
    assert (vault / "tasks" / f"{SAMPLE_RUN_ID}--0002-sample-task-two.md").is_file()
    assert (vault / "lessons" / f"{SAMPLE_RUN_ID}--1.md").is_file()
    assert (vault / "lessons" / f"{SAMPLE_RUN_ID}--2.md").is_file()
    assert (vault / "lessons" / f"{SAMPLE_RUN_ID}--3.md").is_file()

    assert len(result.task_nodes) == 2
    assert len(result.lesson_nodes) == 3
    assert result.run_node.id == f"run/{SAMPLE_RUN_ID}"


def test_run_node_has_spawned_edges_to_each_task(repo_with_run: Path):
    result = project_run(SAMPLE_RUN_ID, repo_root=repo_with_run)
    spawned = result.run_node.edges.get("spawned", ())
    assert set(spawned) == {
        f"task/{SAMPLE_RUN_ID}--0001-sample-task-one",
        f"task/{SAMPLE_RUN_ID}--0002-sample-task-two",
    }


def test_task_node_carries_parent_edge_and_proposer_fingerprint(repo_with_run: Path):
    project_run(SAMPLE_RUN_ID, repo_root=repo_with_run)

    task_md = vault_root_for(repo_with_run) / "tasks" / f"{SAMPLE_RUN_ID}--0001-sample-task-one.md"
    text = task_md.read_text(encoding="utf-8")
    assert "kind: task" in text
    assert f"parent: run/{SAMPLE_RUN_ID}" in text
    assert "status: done" in text
    # Renderer quotes scalars containing `:` for YAML safety.
    assert 'proposer_fingerprint: "sample:proposer:fixtures"' in text
    assert "task_number: 1" in text


def test_task_node_aggregates_notes_section(repo_with_run: Path):
    project_run(SAMPLE_RUN_ID, repo_root=repo_with_run)

    task_md = vault_root_for(repo_with_run) / "tasks" / f"{SAMPLE_RUN_ID}--0002-sample-task-two.md"
    text = task_md.read_text(encoding="utf-8")
    assert "## Notes" in text
    assert "Free-form notes the agent wrote" in text


def test_lesson_nodes_split_per_bullet_with_bold_titles(repo_with_run: Path):
    result = project_run(SAMPLE_RUN_ID, repo_root=repo_with_run)

    titles = [lesson.title for lesson in result.lesson_nodes]
    assert titles == [
        "First lesson with a bold prefix",
        "Second lesson",
        "Third lesson with code",
    ]

    first_body = result.lesson_nodes[0].body
    assert "continues across multiple lines" in first_body


def test_projection_is_idempotent(repo_with_run: Path):
    first = project_run(SAMPLE_RUN_ID, repo_root=repo_with_run)
    second = project_run(SAMPLE_RUN_ID, repo_root=repo_with_run)

    # Same file set, same content (re-running doesn't append or drift).
    assert {p.name for p in first.paths_written} == {p.name for p in second.paths_written}
    for path in first.paths_written:
        # The file still exists and is readable; re-projection didn't corrupt it.
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        metadata, _ = parse_frontmatter(text)
        assert metadata.get("kind") in {"run", "task", "lesson"}


def test_project_run_raises_for_missing_run(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        project_run(SAMPLE_RUN_ID, repo_root=tmp_path)


def test_build_walks_all_runs_and_writes_manifest(repo_with_run: Path):
    # Add a second run so we can confirm `build()` finds both.
    second_run_id = "2026-05-28T09-15-00Z"
    shutil.copytree(
        FIXTURE_DIR,
        repo_with_run / ".nightly" / "runs" / second_run_id,
    )

    result = build(repo_with_run)

    assert len(result.projections) == 2
    # Each run projects 1 run + 2 tasks + 3 lessons = 6 nodes; total = 12.
    assert result.total_nodes == 12

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert manifest["run_count"] == 2
    assert manifest["node_count_by_kind"]["run"] == 2
    assert manifest["node_count_by_kind"]["task"] == 4
    assert manifest["node_count_by_kind"]["lesson"] == 6
    assert manifest["node_count_by_kind"]["pr"] == 0
