"""Tests for nightly_core.paths."""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from nightly_core import (
    current_run_pointer,
    new_run_id,
    nightly_dir,
    planning_dir,
    repo_root,
    run_dir,
    runs_dir,
)


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_repo_root_finds_git_root(tmp_path: Path) -> None:
    _git_init(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert repo_root(sub).resolve() == tmp_path.resolve()


def test_repo_root_falls_back_to_cwd_outside_git(tmp_path: Path) -> None:
    # tmp_path is intentionally not initialized as a git repo
    assert repo_root(tmp_path).resolve() == tmp_path.resolve()


def test_nightly_and_planning_dirs(tmp_path: Path) -> None:
    assert nightly_dir(tmp_path) == tmp_path / ".nightly"
    assert planning_dir(tmp_path) == tmp_path / ".planning"


def test_runs_layout(tmp_path: Path) -> None:
    assert runs_dir(tmp_path) == tmp_path / ".nightly" / "runs"
    assert current_run_pointer(tmp_path) == tmp_path / ".nightly" / "runs" / "CURRENT"
    assert run_dir("2026-05-20T22-14Z", tmp_path) == (
        tmp_path / ".nightly" / "runs" / "2026-05-20T22-14Z"
    )


def test_new_run_id_format_is_iso_8601_compact() -> None:
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")
    assert pattern.match(new_run_id())


def test_new_run_id_with_explicit_timestamp() -> None:
    moment = datetime(2026, 5, 20, 22, 14, 3, tzinfo=UTC)
    assert new_run_id(moment) == "2026-05-20T22-14-03Z"
