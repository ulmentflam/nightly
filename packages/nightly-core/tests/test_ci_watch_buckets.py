"""The classifier that decides whether a PR's CI reads as red or green.

`_bucket_for` maps GitHub's `state` / `conclusion` strings onto six
buckets, and `summarize_status` collapses a PR's checks into one verdict.
Everything downstream keys off that verdict: `nightly ci` prints it, and
`pr_rescue` — cascade slot 5, which the rules block makes a priority —
routes on it.

A mis-bucketed conclusion is therefore not a display bug. Classifying
`startup_failure` as anything but `fail` makes a broken PR read as
healthy and quietly removes it from the rescue queue; the agent moves on
to fresh work while the PR stays red. The classifier was uncovered.
"""

from __future__ import annotations

import pytest

from nightly_core.ci_watch import (
    CHECK_STATUS_RANK,
    CheckBucket,
    CICheck,
    _bucket_for,
    summarize_status,
)


def _check(bucket: CheckBucket) -> CICheck:
    return CICheck(name=f"check-{bucket}", bucket=bucket, state=bucket.upper())


# ── failure conclusions ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "action_required", "stale", "startup_failure"],
)
def test_every_failure_conclusion_buckets_as_fail(conclusion: str) -> None:
    """These are the ones that must never read as healthy. `stale` and
    `startup_failure` are the easy ones to overlook — neither contains
    the word 'fail'."""
    assert _bucket_for("completed", conclusion) == "fail"


def test_success_buckets_as_pass() -> None:
    assert _bucket_for("completed", "success") == "pass"


@pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
def test_non_verdict_conclusions_bucket_as_skipping(conclusion: str) -> None:
    """Neither ran nor failed — treating these as `fail` would put PRs in
    the rescue queue that have nothing to rescue."""
    assert _bucket_for("completed", conclusion) == "skipping"


def test_cancelled_is_its_own_bucket() -> None:
    """Distinct from `fail`: a cancelled run usually means someone pushed
    again, not that the code is broken."""
    assert _bucket_for("completed", "cancelled") == "cancel"


# ── in-flight states ──────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["in_progress", "queued", "pending", "waiting"])
def test_in_flight_states_bucket_as_pending(state: str) -> None:
    """State wins over conclusion while a check is still running — a
    stale `success` from a previous attempt must not mark it green."""
    assert _bucket_for(state, "success") == "pending"


def test_state_is_checked_before_conclusion() -> None:
    """The ordering is load-bearing: a queued check carrying an old
    `failure` conclusion is pending, not failed."""
    assert _bucket_for("queued", "failure") == "pending"


# ── unknown / malformed input ─────────────────────────────────────────────


@pytest.mark.parametrize("conclusion", ["", "some_new_github_conclusion"])
def test_unrecognized_conclusion_is_unknown_not_pass(conclusion: str) -> None:
    """The safe default. If GitHub adds a conclusion Nightly has not seen,
    the failure mode must be 'I can't tell', never 'it's fine'."""
    assert _bucket_for("completed", conclusion) == "unknown"


def test_empty_inputs_do_not_raise() -> None:
    assert _bucket_for("", "") == "unknown"


@pytest.mark.parametrize(
    ("state", "conclusion", "expected"),
    [("COMPLETED", "SUCCESS", "pass"), ("IN_PROGRESS", "", "pending")],
)
def test_matching_is_case_insensitive(state: str, conclusion: str, expected: str) -> None:
    """gh has emitted both cases across versions."""
    assert _bucket_for(state, conclusion) == expected


# ── collapsing a PR's checks into one verdict ─────────────────────────────


def test_one_failure_makes_the_whole_pr_red() -> None:
    """However many green checks surround it."""
    checks = tuple(_check(b) for b in ("pass", "pass", "fail", "pass"))
    assert summarize_status(checks) == "fail"


def test_summary_follows_the_declared_rank() -> None:
    """Rank order is the contract; pinned rather than re-derived."""
    for i, bucket in enumerate(CHECK_STATUS_RANK):
        lower = CHECK_STATUS_RANK[i + 1 :]
        if lower:
            checks = tuple(_check(b) for b in (bucket, *lower))
            assert summarize_status(checks) == bucket


def test_a_pr_with_no_checks_is_unknown_not_passing() -> None:
    """No signal is not a green light — the module's own docstring calls
    this out, and it is the difference between "CI has not reported" and
    "CI approved"."""
    assert summarize_status(()) == "unknown"


def test_all_passing_is_pass() -> None:
    assert summarize_status((_check("pass"), _check("pass"))) == "pass"
