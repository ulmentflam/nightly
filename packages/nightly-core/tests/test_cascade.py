"""Tests for nightly_core.cascade."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nightly_core.cascade import (
    CascadeChoice,
    next_task,
    pick_accepted_rfc,
    pick_github_issue,
    pick_ideated,
    pick_ideated_fallback,
    pick_in_flight,
    pick_pr_rescue,
    pick_unblocked,
)
from nightly_core.plans import update_plan_status
from nightly_core.pr_feedback import PRFeedback, PRReference
from nightly_core.proposers.base import Proposal
from nightly_core.runs import new_task, start_run
from nightly_core.triage import IssueRecord


@pytest.fixture(autouse=True)
def _disable_gh_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Triage shells out to `gh` by default — make sure tests never do.

    Each test can still inject a fetcher via the per-test monkeypatch.
    """
    monkeypatch.setattr(
        "nightly_core.triage.fetch_via_gh",
        lambda _root, **_: [],
    )


# ── pick_in_flight ────────────────────────────────────────────────────────


def test_pick_in_flight_none_when_empty(tmp_path: Path) -> None:
    assert pick_in_flight(tmp_path) is None


def test_pick_in_flight_returns_first_in_progress_plan(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    new_task(run, slug="alpha")
    beta = new_task(run, slug="beta")
    update_plan_status(beta.path / "plan.md", "in_progress")

    plan = pick_in_flight(tmp_path)
    assert plan is not None
    assert plan.slug == "0002-beta"


def test_pick_in_flight_ignores_done_plans(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    done = new_task(run, slug="alpha")
    update_plan_status(done.path / "plan.md", "done")
    assert pick_in_flight(tmp_path) is None


# ── pick_unblocked ────────────────────────────────────────────────────────


def test_pick_unblocked_none_when_no_blocked_plans(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    new_task(run, slug="alpha")
    assert pick_unblocked(tmp_path) is None


def test_pick_unblocked_skips_blocked_without_approval(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    update_plan_status(task.path / "plan.md", "blocked: approval")
    assert pick_unblocked(tmp_path) is None


def test_pick_unblocked_returns_blocked_with_approval(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    update_plan_status(task.path / "plan.md", "blocked: approval", approval_granted=True)
    plan = pick_unblocked(tmp_path)
    assert plan is not None
    assert plan.approval_granted is True


# ── pick_accepted_rfc ─────────────────────────────────────────────────────


def _seed_rfc(root: Path, *, body: str, name: str = "001-retry.md") -> Path:
    rfcs = root / ".planning" / "rfcs"
    rfcs.mkdir(parents=True, exist_ok=True)
    path = rfcs / name
    path.write_text(body, encoding="utf-8")
    return path


def test_pick_accepted_rfc_none_when_no_planning(tmp_path: Path) -> None:
    assert pick_accepted_rfc(tmp_path) is None


def test_pick_accepted_rfc_ignores_unaccepted(tmp_path: Path) -> None:
    _seed_rfc(
        tmp_path,
        body=("---\nstatus: draft\n---\n# RFC\n\n- [ ] do the thing\n"),
    )
    assert pick_accepted_rfc(tmp_path) is None


def test_pick_accepted_rfc_returns_first_unchecked_item(tmp_path: Path) -> None:
    _seed_rfc(
        tmp_path,
        body=(
            "---\nstatus: accepted\n---\n# RFC\n\n"
            "- [x] already done\n"
            "- [ ] needs doing first\n"
            "- [ ] needs doing second\n"
        ),
    )
    match = pick_accepted_rfc(tmp_path)
    assert match is not None
    assert match.item_text == "needs doing first"


def test_pick_accepted_rfc_skips_rfc_with_no_unchecked_items(tmp_path: Path) -> None:
    _seed_rfc(
        tmp_path,
        body="---\nstatus: accepted\n---\n# RFC\n\n- [x] all done\n",
        name="002.md",
    )
    assert pick_accepted_rfc(tmp_path) is None


# ── pick_github_issue ─────────────────────────────────────────────────────


def _issue(number: int, **kwargs: object) -> IssueRecord:
    created = datetime(2026, 5, 1, tzinfo=UTC)
    return IssueRecord(
        number=number,
        title=str(kwargs.get("title", f"Issue {number}")),
        body=str(kwargs.get("body", "x" * 100)),
        labels=tuple(kwargs.get("labels", ())),  # type: ignore[arg-type]
        created_at=created,
        updated_at=created,
        url=f"https://example/{number}",
        author="alice",
    )


def test_pick_github_issue_none_when_no_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.triage.fetch_via_gh",
        lambda _root, **_: [_issue(1, labels=["do-not-automate"])],
    )
    assert pick_github_issue(tmp_path) is None


def test_pick_github_issue_returns_top_ranked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.triage.fetch_via_gh",
        lambda _root, **_: [_issue(1), _issue(2, labels=["nightly-ready"])],
    )
    pick = pick_github_issue(tmp_path)
    assert pick is not None
    assert pick.number == 2


# ── next_task (full cascade) ──────────────────────────────────────────────


def test_next_task_nothing_when_empty_repo(tmp_path: Path) -> None:
    choice = next_task(tmp_path)
    assert isinstance(choice, CascadeChoice)
    assert choice.source == "nothing"
    assert choice.target_path is None


def test_next_task_resumes_in_flight_first(tmp_path: Path) -> None:
    # set up: a blocked task with approval AND an in-flight task. In-flight wins.
    run = start_run(tmp_path)
    blocked = new_task(run, slug="blocked-one")
    update_plan_status(blocked.path / "plan.md", "blocked: approval", approval_granted=True)
    in_flight = new_task(run, slug="in-flight-one")
    update_plan_status(in_flight.path / "plan.md", "in_progress")

    choice = next_task(tmp_path)
    assert choice.source == "resume_in_flight"
    assert "in-flight-one" in choice.summary


def test_next_task_picks_unblocked_when_no_in_flight(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="parked")
    update_plan_status(task.path / "plan.md", "blocked: approval", approval_granted=True)
    choice = next_task(tmp_path)
    assert choice.source == "unblocked_approval"
    assert "parked" in choice.summary


def test_next_task_picks_rfc_when_no_plans(tmp_path: Path) -> None:
    _seed_rfc(
        tmp_path,
        body="---\nstatus: accepted\n---\n- [ ] add a knob\n",
    )
    choice = next_task(tmp_path)
    assert choice.source == "accepted_rfc"
    assert "add a knob" in choice.summary


def test_next_task_picks_issue_when_no_local_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.triage.fetch_via_gh",
        lambda _root, **_: [_issue(42, title="Fix login bug", labels=["nightly-ready"])],
    )
    choice = next_task(tmp_path)
    assert choice.source == "github_issue"
    assert "42" in choice.summary
    assert choice.score is not None
    assert choice.score > 1.0


def test_next_task_rfc_outranks_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_rfc(
        tmp_path,
        body="---\nstatus: accepted\n---\n- [ ] do RFC work\n",
    )
    monkeypatch.setattr(
        "nightly_core.triage.fetch_via_gh",
        lambda _root, **_: [_issue(99, labels=["nightly-ready"])],
    )
    choice = next_task(tmp_path)
    assert choice.source == "accepted_rfc"


def test_next_task_unblocked_outranks_rfc(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    update_plan_status(task.path / "plan.md", "blocked: approval", approval_granted=True)
    _seed_rfc(
        tmp_path,
        body="---\nstatus: accepted\n---\n- [ ] less urgent\n",
    )
    assert next_task(tmp_path).source == "unblocked_approval"


# ── Phase 5: ideate step ──────────────────────────────────────────────────


def _eligible_proposal(score: float = 3.0) -> Proposal:
    """Build a Proposal that clears the autonomy bar."""
    return Proposal(
        proposer="lint_debt",
        category="lint_debt",
        title="apply autofixable F401",
        body="# body",
        score=score,
        file_scope=("src/a.py",),
        estimated_loc=4,
    )


def _ineligible_proposal(score: float = 5.0) -> Proposal:
    """Build a Proposal that fails the autonomy bar (todo_audit category)."""
    return Proposal(
        proposer="todo_fixme",
        category="todo_audit",
        title="audit TODOs",
        body="# body",
        score=score,
        file_scope=("src/a.py", "src/b.py"),
        estimated_loc=12,
    )


def test_pick_ideated_returns_none_with_no_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The autouse stub returns []; pick_ideated should yield None."""
    assert pick_ideated(tmp_path) is None


def test_pick_ideated_returns_top_eligible_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [_eligible_proposal(score=2.0), _eligible_proposal(score=4.5)],
    )
    pick = pick_ideated(tmp_path)
    assert pick is not None
    assert pick.score == 4.5


def test_pick_ideated_filters_to_auto_pr_eligible_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A higher-scoring ineligible proposal must not win — the bar gates."""
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [
            _ineligible_proposal(score=10.0),  # highest score but ineligible
            _eligible_proposal(score=2.0),
        ],
    )
    pick = pick_ideated(tmp_path)
    assert pick is not None
    assert pick.proposer == "lint_debt"


def test_next_task_picks_ideate_when_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [_eligible_proposal(score=3.5)],
    )
    choice = next_task(tmp_path)
    assert choice.source == "ideate"
    assert "apply autofixable" in choice.summary
    assert choice.score == 3.5
    assert "autonomy bar" in (choice.rationale or "").lower()


def test_next_task_nothing_mentions_session_start(tmp_path: Path) -> None:
    """When the cascade bottoms out in a *disarmed* session, the rationale
    should point the agent at `nightly session start` so the next call
    enables the auto-ideate fallback path."""
    choice = next_task(tmp_path)
    assert choice.source == "nothing"
    assert "session start" in (choice.rationale or "")


def test_next_task_github_issue_outranks_ideate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real human-tagged issue must outrank a proposer-emitted one."""
    monkeypatch.setattr(
        "nightly_core.triage.fetch_via_gh",
        lambda _root, **_: [_issue(7, title="real bug", labels=["nightly-ready"])],
    )
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [_eligible_proposal(score=4.9)],
    )
    choice = next_task(tmp_path)
    assert choice.source == "github_issue"


# ── Phase 9: pr_rescue ───────────────────────────────────────────────────


def _pr_branch(branch: str = "nightly/add-retry-2026-05-21T00-00-00Z", n: int = 42):
    """Fake one open Nightly PR for `_nightly_open_pr_branches` to return."""
    return (branch, n, f"https://github.com/x/y/pull/{n}")


def _feedback(
    *,
    branch: str = "nightly/add-retry-2026-05-21T00-00-00Z",
    n: int = 42,
    kind: str = "review_comment",
    body: str = "consider Decimal here",
    is_bot: bool = False,
    is_blocking_state: str | None = None,
    when: datetime | None = None,
) -> PRFeedback:
    from datetime import UTC

    return PRFeedback(
        pr=PRReference(branch=branch, number=n, url=f"u/{n}", state="OPEN", title="t"),
        kind=kind,  # type: ignore[arg-type]
        author_login="bot" if is_bot else "alice",
        author_is_bot=is_bot,
        body=body,
        state=is_blocking_state,
        created_at=when or datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
        url=f"u/{n}",
    )


def test_pick_pr_rescue_returns_none_when_no_nightly_prs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Autouse stub already returns [] for branches; just verify the result.
    assert pick_pr_rescue(tmp_path) is None


def test_pick_pr_rescue_returns_candidate_with_new_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.cascade._nightly_open_pr_branches",
        lambda _root=None, **_: [_pr_branch()],
    )
    monkeypatch.setattr(
        "nightly_core.cascade.fetch_feedback",
        lambda _branch, root=None, fetcher=None, since=None: [_feedback()],
    )
    candidate = pick_pr_rescue(tmp_path)
    assert candidate is not None
    assert candidate.pr_number == 42
    assert len(candidate.feedback) == 1
    assert candidate.has_blocking is False


def test_pick_pr_rescue_skips_branches_with_no_new_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.cascade._nightly_open_pr_branches",
        lambda _root=None, **_: [_pr_branch()],
    )
    monkeypatch.setattr(
        "nightly_core.cascade.fetch_feedback",
        lambda _b, root=None, fetcher=None, since=None: [],  # no feedback
    )
    assert pick_pr_rescue(tmp_path) is None


def test_pick_pr_rescue_ranks_blocking_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking feedback should win over non-blocking feedback, regardless of count."""
    b1 = _pr_branch("nightly/alpha-1", 1)
    b2 = _pr_branch("nightly/beta-2", 2)

    def fake_fetch(branch, root=None, fetcher=None, since=None):
        if branch == b1[0]:
            # non-blocking but 5 items
            return [_feedback(branch=b1[0], n=1, kind="review_comment") for _ in range(5)]
        return [
            _feedback(
                branch=b2[0],
                n=2,
                kind="review",
                body="needs more tests",
                is_blocking_state="CHANGES_REQUESTED",
            ),
        ]

    monkeypatch.setattr(
        "nightly_core.cascade._nightly_open_pr_branches",
        lambda _root=None, **_: [b1, b2],
    )
    monkeypatch.setattr("nightly_core.cascade.fetch_feedback", fake_fetch)

    candidate = pick_pr_rescue(tmp_path)
    assert candidate is not None
    assert candidate.pr_number == 2  # blocking wins despite lower count
    assert candidate.has_blocking is True


def test_pick_pr_rescue_matches_plan_by_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a plan's slug appears in the branch, link them."""
    run = start_run(tmp_path)
    task = new_task(run, slug="add-retry")
    branch_with_slug = "nightly/add-retry-2026-05-21T00-00-00Z"
    monkeypatch.setattr(
        "nightly_core.cascade._nightly_open_pr_branches",
        lambda _root=None, **_: [(branch_with_slug, 7, "u/7")],
    )
    monkeypatch.setattr(
        "nightly_core.cascade.fetch_feedback",
        lambda _b, root=None, fetcher=None, since=None: [_feedback(branch=branch_with_slug, n=7)],
    )
    candidate = pick_pr_rescue(tmp_path)
    assert candidate is not None
    assert candidate.plan_path == task.path / "plan.md"


def test_pick_pr_rescue_no_plan_match_still_returns_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orphan PR (no matching plan) still surfaces — agent reads PR fresh."""
    monkeypatch.setattr(
        "nightly_core.cascade._nightly_open_pr_branches",
        lambda _root=None, **_: [("nightly/orphan-9999", 9, "u/9")],
    )
    monkeypatch.setattr(
        "nightly_core.cascade.fetch_feedback",
        lambda _b, root=None, fetcher=None, since=None: [
            _feedback(branch="nightly/orphan-9999", n=9)
        ],
    )
    candidate = pick_pr_rescue(tmp_path)
    assert candidate is not None
    assert candidate.plan_path is None


def test_next_task_pr_rescue_fires_between_issue_and_ideate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No issues, but a Nightly PR has new feedback → pr_rescue, not ideate."""
    monkeypatch.setattr(
        "nightly_core.cascade._nightly_open_pr_branches",
        lambda _root=None, **_: [_pr_branch()],
    )
    monkeypatch.setattr(
        "nightly_core.cascade.fetch_feedback",
        lambda _b, root=None, fetcher=None, since=None: [_feedback()],
    )
    # Force ideate to also have a viable candidate — pr_rescue must outrank.
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [_eligible_proposal()],
    )

    choice = next_task(tmp_path)
    assert choice.source == "pr_rescue"
    assert "42" in choice.summary
    assert "feedback" in (choice.rationale or "").lower()


