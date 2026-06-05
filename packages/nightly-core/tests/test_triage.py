"""Tests for nightly_core.triage."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightly_core.triage import (
    IssueRecord,
    fetch_open_pr_issue_refs_via_gh,
    fetch_via_gh,
    rank_issues,
    score_issue,
)

_NOW = datetime(2026, 5, 20, tzinfo=UTC)


def _issue(
    number: int,
    *,
    title: str = "Fix something",
    body: str = "An adequately-long body explaining the acceptance criterion.",
    labels: tuple[str, ...] = (),
    days_old: float = 0.0,
    author: str = "alice",
) -> IssueRecord:
    created = _NOW - timedelta(days=days_old)
    return IssueRecord(
        number=number,
        title=title,
        body=body,
        labels=labels,
        created_at=created,
        updated_at=created,
        url=f"https://github.com/x/y/issues/{number}",
        author=author,
    )


# ── score_issue ───────────────────────────────────────────────────────────


def test_score_no_labels_zero_days_is_baseline() -> None:
    assert score_issue(_issue(1, days_old=0), now=_NOW) == pytest.approx(1.0)


def test_score_nightly_ready_outranks_default() -> None:
    s_default = score_issue(_issue(1), now=_NOW)
    s_ready = score_issue(_issue(2, labels=("nightly-ready",)), now=_NOW)
    assert s_ready > s_default
    assert s_ready == pytest.approx(1.5)


def test_score_bug_outranks_default() -> None:
    s_default = score_issue(_issue(1), now=_NOW)
    s_bug = score_issue(_issue(2, labels=("bug",)), now=_NOW)
    assert s_bug > s_default


def test_score_grows_with_age() -> None:
    """log1p growth: monotonic, but slow enough that a year is ~3.5x
    a fresh issue rather than blowing through the cap."""
    fresh = score_issue(_issue(1, days_old=0), now=_NOW)
    month = score_issue(_issue(2, days_old=30), now=_NOW)
    year = score_issue(_issue(3, days_old=365), now=_NOW)
    assert fresh < month < year
    assert fresh == pytest.approx(1.0)
    # 30 days → 1 + log1p(1) ≈ 1.693
    assert month == pytest.approx(1.0 + 0.693, abs=0.01)
    # 365 days is well below the safety cap of 5.0
    assert year < 5.0
    assert year > 3.0


def test_score_combines_label_and_age() -> None:
    import math

    nr_30d = _issue(1, labels=("nightly-ready",), days_old=30)
    # nightly-ready label = 1.5, age weight at 30d = 1 + log1p(1)
    expected = 1.5 * (1.0 + math.log1p(1.0))
    assert score_issue(nr_30d, now=_NOW) == pytest.approx(expected)


# ── hard gates ────────────────────────────────────────────────────────────


def _fetcher(issues: list[IssueRecord]) -> Callable[[Path], list[IssueRecord]]:
    return lambda _root: issues


def test_do_not_automate_label_skips(tmp_path: Path) -> None:
    rankings = rank_issues(
        tmp_path,
        fetcher=_fetcher([_issue(1, labels=("do-not-automate",))]),
        now=_NOW,
    )
    assert rankings[0].skip_reason is not None
    assert "do-not-automate" in rankings[0].skip_reason


def test_needs_secrets_label_skips(tmp_path: Path) -> None:
    rankings = rank_issues(
        tmp_path,
        fetcher=_fetcher([_issue(1, labels=("needs-secrets",))]),
        now=_NOW,
    )
    assert rankings[0].skip_reason is not None
    assert "needs-secrets" in rankings[0].skip_reason


def test_thin_body_skips(tmp_path: Path) -> None:
    rankings = rank_issues(
        tmp_path,
        fetcher=_fetcher([_issue(1, body="too short")]),
        now=_NOW,
    )
    assert rankings[0].skip_reason is not None
    assert "acceptance criterion" in rankings[0].skip_reason


# ── rank_issues ordering ──────────────────────────────────────────────────


def test_ranking_orders_eligible_by_score_desc(tmp_path: Path) -> None:
    rankings = rank_issues(
        tmp_path,
        fetcher=_fetcher(
            [
                _issue(1, days_old=0),
                _issue(2, labels=("nightly-ready",), days_old=10),
                _issue(3, labels=("bug",), days_old=20),
            ]
        ),
        now=_NOW,
    )
    assert [r.number for r in rankings] == [2, 3, 1]
    assert all(r.skip_reason is None for r in rankings)


def test_ranking_puts_skipped_after_eligible(tmp_path: Path) -> None:
    rankings = rank_issues(
        tmp_path,
        fetcher=_fetcher(
            [
                _issue(1),  # eligible
                _issue(2, labels=("do-not-automate",)),  # skipped
                _issue(3, labels=("nightly-ready",)),  # eligible, higher score
            ]
        ),
        now=_NOW,
    )
    assert rankings[-1].number == 2
    assert rankings[-1].skip_reason is not None
    assert [r.number for r in rankings[:2]] == [3, 1]


def test_ranking_empty_fetcher_returns_empty(tmp_path: Path) -> None:
    assert rank_issues(tmp_path, fetcher=_fetcher([])) == []


# ── fetch_via_gh degradation ──────────────────────────────────────────────


def test_fetch_via_gh_without_gh_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert fetch_via_gh(tmp_path) == []


# ── issue #10 §bug-3: open-PR overlap guard ──────────────────────────────


def test_rank_issues_skips_issue_with_open_pr_fixing_it(tmp_path: Path) -> None:
    """Issue #93 has an open PR (#95) with body 'fixes #93'.

    Regression guard for issue #10's livelock failure: the cascade
    re-picked #93 forever because the ranker had no in-flight-PR
    guard. With the fix, #93 is skipped with a clear reason while
    issues without overlapping PRs are still pickable.
    """
    rankings = rank_issues(
        tmp_path,
        fetcher=_fetcher([_issue(93), _issue(94)]),
        pr_ref_fetcher=lambda _root: frozenset({93}),
        now=_NOW,
    )
    by_num = {r.number: r for r in rankings}
    assert by_num[93].skip_reason == "open PR already addresses this issue"
    assert by_num[94].skip_reason is None
    # Eligible issues come first; #94 is the only eligible one, so it leads.
    assert rankings[0].number == 94


def test_rank_issues_recognizes_all_close_keywords(tmp_path: Path) -> None:
    """The closing-keyword regex matches the full GitHub-documented set:
    close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved —
    case-insensitive, optional colon, optional whitespace before `#`."""
    from nightly_core.triage import _CLOSE_KEYWORD_RE

    cases = {
        "close #1": 1,
        "Closes #2": 2,
        "CLOSED #3": 3,
        "fix #4": 4,
        "Fixes #5": 5,
        "fixed: #6": 6,
        "resolve #7": 7,
        "Resolves #8": 8,
        "resolved #9": 9,
        "closes:  #10": 10,
    }
    for text, expected in cases.items():
        match = _CLOSE_KEYWORD_RE.search(text)
        assert match is not None, f"failed to match: {text!r}"
        assert int(match.group(1)) == expected, f"wrong number for {text!r}"


def test_rank_issues_overlap_guard_silent_when_no_prs(tmp_path: Path) -> None:
    """Empty open-PR set leaves all eligible issues pickable —
    the guard never produces a false positive."""
    rankings = rank_issues(
        tmp_path,
        fetcher=_fetcher([_issue(1), _issue(2)]),
        pr_ref_fetcher=lambda _root: frozenset(),
        now=_NOW,
    )
    assert all(r.skip_reason is None for r in rankings)


def test_fetch_open_pr_issue_refs_via_gh_without_gh_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `gh`, the helper degrades silently to empty — no exceptions."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert fetch_open_pr_issue_refs_via_gh(tmp_path) == frozenset()


def test_fetch_open_pr_issue_refs_via_gh_parses_fixes_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a `gh pr list` payload with `fixes #93` in the body
    yields `{93}` from the helper. Imports `fetch_open_pr_issue_refs_via_gh`
    via the module-top import (a local binding the autouse stub
    cannot retroactively override)."""
    import subprocess as subprocess_module

    payload = (
        '[{"title": "Fix the daemon pull tick", "body": "This PR fixes #93\\n\\nAlso closes #94."}]'
    )

    def _fake_run(*_args, **_kwargs):
        class _R:
            stdout = payload
            returncode = 0

        return _R()

    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/gh")
    monkeypatch.setattr(subprocess_module, "run", _fake_run)
    refs = fetch_open_pr_issue_refs_via_gh(tmp_path)
    assert refs == frozenset({93, 94})
