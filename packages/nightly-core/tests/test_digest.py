"""Tests for nightly_core.digest — the session key-state digest."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightly_core import digest as digest_mod
from nightly_core.digest import render_digest, write_digest
from nightly_core.runs import new_task, start_run


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".nightly" / "runs").mkdir(parents=True)
    return tmp_path


def test_render_with_no_run_still_returns_string(tmp_path: Path) -> None:
    """No active run → digest renders a degraded report, never raises."""
    text = render_digest(tmp_path)
    assert "Nightly session digest" in text
    assert "no active run" in text


def test_render_includes_plans_and_prs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = start_run(repo)
    # One in-progress, one blocked, one done plan.
    from nightly_core.plans import update_plan_status  # local import for fixture setup

    for slug, status in (
        ("alpha", "in_progress"),
        ("beta", "blocked: approval"),
        ("gamma", "done"),
    ):
        task = new_task(run, slug=slug)
        update_plan_status(task.path / "plan.md", status)  # type: ignore[arg-type]

    # Fake the PR listing (don't shell out to gh in tests).
    monkeypatch.setattr(
        "nightly_core.cascade.open_nightly_pr_branches",
        lambda root=None, **kw: [("nightly/alpha-123", 42, "https://x/42")],
    )

    text = render_digest(repo)
    assert "in_progress" in text
    assert "alpha" in text
    assert "blocked" in text
    assert "beta" in text
    assert "done this/earlier runs: 1" in text
    assert "#42" in text
    assert "if you can name a recommendation, execute it" in text


def test_render_all_subsystems_failing_returns_string(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every sub-section raising still yields a usable digest string."""
    start_run(repo)

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr("nightly_core.plans.list_plans", _boom)
    monkeypatch.setattr("nightly_core.cascade.open_nightly_pr_branches", _boom)
    monkeypatch.setattr(digest_mod.subprocess, "run", _boom)

    text = render_digest(repo)
    assert "Nightly session digest" in text
    assert "plans unavailable" in text
    assert "PR listing unavailable" in text
    assert "(unknown)" in text  # branch degraded


def test_write_digest_creates_file_under_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = start_run(repo)
    monkeypatch.setattr(
        "nightly_core.cascade.open_nightly_pr_branches", lambda root=None, **kw: []
    )
    path = write_digest(repo)
    assert path is not None
    assert path == run.path / "digest.md"
    assert path.is_file()
    assert "Nightly session digest" in path.read_text(encoding="utf-8")


def test_write_digest_returns_none_with_no_run(tmp_path: Path) -> None:
    assert write_digest(tmp_path) is None


def test_render_reads_last_history_line(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = start_run(repo)
    (run.path / "keepalive.history").write_text(
        "github_issue|-|first\naccepted_rfc|-|second\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "nightly_core.cascade.open_nightly_pr_branches", lambda root=None, **kw: []
    )
    text = render_digest(repo)
    assert "accepted_rfc|-|second" in text
