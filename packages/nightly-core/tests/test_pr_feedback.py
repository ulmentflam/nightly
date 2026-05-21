"""Tests for nightly_core.pr_feedback."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nightly_core.pr_feedback import (
    DEFAULT_REVIEW_BOTS,
    PRFeedback,
    PRReference,
    fetch_feedback,
    fetch_via_gh,
)

_NOW = datetime(2026, 5, 21, tzinfo=UTC)


def _ref(branch: str = "nightly/fix-thing", number: int = 42) -> PRReference:
    return PRReference(
        branch=branch,
        number=number,
        url=f"https://example/p/{number}",
        state="OPEN",
        title="Fix the thing",
    )


def _fb(
    *,
    kind: str = "review",
    login: str = "alice",
    is_bot: bool = False,
    body: str = "looks good",
    state: str | None = None,
    file_ref: str | None = None,
    line_ref: int | None = None,
    created_at: datetime | None = None,
) -> PRFeedback:
    return PRFeedback(
        pr=_ref(),
        kind=kind,  # type: ignore[arg-type]
        author_login=login,
        author_is_bot=is_bot,
        body=body,
        state=state,
        file_ref=file_ref,
        line_ref=line_ref,
        created_at=created_at or _NOW,
        url=_ref().url,
    )


# ── PRFeedback.is_blocking ────────────────────────────────────────────────


def test_is_blocking_for_check_failure() -> None:
    assert _fb(kind="check_failure").is_blocking is True


def test_is_blocking_for_changes_requested_review() -> None:
    assert _fb(kind="review", state="CHANGES_REQUESTED").is_blocking is True


def test_not_blocking_for_approved_review() -> None:
    assert _fb(kind="review", state="APPROVED").is_blocking is False


def test_not_blocking_for_plain_comment() -> None:
    assert _fb(kind="issue_comment").is_blocking is False
    assert _fb(kind="review_comment").is_blocking is False


# ── fetch_feedback (with injected fetcher) ───────────────────────────────


def test_fetch_feedback_passes_through_when_no_filter() -> None:
    items = [_fb(), _fb(login="bob")]

    def fake(_branch: str, _root: Path | None) -> list[PRFeedback]:
        return items

    out = fetch_feedback("nightly/x", fetcher=fake)
    assert out == items


def test_fetch_feedback_filters_by_since(tmp_path: Path) -> None:
    yesterday = _NOW.replace(day=20)
    items = [
        _fb(body="old", created_at=yesterday),
        _fb(body="new", created_at=_NOW),
    ]

    def fake(_branch: str, _root: Path | None) -> list[PRFeedback]:
        return items

    out = fetch_feedback(
        "nightly/x",
        root=tmp_path,
        fetcher=fake,
        since=yesterday,
    )
    assert len(out) == 1
    assert out[0].body == "new"


def test_fetch_feedback_returns_empty_with_no_fetcher_results() -> None:
    out = fetch_feedback("nightly/x", fetcher=lambda _b, _r: [])
    assert out == []


# ── known bot detection ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "login",
    [
        "coderabbitai[bot]",
        "github-copilot[bot]",
        "cursor[bot]",
        "greptile-apps[bot]",
        "dependabot[bot]",
    ],
)
def test_default_review_bots_includes_known_reviewers(login: str) -> None:
    assert login in DEFAULT_REVIEW_BOTS


# ── fetch_via_gh degradation ─────────────────────────────────────────────


def test_fetch_via_gh_without_gh_binary_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert fetch_via_gh("nightly/x", tmp_path) == []


# ── parsing — exercise the gh JSON shape via a stubbed _gh_pr_view ───────


def _stub_gh_pr_view(payload: dict | None) -> object:
    """Build a fake _gh_pr_view that returns the given payload."""

    def fake(_branch: str, _root: Path | None) -> dict | None:
        return payload

    return fake


def _stub_review_comments(items: list[dict]) -> object:
    def fake(_pr_number: int, _root: Path | None) -> list[dict]:
        return items

    return fake


def test_fetch_via_gh_parses_review_with_changes_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/gh")
    monkeypatch.setattr(
        "nightly_core.pr_feedback._gh_pr_view",
        _stub_gh_pr_view(
            {
                "number": 42,
                "title": "Add retry budget",
                "url": "https://gh/x/p/42",
                "state": "OPEN",
                "headRefName": "nightly/add-retry",
                "reviews": [
                    {
                        "author": {"login": "alice", "type": "User"},
                        "body": "needs more tests",
                        "state": "CHANGES_REQUESTED",
                        "submittedAt": "2026-05-21T08:00:00Z",
                        "url": "https://gh/x/p/42#r1",
                    }
                ],
                "comments": [],
                "statusCheckRollup": [],
            }
        ),
    )
    monkeypatch.setattr(
        "nightly_core.pr_feedback._gh_pr_review_comments",
        _stub_review_comments([]),
    )

    out = fetch_via_gh("nightly/add-retry", tmp_path)
    assert len(out) == 1
    item = out[0]
    assert item.kind == "review"
    assert item.author_login == "alice"
    assert item.author_is_bot is False
    assert item.state == "CHANGES_REQUESTED"
    assert item.is_blocking is True


def test_fetch_via_gh_detects_known_bots_by_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """coderabbitai isn't always tagged type=Bot — login-based fallback covers it."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/gh")
    monkeypatch.setattr(
        "nightly_core.pr_feedback._gh_pr_view",
        _stub_gh_pr_view(
            {
                "number": 7,
                "url": "u",
                "state": "OPEN",
                "headRefName": "nightly/x",
                "title": "x",
                "reviews": [
                    {
                        "author": {"login": "coderabbitai[bot]", "type": "User"},
                        "body": "I'll review this for you.",
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-21T08:00:00Z",
                        "url": "u",
                    }
                ],
                "comments": [],
                "statusCheckRollup": [],
            }
        ),
    )
    monkeypatch.setattr(
        "nightly_core.pr_feedback._gh_pr_review_comments",
        _stub_review_comments([]),
    )

    out = fetch_via_gh("nightly/x", tmp_path)
    assert len(out) == 1
    assert out[0].author_is_bot is True
    assert out[0].author_login == "coderabbitai[bot]"


