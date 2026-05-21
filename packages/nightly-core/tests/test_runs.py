"""Tests for nightly_core.runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightly_core.paths import current_run_pointer
from nightly_core.runs import (
    conclude_run,
    current_run,
    list_runs,
    new_task,
    next_task_index,
    slugify,
    start_run,
)

# ── slugify ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Fix the login bug", "fix-the-login-bug"),
        ("UPPER CASE", "upper-case"),
        ("with/punctuation!and?", "with-punctuation-and"),
        ("   trim   me   ", "trim-me"),
        ("a" * 80, "a" * 40),  # truncated to 40
        ("", "task"),
        ("///", "task"),
    ],
)
def test_slugify(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


# ── start_run / current_run / conclude_run ────────────────────────────────


def test_start_run_creates_layout_and_current_pointer(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    assert run.path.is_dir()
    assert (run.path / "tasks").is_dir()
    assert (run.path / "proposed").is_dir()
    assert (run.path / "proposed" / "approvals").is_dir()
    assert (run.path / "proposed" / "planning").is_dir()

    pointer = current_run_pointer(tmp_path)
    assert pointer.read_text(encoding="utf-8").strip() == run.id


def test_start_run_with_task_seeds_first_task(tmp_path: Path) -> None:
    run = start_run(tmp_path, task="Fix login bug")
    task_dir = run.path / "tasks" / "0001-fix-login-bug"
    assert task_dir.is_dir()
    plan = (task_dir / "plan.md").read_text(encoding="utf-8")
    assert "Fix login bug" in plan
    assert "File scope" in plan


def test_current_run_returns_none_when_no_run(tmp_path: Path) -> None:
    assert current_run(tmp_path) is None


def test_current_run_returns_run_after_start(tmp_path: Path) -> None:
    started = start_run(tmp_path)
    fetched = current_run(tmp_path)
    assert fetched is not None
    assert fetched.id == started.id
    assert fetched.is_concluded is False


def test_conclude_run_marks_run_concluded(tmp_path: Path) -> None:
    started = start_run(tmp_path)
    concluded = conclude_run(tmp_path)
    assert concluded is not None
    assert concluded.id == started.id
    assert concluded.is_concluded is True
    assert (started.path / "CONCLUDE").is_file()


def test_conclude_run_with_no_current_returns_none(tmp_path: Path) -> None:
    assert conclude_run(tmp_path) is None


# ── list_runs ─────────────────────────────────────────────────────────────


def test_list_runs_empty(tmp_path: Path) -> None:
    assert list_runs(tmp_path) == []


def test_list_runs_returns_runs(tmp_path: Path) -> None:
    a = start_run(tmp_path)
    b = start_run(tmp_path)
    listed = list_runs(tmp_path)
    assert {r.id for r in listed} == {a.id, b.id}


# ── new_task / next_task_index ────────────────────────────────────────────


def test_next_task_index_empty_run(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    assert next_task_index(run) == 1


def test_new_task_numbers_sequentially(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    first = new_task(run, slug="alpha")
    second = new_task(run, slug="beta")
    third = new_task(run, slug="gamma")
    assert (first.index, second.index, third.index) == (1, 2, 3)


def test_new_task_writes_plan_md(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="add-retry", description="Add retry budget")
    plan = (task.path / "plan.md").read_text(encoding="utf-8")
    assert "Add retry budget" in plan
    assert "Success criteria" in plan


def test_new_task_with_existing_slug_is_idempotent(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    first = new_task(run, slug="alpha", description="first")
    second = new_task(run, slug="alpha", description="second-call")
    assert first.path == second.path
    assert first.index == second.index
    # plan.md was not clobbered on the second call
    plan = (first.path / "plan.md").read_text(encoding="utf-8")
    assert "first" in plan
    assert "second-call" not in plan


def test_new_task_normalizes_messy_slug(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="Add!!Retry Budget???")
    assert task.slug == "add-retry-budget"
    assert task.path.name == "0001-add-retry-budget"
