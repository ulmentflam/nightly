"""Tests for `backfill_feedback()` in `nightly_core.vault.project`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nightly_core.pr_feedback import PRFeedback, PRReference
from nightly_core.vault.project import backfill_feedback, project_pr, vault_root_for

# ── helpers ───────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)

_PR_REF = PRReference(
    branch="nightly/feat-foo",
    number=42,
    url="https://example.com/pulls/42",
    state="OPEN",
    title="feat: foo",
)


def _make_feedback(
    *,
    kind: str = "review",
    login: str = "alice",
    is_bot: bool = False,
    body: str = "looks good",
    state: str | None = None,
    file_ref: str | None = None,
    line_ref: int | None = None,
    url: str = "https://example.com/pulls/42#r1",
    created_at: datetime | None = None,
) -> PRFeedback:
    return PRFeedback(
        pr=_PR_REF,
        kind=kind,  # type: ignore[arg-type]
        author_login=login,
        author_is_bot=is_bot,
        body=body,
        state=state,
        file_ref=file_ref,
        line_ref=line_ref,
        created_at=created_at or _NOW,
        url=url,
    )


def _seed_pr_node(repo_root: Path, pr_number: int = 42, branch: str = "nightly/feat-foo") -> Path:
    """Write a minimal PR vault node so backfill_feedback has something to walk."""
    return project_pr(
        pr_number=pr_number,
        title=f"PR #{pr_number}",
        branch=branch,
        url=f"https://example.com/pulls/{pr_number}",
        repo_root=repo_root,
    )


# ── test cases ────────────────────────────────────────────────────────────


def test_backfill_feedback_no_pr_nodes_returns_empty(tmp_path: Path) -> None:
    """When there are no PR nodes in vault/pulls/, the function returns []."""
    paths = backfill_feedback(tmp_path)
    assert paths == []


def test_backfill_feedback_writes_nodes_for_two_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One PR node + two feedback items → two feedback nodes written."""
    _seed_pr_node(tmp_path, pr_number=42, branch="nightly/feat-foo")

    blocking_item = _make_feedback(
        kind="review",
        login="reviewer",
        body="Please fix the bug",
        state="CHANGES_REQUESTED",
        url="https://example.com/pulls/42#r1",
    )
    approval_item = _make_feedback(
        kind="review",
        login="approver",
        body="LGTM!",
        state="APPROVED",
        url="https://example.com/pulls/42#r2",
    )

    monkeypatch.setattr(
        "nightly_core.pr_feedback.fetch_feedback",
        lambda branch, root=None, fetcher=None, since=None: [blocking_item, approval_item],
    )

    paths = backfill_feedback(tmp_path)
    assert len(paths) == 2

    vault_root = vault_root_for(tmp_path)
    feedback_dir = vault_root / "feedback"
    assert feedback_dir.is_dir()

    texts = [p.read_text(encoding="utf-8") for p in paths]

    # One node should be blocking, the other praise.
    statuses = {t.split("status:")[1].split("\n")[0].strip() for t in texts}
    assert "blocking" in statuses
    assert "praise" in statuses


def test_backfill_feedback_status_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CHANGES_REQUESTED review → status: blocking."""
    _seed_pr_node(tmp_path)
    item = _make_feedback(kind="review", state="CHANGES_REQUESTED", body="fix this")
    monkeypatch.setattr(
        "nightly_core.pr_feedback.fetch_feedback",
        lambda branch, root=None, fetcher=None, since=None: [item],
    )

    paths = backfill_feedback(tmp_path)
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "status: blocking" in text


def test_backfill_feedback_status_praise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An APPROVED review → status: praise."""
    _seed_pr_node(tmp_path)
    item = _make_feedback(kind="review", state="APPROVED", body="ship it")
    monkeypatch.setattr(
        "nightly_core.pr_feedback.fetch_feedback",
        lambda branch, root=None, fetcher=None, since=None: [item],
    )

    paths = backfill_feedback(tmp_path)
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "status: praise" in text