def test_fetch_via_gh_collects_inline_review_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/gh")
    monkeypatch.setattr(
        "nightly_core.pr_feedback._gh_pr_view",
        _stub_gh_pr_view(
            {
                "number": 9,
                "url": "u",
                "state": "OPEN",
                "headRefName": "nightly/x",
                "title": "x",
                "reviews": [],
                "comments": [],
                "statusCheckRollup": [],
            }
        ),
    )
    monkeypatch.setattr(
        "nightly_core.pr_feedback._gh_pr_review_comments",
        _stub_review_comments(
            [
                {
                    "user": {"login": "alice", "type": "User"},
                    "body": "should this be Decimal?",
                    "path": "src/billing.py",
                    "line": 42,
                    "created_at": "2026-05-21T09:00:00Z",
                    "html_url": "u#disc",
                }
            ]
        ),
    )

    out = fetch_via_gh("nightly/x", tmp_path)
    assert len(out) == 1
    item = out[0]
    assert item.kind == "review_comment"
    assert item.file_ref == "src/billing.py"
    assert item.line_ref == 42


def test_fetch_via_gh_collects_failed_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/gh")
    monkeypatch.setattr(
        "nightly_core.pr_feedback._gh_pr_view",
        _stub_gh_pr_view(
            {
                "number": 11,
                "url": "u",
                "state": "OPEN",
                "headRefName": "nightly/x",
                "title": "x",
                "reviews": [],
                "comments": [],
                "statusCheckRollup": [
                    {
                        "name": "ci/pytest",
                        "workflowName": "CI",
                        "conclusion": "FAILURE",
                        "completedAt": "2026-05-21T08:00:00Z",
                        "summary": "3 tests failed",
                        "detailsUrl": "u/checks/1",
                    },
                    {
                        "name": "ci/lint",
                        "conclusion": "SUCCESS",  # should NOT surface
                    },
                ],
            }
        ),
    )
    monkeypatch.setattr(
        "nightly_core.pr_feedback._gh_pr_review_comments",
        _stub_review_comments([]),
    )

    out = fetch_via_gh("nightly/x", tmp_path)
    assert len(out) == 1
    assert out[0].kind == "check_failure"
    assert out[0].state == "ci/pytest"
    assert out[0].is_blocking is True
    assert "3 tests failed" in out[0].body
