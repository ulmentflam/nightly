"""Tests for nightly_core.ci_watch — CI status polling across Nightly PRs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightly_core.ci_watch import (
    CICheck,
    PRCIStatus,
    fetch_pr_checks,
    list_ci_status,
    summarize_status,
)

# ── summarize_status ─────────────────────────────────────────────────────


def test_summarize_status_empty_is_unknown() -> None:
    assert summarize_status(()) == "unknown"


def test_summarize_status_fail_wins_over_pass() -> None:
    checks = (
        CICheck(name="a", bucket="pass", state="SUCCESS"),
        CICheck(name="b", bucket="fail", state="FAILURE"),
        CICheck(name="c", bucket="pending", state="IN_PROGRESS"),
    )
    assert summarize_status(checks) == "fail"


def test_summarize_status_pending_wins_over_pass() -> None:
    checks = (
        CICheck(name="a", bucket="pass", state="SUCCESS"),
        CICheck(name="b", bucket="pending", state="IN_PROGRESS"),
    )
    assert summarize_status(checks) == "pending"


def test_summarize_status_all_pass() -> None:
    checks = (
        CICheck(name="a", bucket="pass", state="SUCCESS"),
        CICheck(name="b", bucket="pass", state="SUCCESS"),
    )
    assert summarize_status(checks) == "pass"


def test_summarize_status_cancel_above_pending() -> None:
    checks = (
        CICheck(name="a", bucket="pending", state="IN_PROGRESS"),
        CICheck(name="b", bucket="cancel", state="CANCELLED"),
    )
    assert summarize_status(checks) == "cancel"


# ── fetch_pr_checks ──────────────────────────────────────────────────────


def test_fetch_pr_checks_returns_empty_when_gh_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nightly_core.ci_watch.shutil.which", lambda _: None)
    assert fetch_pr_checks("nightly/x", root=tmp_path) == ()


def test_fetch_pr_checks_parses_gh_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nightly_core.ci_watch.shutil.which", lambda _: "/usr/bin/gh")
    fake_stdout = (
        '[{"name":"lint","state":"SUCCESS","bucket":"pass",'
        '"workflow":"ci.yml","link":"https://gh/x"},'
        '{"name":"test","state":"FAILURE","bucket":"fail",'
        '"workflow":"ci.yml","link":"https://gh/x"}]'
    )

    def fake_run(*_a, **_kw):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr("nightly_core.ci_watch.subprocess.run", fake_run)
    checks = fetch_pr_checks("nightly/x", root=tmp_path)
    assert len(checks) == 2
    assert {c.name for c in checks} == {"lint", "test"}
    assert any(c.bucket == "fail" for c in checks)


def test_fetch_pr_checks_handles_gh_error_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nightly_core.ci_watch.shutil.which", lambda _: "/usr/bin/gh")

    def fake_run(*_a, **_kw):
        raise subprocess.CalledProcessError(1, "gh", stderr="boom")

    monkeypatch.setattr("nightly_core.ci_watch.subprocess.run", fake_run)
    assert fetch_pr_checks("nightly/x", root=tmp_path) == ()


def test_fetch_pr_checks_handles_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nightly_core.ci_watch.shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        "nightly_core.ci_watch.subprocess.run",
        lambda *_a, **_kw: subprocess.CompletedProcess([], 0, "not json", ""),
    )
    assert fetch_pr_checks("nightly/x", root=tmp_path) == ()


def test_fetch_pr_checks_unknown_bucket_falls_back_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh has historically used different bucket strings; fall back to state."""
    monkeypatch.setattr("nightly_core.ci_watch.shutil.which", lambda _: "/usr/bin/gh")
    fake_stdout = (
        '[{"name":"build","state":"IN_PROGRESS","bucket":"weirdbucket","workflow":"w","link":"u"}]'
    )
    monkeypatch.setattr(
        "nightly_core.ci_watch.subprocess.run",
        lambda *_a, **_kw: subprocess.CompletedProcess([], 0, fake_stdout, ""),
    )
    checks = fetch_pr_checks("nightly/x", root=tmp_path)
    assert checks[0].bucket == "pending"


# ── list_ci_status ───────────────────────────────────────────────────────


def test_list_ci_status_empty_when_no_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.ci_watch._nightly_open_pr_branches",
        lambda _root=None, **_: [],
    )
    assert list_ci_status(tmp_path) == []


def test_list_ci_status_aggregates_per_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.ci_watch._nightly_open_pr_branches",
        lambda _root=None, **_: [
            ("nightly/alpha", 1, "https://x/1"),
            ("nightly/beta", 2, "https://x/2"),
        ],
    )

    def fake_fetch(branch, root=None):
        if "alpha" in branch:
            return (
                CICheck(name="lint", bucket="pass", state="SUCCESS"),
                CICheck(name="test", bucket="pass", state="SUCCESS"),
            )
        return (
            CICheck(name="lint", bucket="pass", state="SUCCESS"),
            CICheck(name="test", bucket="fail", state="FAILURE"),
        )

    monkeypatch.setattr("nightly_core.ci_watch.fetch_pr_checks", fake_fetch)
    statuses = list_ci_status(tmp_path)
    assert len(statuses) == 2
    by_branch = {s.branch: s for s in statuses}
    assert by_branch["nightly/alpha"].overall == "pass"
    assert by_branch["nightly/beta"].overall == "fail"
    assert by_branch["nightly/beta"].is_failing
    assert {c.name for c in by_branch["nightly/beta"].failed_checks} == {"test"}


def test_prcistatus_is_failing_helpers() -> None:
    """Sanity on the property helpers."""
    s = PRCIStatus(
        branch="x",
        pr_number=1,
        pr_url="u",
        overall="fail",
        checks=(CICheck(name="a", bucket="fail", state="FAILURE"),),
    )
    assert s.is_failing
    assert not s.is_pending
    assert s.failed_checks