def test_backfill_feedback_status_nit_for_plain_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain issue_comment → status: nit."""
    _seed_pr_node(tmp_path)
    item = _make_feedback(kind="issue_comment", body="minor note")
    monkeypatch.setattr(
        "nightly_core.pr_feedback.fetch_feedback",
        lambda branch, root=None, fetcher=None, since=None: [item],
    )

    paths = backfill_feedback(tmp_path)
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "status: nit" in text


def test_backfill_feedback_body_contains_feedback_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feedback node body should contain the original feedback body text."""
    _seed_pr_node(tmp_path)
    item = _make_feedback(body="This is the detailed feedback message.")
    monkeypatch.setattr(
        "nightly_core.pr_feedback.fetch_feedback",
        lambda branch, root=None, fetcher=None, since=None: [item],
    )

    paths = backfill_feedback(tmp_path)
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "This is the detailed feedback message." in text


def test_backfill_feedback_derived_from_edge_points_to_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feedback node's derived_from edge should point to `pr/<number>`."""
    pr_number = 42
    _seed_pr_node(tmp_path, pr_number=pr_number)
    item = _make_feedback()
    monkeypatch.setattr(
        "nightly_core.pr_feedback.fetch_feedback",
        lambda branch, root=None, fetcher=None, since=None: [item],
    )

    paths = backfill_feedback(tmp_path)
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert f"derived_from: [pr/{pr_number}]" in text


def test_backfill_feedback_fetch_raises_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If fetch_feedback raises an exception, backfill_feedback catches it and returns []."""
    _seed_pr_node(tmp_path)

    def boom(branch, root=None, fetcher=None, since=None):
        raise RuntimeError("gh not available")

    monkeypatch.setattr("nightly_core.pr_feedback.fetch_feedback", boom)

    paths = backfill_feedback(tmp_path)
    assert paths == []


def test_backfill_feedback_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running backfill_feedback twice produces the same files (sha is stable)."""
    _seed_pr_node(tmp_path)
    item = _make_feedback(url="https://example.com/pulls/42#r1", body="great work")
    monkeypatch.setattr(
        "nightly_core.pr_feedback.fetch_feedback",
        lambda branch, root=None, fetcher=None, since=None: [item],
    )

    paths_first = backfill_feedback(tmp_path)
    paths_second = backfill_feedback(tmp_path)

    assert len(paths_first) == 1
    assert len(paths_second) == 1
    # Same path — same sha derived from the same url+body.
    assert paths_first[0] == paths_second[0]
    # Content is identical.
    assert paths_first[0].read_text(encoding="utf-8") == paths_second[0].read_text(
        encoding="utf-8"
    )


def test_backfill_feedback_node_id_uses_pr_number_and_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feedback node id should be `feedback/<pr_number>--<12-char-sha>`."""
    import hashlib

    pr_number = 42
    url = "https://example.com/pulls/42#r7"
    body = "unique body text"
    expected_sha = hashlib.sha256((url + "\x00" + body).encode()).hexdigest()[:12]

    _seed_pr_node(tmp_path, pr_number=pr_number)
    item = _make_feedback(url=url, body=body)
    monkeypatch.setattr(
        "nightly_core.pr_feedback.fetch_feedback",
        lambda branch, root=None, fetcher=None, since=None: [item],
    )

    paths = backfill_feedback(tmp_path)
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert f"id: feedback/{pr_number}--{expected_sha}" in text


def test_backfill_feedback_multiple_prs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feedback is collected for every PR node in vault/pulls/."""
    _seed_pr_node(tmp_path, pr_number=10, branch="nightly/pr-ten")
    _seed_pr_node(tmp_path, pr_number=20, branch="nightly/pr-twenty")

    calls: list[str] = []

    def fake_fetch(branch, root=None, fetcher=None, since=None):
        calls.append(branch)
        return [
            _make_feedback(
                body=f"comment on {branch}",
                url=f"https://example.com/{branch}",
            )
        ]

    monkeypatch.setattr("nightly_core.pr_feedback.fetch_feedback", fake_fetch)

    paths = backfill_feedback(tmp_path)
    assert len(paths) == 2
    assert set(calls) == {"nightly/pr-ten", "nightly/pr-twenty"}
