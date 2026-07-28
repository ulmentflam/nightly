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


def test_render_includes_plans_and_prs(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_write_digest_creates_file_under_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = start_run(repo)
    monkeypatch.setattr("nightly_core.cascade.open_nightly_pr_branches", lambda root=None, **kw: [])
    path = write_digest(repo)
    assert path is not None
    assert path == run.path / "digest.md"
    assert path.is_file()
    assert "Nightly session digest" in path.read_text(encoding="utf-8")


def test_write_digest_returns_none_with_no_run(tmp_path: Path) -> None:
    assert write_digest(tmp_path) is None


def test_render_reads_last_history_line(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = start_run(repo)
    (run.path / "keepalive.history").write_text(
        "github_issue|-|first\naccepted_rfc|-|second\n", encoding="utf-8"
    )
    monkeypatch.setattr("nightly_core.cascade.open_nightly_pr_branches", lambda root=None, **kw: [])
    text = render_digest(repo)
    assert "accepted_rfc|-|second" in text


# ── pending handoffs (RFC 012 C2) ─────────────────────────────────────────


def _write_handoff(run_path: Path, slug: str, body: str) -> None:
    d = run_path / "tasks" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "HANDOFF.md").write_text(body, encoding="utf-8")


def test_no_handoffs_found_in_a_clean_run(tmp_path: Path) -> None:
    from nightly_core.digest import find_handoffs

    run = start_run(tmp_path)
    assert find_handoffs(run.path) == []


def test_handoff_summary_skips_the_markdown_title(tmp_path: Path) -> None:
    """The title says which task; the first prose line says what's left."""
    from nightly_core.digest import find_handoffs

    run = start_run(tmp_path)
    _write_handoff(run.path, "0001-alpha", "# Handoff — alpha\n\nB2 remains: six skill files.\n")
    assert find_handoffs(run.path) == [("0001-alpha", "B2 remains: six skill files.")]


def test_handoff_without_prose_still_reports(tmp_path: Path) -> None:
    from nightly_core.digest import find_handoffs

    run = start_run(tmp_path)
    _write_handoff(run.path, "0001-alpha", "# Handoff\n\n")
    assert find_handoffs(run.path) == [("0001-alpha", "(no summary)")]


def test_handoffs_are_sorted_by_slug(tmp_path: Path) -> None:
    from nightly_core.digest import find_handoffs

    run = start_run(tmp_path)
    _write_handoff(run.path, "0002-beta", "second\n")
    _write_handoff(run.path, "0001-alpha", "first\n")
    assert [s for s, _ in find_handoffs(run.path)] == ["0001-alpha", "0002-beta"]


def test_handoffs_absent_run_is_not_an_error(tmp_path: Path) -> None:
    from nightly_core.digest import find_handoffs

    assert find_handoffs(None) == []
    assert find_handoffs(tmp_path / "nope") == []


def test_digest_surfaces_pending_handoffs(tmp_path: Path, monkeypatch) -> None:
    """The digest is what survives compaction — a handoff must ride along."""
    from nightly_core.digest import render_digest

    run = start_run(tmp_path)
    _write_handoff(run.path, "0001-alpha", "# Handoff\n\nAdmission tests still to write.\n")
    monkeypatch.chdir(tmp_path)
    out = render_digest(tmp_path)
    assert "Pending handoffs" in out
    assert "Admission tests still to write." in out


def test_digest_omits_the_section_when_there_are_none(tmp_path: Path, monkeypatch) -> None:
    from nightly_core.digest import render_digest

    start_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert "Pending handoffs" not in render_digest(tmp_path)