def test_next_task_github_issue_still_outranks_pr_rescue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh issues outrank rescue: starting beats finishing only when there's
    nothing to start with. pr_rescue is below github_issue in the cascade."""
    monkeypatch.setattr(
        "nightly_core.triage.fetch_via_gh",
        lambda _root, **_: [_issue(7, labels=["nightly-ready"])],
    )
    monkeypatch.setattr(
        "nightly_core.cascade._nightly_open_pr_branches",
        lambda _root=None, **_: [_pr_branch()],
    )
    monkeypatch.setattr(
        "nightly_core.cascade.fetch_feedback",
        lambda _b, root=None, fetcher=None, since=None: [
            _feedback(is_blocking_state="CHANGES_REQUESTED", kind="review"),
        ],
    )
    choice = next_task(tmp_path)
    # Brainstorm §03 step 4 (github_issue) precedes step 5 (pr_rescue).
    assert choice.source == "github_issue"


# ── auto-ideate fallback (armed-session lever) ────────────────────────────


def _arm_session(root: Path) -> Path:
    """Helper: start a run and arm SESSION_ACTIVE on it."""
    run = start_run(root)
    marker = run.path / "SESSION_ACTIVE"
    marker.write_text("armed\n", encoding="utf-8")
    return marker


def test_pick_ideated_fallback_returns_top_regardless_of_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback returns the highest-scoring proposal even if ineligible."""
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [
            _ineligible_proposal(score=9.0),
            _eligible_proposal(score=4.0),
        ],
    )
    pick = pick_ideated_fallback(tmp_path)
    assert pick is not None
    assert pick.score == 9.0
    assert pick.proposer == "todo_fixme"  # ineligible category wins


