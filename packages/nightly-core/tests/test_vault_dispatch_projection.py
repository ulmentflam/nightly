"""Tests for dispatch node projection (RFC 003 second slice)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightly_core.vault import project_run, vault_root_for


@pytest.fixture
def repo_with_dispatched_task(tmp_path: Path) -> Path:
    """A repo with one task that recorded a dispatch.json."""
    run_id = "2026-05-31T08-00-00Z"
    run_path = tmp_path / ".nightly" / "runs" / run_id
    task_dir = run_path / "tasks" / "0001-implement-vault"
    task_dir.mkdir(parents=True)

    (task_dir / "plan.md").write_text(
        "---\n"
        "status: done\n"
        "slug: 0001-implement-vault\n"
        "task_number: 1\n"
        "created: 2026-05-31T08:00:00Z\n"
        "updated: 2026-05-31T08:45:00Z\n"
        "---\n"
        "# Task 0001\n\nbody\n",
        encoding="utf-8",
    )

    (task_dir / "dispatch.json").write_text(
        json.dumps(
            {
                "slug": "0001-implement-vault",
                "role": "implementer",
                "host": "claude",
                "pid": 12345,
                "log_path": ".nightly/runs/2026-05-31T08-00-00Z/tasks/0001-implement-vault/dispatch.log",
                "started_at": "2026-05-31T08:05:00+00:00",
                "argv": ["claude", "-p", "..."],
                "cwd": "/tmp/worktree",
                "status": "completed",
                "exit_code": 0,
                "finished_at": "2026-05-31T08:42:18+00:00",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_dispatch_node_is_projected(repo_with_dispatched_task: Path):
    run_id = "2026-05-31T08-00-00Z"
    result = project_run(run_id, repo_root=repo_with_dispatched_task)

    assert len(result.dispatch_nodes) == 1
    node = result.dispatch_nodes[0]
    assert node.kind == "dispatch"
    assert node.id == f"dispatch/{run_id}--0001-implement-vault--1"
    assert node.status == "completed"
    assert node.title == "implementer · 0001-implement-vault"
    assert node.data["specialist"] == "implementer"
    assert node.data["host"] == "claude"
    assert node.data["pid"] == 12345
    assert node.data["exit_code"] == 0
    # Duration is computed from started_at/finished_at
    assert node.data["duration_s"] > 0


def test_dispatch_node_parent_edge_points_to_task(repo_with_dispatched_task: Path):
    run_id = "2026-05-31T08-00-00Z"
    result = project_run(run_id, repo_root=repo_with_dispatched_task)
    node = result.dispatch_nodes[0]
    assert node.edges["parent"] == (f"task/{run_id}--0001-implement-vault",)


def test_dispatch_md_file_is_written(repo_with_dispatched_task: Path):
    run_id = "2026-05-31T08-00-00Z"
    project_run(run_id, repo_root=repo_with_dispatched_task)
    vault = vault_root_for(repo_with_dispatched_task)
    md = vault / "dispatches" / f"{run_id}--0001-implement-vault--1.md"
    assert md.is_file()
    text = md.read_text(encoding="utf-8")
    assert "kind: dispatch" in text
    assert "status: completed" in text
    assert f"parent: task/{run_id}--0001-implement-vault" in text


def test_dispatch_node_body_wiki_links_task(repo_with_dispatched_task: Path):
    """Dispatch node body should `[[link]]` back to its task for navigation."""
    run_id = "2026-05-31T08-00-00Z"
    result = project_run(run_id, repo_root=repo_with_dispatched_task)
    body = result.dispatch_nodes[0].body
    assert f"[[task/{run_id}--0001-implement-vault]]" in body


def test_dispatch_projection_skips_malformed_json(tmp_path: Path):
    run_id = "2026-05-31T09-00-00Z"
    task_dir = tmp_path / ".nightly" / "runs" / run_id / "tasks" / "0001-broken"
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text(
        "---\nstatus: done\nslug: 0001-broken\n---\n# x\n", encoding="utf-8"
    )
    (task_dir / "dispatch.json").write_text("not json {{{", encoding="utf-8")

    result = project_run(run_id, repo_root=tmp_path)
    assert result.dispatch_nodes == ()  # malformed → skipped silently


def test_dispatch_projection_returns_empty_when_no_dispatches(tmp_path: Path):
    """Existing runs that never used background dispatch project cleanly."""
    run_id = "2026-05-31T10-00-00Z"
    task_dir = tmp_path / ".nightly" / "runs" / run_id / "tasks" / "0001-x"
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text(
        "---\nstatus: done\nslug: 0001-x\n---\n# x\n", encoding="utf-8"
    )

    result = project_run(run_id, repo_root=tmp_path)
    assert result.dispatch_nodes == ()
