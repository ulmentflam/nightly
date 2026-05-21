"""Tests for the autonomy bar."""

from __future__ import annotations

from nightly_core.autonomy import (
    AUTO_PR_CATEGORIES,
    AUTO_PR_LOC_CEILING,
    auto_pr_rejection_reason,
    can_auto_pr,
)
from nightly_core.proposers.base import Proposal, ProposerCategory


def _p(
    *,
    category: ProposerCategory = "lint_debt",
    file_scope: tuple[str, ...] = ("src/a.py",),
    estimated_loc: int = 5,
) -> Proposal:
    return Proposal(
        proposer="lint_debt",
        category=category,
        title="t",
        body="b",
        score=1.0,
        file_scope=file_scope,
        estimated_loc=estimated_loc,
    )


def test_auto_pr_categories_are_conservative() -> None:
    # Phase 5: only the two safest categories pass the bar.
    assert frozenset({"lint_debt", "dep_upgrade"}) == AUTO_PR_CATEGORIES


def test_loc_ceiling_matches_brainstorm() -> None:
    assert AUTO_PR_LOC_CEILING == 80


def test_clean_lint_debt_passes() -> None:
    assert can_auto_pr(_p()) is True
    assert auto_pr_rejection_reason(_p()) is None


def test_dep_upgrade_passes() -> None:
    assert can_auto_pr(_p(category="dep_upgrade")) is True


def test_disallowed_category_rejected() -> None:
    reason = auto_pr_rejection_reason(_p(category="todo_audit"))
    assert reason is not None
    assert "category" in reason
    assert can_auto_pr(_p(category="todo_audit")) is False


def test_type_holes_category_rejected() -> None:
    """type_holes is not in the auto-PR set — these need human judgement."""
    assert can_auto_pr(_p(category="type_holes")) is False


def test_multi_file_rejected() -> None:
    reason = auto_pr_rejection_reason(_p(file_scope=("a.py", "b.py")))
    assert reason is not None
    assert "multi-file" in reason


def test_empty_file_scope_rejected() -> None:
    reason = auto_pr_rejection_reason(_p(file_scope=()))
    assert reason is not None
    assert "scope" in reason


def test_loc_at_ceiling_rejected() -> None:
    """The ceiling is exclusive: exactly 80 LOC is too big."""
    reason = auto_pr_rejection_reason(_p(estimated_loc=AUTO_PR_LOC_CEILING))
    assert reason is not None
    assert "ceiling" in reason


def test_loc_just_below_ceiling_accepted() -> None:
    assert can_auto_pr(_p(estimated_loc=AUTO_PR_LOC_CEILING - 1)) is True


def test_loc_zero_rejected_as_unknown() -> None:
    reason = auto_pr_rejection_reason(_p(estimated_loc=0))
    assert reason is not None
    assert "unknown" in reason
