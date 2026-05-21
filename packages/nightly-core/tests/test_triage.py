"""Tests for nightly_core.triage."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightly_core.triage import (
    IssueRecord,
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
