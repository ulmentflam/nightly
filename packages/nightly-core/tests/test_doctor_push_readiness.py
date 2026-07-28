"""`nightly doctor` must say when work cannot leave the machine.

Written after a session that completed two RFCs, committed everything,
and could not push — because a signing agent had auto-locked. Nothing in
`status` or `doctor` said so. The work looked done from inside the
session and was invisible from outside it, which is the most expensive
way an overnight run can fail: silently, while appearing to succeed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightly_core.doctor import _branches_without_upstream, _check_push_readiness


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with an `origin` remote that is itself a local bare repo, so
    pushes work offline and upstream tracking is real."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "remote", "add", "origin", str(origin))
    (work / "f.txt").write_text("seed\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


def _commit_on(repo: Path, branch: str, *, push: bool) -> None:
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / f"{branch.replace('/', '-')}.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"work on {branch}")
    if push:
        _git(repo, "push", "-q", "-u", "origin", branch)
    _git(repo, "checkout", "-q", "main")


def test_clean_repo_reports_ok(repo: Path) -> None:
    check = _check_push_readiness(repo)
    assert check.status == "ok"


def test_pushed_branch_is_not_flagged(repo: Path) -> None:
    _commit_on(repo, "nightly/done", push=True)
    assert _check_push_readiness(repo).status == "ok"


def test_branch_ahead_of_its_upstream_is_flagged(repo: Path) -> None:
    """The exact shape of the failure this check was written for."""
    _commit_on(repo, "nightly/wip", push=True)
    _git(repo, "checkout", "-q", "nightly/wip")
    (repo / "more.txt").write_text("more\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unpushed work")
    _git(repo, "checkout", "-q", "main")

    check = _check_push_readiness(repo)
    assert check.status == "warning"
    assert "nightly/wip" in check.detail
    assert "ahead 1" in check.detail


def test_never_pushed_branch_is_flagged(repo: Path) -> None:
    _commit_on(repo, "nightly/fresh", push=False)
    check = _check_push_readiness(repo)
    assert check.status == "warning"
    assert "never pushed" in check.detail
    assert "nightly/fresh" in check.detail


def test_non_nightly_branches_are_ignored(repo: Path) -> None:
    """Doctor speaks for Nightly's work, not the operator's own branches."""
    _commit_on(repo, "feature/mine", push=False)
    assert _check_push_readiness(repo).status == "ok"


def test_merged_and_deleted_upstream_is_not_lost_work(repo: Path) -> None:
    """A `[gone]` upstream means merged-and-cleaned, i.e. local cruft —
    flagging it as unpushed work would cry wolf on every finished task."""
    _commit_on(repo, "nightly/merged", push=True)
    _git(repo, "push", "-q", "origin", "--delete", "nightly/merged")
    _git(repo, "fetch", "-q", "--prune")
    assert _check_push_readiness(repo).status == "ok"


def test_branches_without_upstream_helper(repo: Path) -> None:
    _commit_on(repo, "nightly/a", push=False)
    _commit_on(repo, "nightly/b", push=True)
    assert _branches_without_upstream(repo) == ["nightly/a"]


def test_non_git_directory_is_skipped_not_failed(tmp_path: Path) -> None:
    check = _check_push_readiness(tmp_path)
    assert check.status == "skipped"


def test_broken_signer_is_reported(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked ssh agent fails the commit *and* the push — same key."""
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "gpg.format", "ssh")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ssh-add":
            return subprocess.CompletedProcess(cmd, 0, "The agent has no identities.\n", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    check = _check_push_readiness(repo)
    assert check.status == "warning"
    assert "no identities" in check.detail


def test_signer_not_checked_when_signing_is_off(repo: Path) -> None:
    """`commit.gpgsign=false` means an empty agent is irrelevant."""
    _git(repo, "config", "commit.gpgsign", "false")
    assert _check_push_readiness(repo).status == "ok"


def test_check_never_repairs_anything(repo: Path) -> None:
    """Pushing is the operator's call; unlocking an agent is theirs too."""
    _commit_on(repo, "nightly/fresh", push=False)
    before = subprocess.run(
        ["git", "log", "--oneline", "--all"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    _check_push_readiness(repo)
    after = subprocess.run(
        ["git", "log", "--oneline", "--all"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert before == after