def test_pick_ideated_fallback_returns_none_when_no_proposals(
    tmp_path: Path,
) -> None:
    """Empty proposer suite → None, even in fallback mode."""
    # Default autouse stub keeps run_proposers empty in the test env.
    assert pick_ideated_fallback(tmp_path) is None


def test_next_task_fires_ideate_fallback_when_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Armed session + only ineligible proposals → cascade dispatches anyway."""
    _arm_session(tmp_path)
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [_ineligible_proposal(score=4.0)],
    )
    choice = next_task(tmp_path)
    assert choice.source == "ideate_fallback"
    assert "fallback" in choice.summary
    assert choice.score == 4.0
    rationale = choice.rationale or ""
    assert "armed" in rationale.lower() or "local proposal" in rationale.lower()


def test_next_task_skips_fallback_when_disarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disarmed session: ineligible proposals → cascade returns `nothing`."""
    # Don't arm; even if there's a run, no SESSION_ACTIVE marker.
    start_run(tmp_path)
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [_ineligible_proposal(score=4.0)],
    )
    choice = next_task(tmp_path)
    assert choice.source == "nothing"


def test_next_task_honors_conclude_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug fix: when CONCLUDE is present, the cascade must NOT hand out new
    work — even when in-flight plans or ideate proposals exist."""
    run = start_run(tmp_path)
    # Plenty of work would normally route — an in-progress plan + an
    # eligible proposal. Both must be ignored once CONCLUDE is written.
    task = new_task(run, slug="alpha")
    update_plan_status(task.path / "plan.md", "in_progress")
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [_eligible_proposal(score=4.0)],
    )
    (run.path / "CONCLUDE").write_text("", encoding="utf-8")

    choice = next_task(tmp_path)
    assert choice.source == "concluded"
    assert choice.target_path is None
    rationale = choice.rationale or ""
    assert "conclude" in rationale.lower()


def test_next_task_strict_ideate_outranks_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An eligible proposal still goes through the strict `ideate` path —
    the fallback is only consulted when strict ideate finds nothing."""
    _arm_session(tmp_path)
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [
            _ineligible_proposal(score=9.0),  # higher but ineligible
            _eligible_proposal(score=3.0),
        ],
    )
    choice = next_task(tmp_path)
    assert choice.source == "ideate"
    assert choice.score == 3.0  # strict path wins over higher-scoring fallback
