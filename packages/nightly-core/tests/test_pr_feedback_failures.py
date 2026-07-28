"""The `gh` wrappers in `pr_feedback` must degrade, never raise.

`pr_rescue` is cascade slot 5 and the rules block makes it a priority —
getting open PRs back to green outranks fresh work. It runs unattended
against a network service, so every failure here is a realistic 3am
event: `gh` not installed, an expired token, a rate limit, a timeout, a
truncated or non-JSON body.

If any of those propagates, the cascade dies mid-run instead of moving to
the next rung. `pr_feedback` was the lowest-coverage module in the
package (70%), and the uncovered lines were precisely these
failure branches — the ones that only execute when something is already
going wrong.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nightly_core import pr_feedback
from nightly_core.pr_feedback import _gh_pr_review_comments, _gh_pr_view


def _stub_run(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", exc: BaseException | None = None
):
    """Replace `subprocess.run` inside pr_feedback only."""

    def fake(*_args, **_kwargs):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(pr_feedback.subprocess, "run", fake)


# ── _gh_pr_view ───────────────────────────────────────────────────────────


def test_pr_view_parses_a_normal_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, stdout=json.dumps({"number": 36, "title": "x"}))
    assert _gh_pr_view("nightly/x", None) == {"number": 36, "title": "x"}


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "gh"),
        subprocess.TimeoutExpired("gh", 30),
        OSError("gh not found"),
    ],
    ids=["gh-failed", "gh-timed-out", "gh-missing"],
)
def test_pr_view_swallows_every_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """No PR for the branch, an expired token, and `gh` not installed are
    all indistinguishable here — and all mean "no feedback to act on"."""
    _stub_run(monkeypatch, exc=exc)
    assert _gh_pr_view("nightly/x", None) is None


def test_pr_view_treats_empty_output_as_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, stdout="   \n")
    assert _gh_pr_view("nightly/x", None) is None


def test_pr_view_survives_non_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy error page or a truncated body must not raise."""
    _stub_run(monkeypatch, stdout="<html>502 Bad Gateway</html>")
    assert _gh_pr_view("nightly/x", None) is None


def test_pr_view_rejects_json_that_is_not_an_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid JSON of the wrong shape is the subtlest case — it parses, so
    only the isinstance guard stops it reaching callers as a PR dict."""
    _stub_run(monkeypatch, stdout=json.dumps([{"number": 36}]))
    assert _gh_pr_view("nightly/x", None) is None


# ── _gh_pr_review_comments ────────────────────────────────────────────────


@pytest.mark.parametrize("pr_number", [0, -1])
def test_review_comments_skips_the_api_for_a_bogus_number(
    monkeypatch: pytest.MonkeyPatch, pr_number: int
) -> None:
    """Guard before the call — a malformed PR number should not spend a
    network round trip to learn it was malformed."""

    def explode(*_a, **_k):
        raise AssertionError("subprocess should not have been invoked")

    monkeypatch.setattr(pr_feedback.subprocess, "run", explode)
    assert _gh_pr_review_comments(pr_number, None) == []


def test_review_comments_parses_a_normal_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, stdout=json.dumps([{"body": "nit"}, {"body": "bug"}]))
    assert _gh_pr_review_comments(36, None) == [{"body": "nit"}, {"body": "bug"}]


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "gh"),
        subprocess.TimeoutExpired("gh", 30),
        OSError("gh not found"),
    ],
    ids=["gh-failed", "gh-timed-out", "gh-missing"],
)
def test_review_comments_swallows_every_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    _stub_run(monkeypatch, exc=exc)
    assert _gh_pr_review_comments(36, None) == []


def test_review_comments_survives_non_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, stdout="rate limit exceeded")
    assert _gh_pr_review_comments(36, None) == []


def test_review_comments_rejects_json_that_is_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--paginate` returns an array; an object means an error envelope."""
    _stub_run(monkeypatch, stdout=json.dumps({"message": "Not Found"}))
    assert _gh_pr_review_comments(36, None) == []


def test_wrappers_return_empty_rather_than_raising_on_a_real_missing_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end shape check with `gh` genuinely absent from PATH: both
    wrappers must return their empty value, not propagate FileNotFoundError."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _gh_pr_view("nightly/x", None) is None
    assert _gh_pr_review_comments(36, None) == []
