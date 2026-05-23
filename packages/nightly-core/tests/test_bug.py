"""Tests for `nightly_core.bug` — debug-bundle report builder."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightly_core.bug import (
    DEFAULT_BUG_REPO,
    build_report,
    gh_command,
    submit_report,
    write_report,
)
from nightly_core.runs import start_run


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real-on-disk repo with `.nightly/` scaffold and one started run."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".nightly" / "runs").mkdir(parents=True)
    return tmp_path


def _seed_run(repo: Path) -> Path:
    """Start a run and return the run path (so tests can drop in markers)."""
    run = start_run(repo)
    return run.path


def test_build_report_writes_under_dot_nightly_bugs(repo: Path) -> None:
    """The report path lives at `.nightly/bugs/<stamp>/report.md` so a
    busted run dir can't keep us from emitting a clean report."""
    _seed_run(repo)
    report = build_report(root=repo, summary="self-concluded mid-cascade")
    assert report.path.parent.parent == repo / ".nightly" / "bugs"
    assert report.path.name == "report.md"


def test_build_report_captures_markers(repo: Path) -> None:
    """When CONCLUDE / SESSION_ACTIVE markers are present, the report
    must include them — this is the load-bearing diagnostic when the
    agent self-concluded."""
    run_path = _seed_run(repo)
    (run_path / "CONCLUDE").write_text("2026-05-23T01:15:27Z\n", encoding="utf-8")
    (run_path / "SESSION_ACTIVE").write_text("2026-05-23T00:00:00Z\n", encoding="utf-8")
    (run_path / "keepalive.log").write_text(
        "2026-05-23T01:15:27Z  decision=conclude        msg=allowed\n",
        encoding="utf-8",
    )

    report = build_report(root=repo, summary="agent self-concluded")
    written = write_report(report)
    text = written.read_text(encoding="utf-8")

    assert "✓ `CONCLUDE`" in text
    assert "✓ `SESSION_ACTIVE`" in text
    assert "decision=conclude" in text
    assert "agent self-concluded" in text


def test_build_report_handles_no_run(repo: Path) -> None:
    """Even without a current run, the command must not crash —
    the operator might be filing a bug about *failing to start* a run."""
    report = build_report(root=repo, summary="cannot start run")
    written = write_report(report)
    text = written.read_text(encoding="utf-8")
    assert "No active run" in text or "(no active run)" in text
    assert "cannot start run" in text


def test_build_report_uses_supplied_title(repo: Path) -> None:
    _seed_run(repo)
    report = build_report(root=repo, title="agent self-conclude regression")
    assert report.title == "agent self-conclude regression"


def test_build_report_auto_titles_when_omitted(repo: Path) -> None:
    _seed_run(repo)
    report = build_report(root=repo)
    assert "Nightly bug report" in report.title


def test_build_report_includes_extra_attachments(repo: Path) -> None:
    """`extra_attachments` lists the files the operator may want to
    paste manually — `gh issue create` has no attachment flag, so we
    surface them rather than auto-attaching."""
    run_path = _seed_run(repo)
    (run_path / "keepalive.log").write_text("log\n", encoding="utf-8")
    (run_path / "briefing.md").write_text("# brief\n", encoding="utf-8")
    report = build_report(root=repo)
    names = {p.name for p in report.extra_attachments}
    assert "keepalive.log" in names
    assert "briefing.md" in names


def test_gh_command_targets_default_repo(repo: Path) -> None:
    _seed_run(repo)
    report = build_report(root=repo)
    cmd = gh_command(report)
    assert cmd[:3] == ["gh", "issue", "create"]
    assert "--repo" in cmd
    assert DEFAULT_BUG_REPO in cmd
    assert "--title" in cmd
    assert "--body-file" in cmd
    # Issue is tagged so the operator can filter on it.
    assert "--label" in cmd


def test_gh_command_repo_override(repo: Path) -> None:
    _seed_run(repo)
    report = build_report(root=repo)
    cmd = gh_command(report, repo="evan/forked-nightly")
    assert "evan/forked-nightly" in cmd
    assert DEFAULT_BUG_REPO not in cmd


def test_submit_report_handles_missing_gh(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When `gh` isn't on PATH, submit returns ok=False with a clear
    error — the report stays on disk for manual filing."""
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda _: None)
    _seed_run(repo)
    report = build_report(root=repo)
    write_report(report)
    result = submit_report(report)
    assert result.ok is False
    assert result.issue_url is None
    assert result.error is not None
    assert "gh" in result.error.lower()


def test_submit_report_parses_issue_url(repo: Path) -> None:
    """On success, the issue URL is extracted from gh's stdout —
    that's what the CLI prints back to the operator."""
    _seed_run(repo)
    report = build_report(root=repo)
    write_report(report)
    fake_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="https://github.com/ulmentflam/nightly/issues/42\n",
        stderr="",
    )
    result = submit_report(report, runner=fake_completed)
    assert result.ok is True
    assert result.issue_url == "https://github.com/ulmentflam/nightly/issues/42"


def test_submit_report_propagates_gh_failure(repo: Path) -> None:
    _seed_run(repo)
    report = build_report(root=repo)
    write_report(report)
    fake_completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="HTTP 401: bad credentials\n",
    )
    result = submit_report(report, runner=fake_completed)
    assert result.ok is False
    assert result.issue_url is None
    assert "401" in (result.error or "")


def test_build_report_includes_rules_block(repo: Path) -> None:
    """The report should pull in the AGENTS.md rules block so the
    triager can see what the agent was supposed to be following."""
    from nightly_core.rules import MARKER_END, MARKER_START, seed_rules

    seed_rules(repo)
    _seed_run(repo)
    report = build_report(root=repo)
    written = write_report(report)
    text = written.read_text(encoding="utf-8")
    assert MARKER_START in text or "Nightly autonomy contract" in text
    assert MARKER_END in text or "Nightly autonomy contract" in text


def test_build_report_never_includes_secrets_from_env(repo: Path) -> None:
    """Sanity check: the report should NOT dump env vars or
    arbitrary `os.environ` — it's filed publicly by default."""
    _seed_run(repo)
    report = build_report(root=repo)
    written = write_report(report)
    text = written.read_text(encoding="utf-8")
    # No `os.environ` style dump; specifically no GH_TOKEN / ANTHROPIC_API_KEY
    # / AWS keys / OPENAI_API_KEY references. (The fixture might not have
    # these set, but the report shouldn't have a section that *could* leak.)
    assert "ANTHROPIC_API_KEY" not in text
    assert "OPENAI_API_KEY" not in text
    assert "GH_TOKEN" not in text
