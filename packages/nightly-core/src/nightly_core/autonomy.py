"""The autonomy bar — when may a proposed issue be auto-PR'd?

Per the brainstorm §06, a proposal becomes a real PR without human
approval only when ALL of these hold:

1. It is single-file and reverts cleanly.
2. Touched paths have >90% test coverage.
3. It is in the dep-upgrade or autofixable-lint category.
4. The change is < 80 lines.

Phase 5 implements rules 1, 3, and 4. Rule 2 (coverage) requires data we
don't yet collect — `can_auto_pr` will start enforcing it once a coverage
loader lands. Until then, the bar is intentionally conservative: only
single-file, small, lint-or-dep proposals slip through; everything else
waits for human review.
"""

from __future__ import annotations

from nightly_core.proposers.base import Proposal, ProposerCategory

__all__ = [
    "AUTO_PR_CATEGORIES",
    "AUTO_PR_LOC_CEILING",
    "auto_pr_rejection_reason",
    "can_auto_pr",
]


# Categories the bar lets through without human approval.
AUTO_PR_CATEGORIES: frozenset[ProposerCategory] = frozenset({"lint_debt", "dep_upgrade"})

# Per brainstorm §06: < 80 lines.
AUTO_PR_LOC_CEILING = 80


def auto_pr_rejection_reason(proposal: Proposal) -> str | None:
    """Return the human-readable reason this proposal fails the bar, or `None`.

    Returning the reason (rather than just a boolean) lets the briefing
    and the agent surface *why* a proposal couldn't be auto-promoted —
    which is often more useful than the binary verdict.
    """
    if proposal.category not in AUTO_PR_CATEGORIES:
        return (
            f"category '{proposal.category}' is not auto-PR-eligible "
            f"(allowed: {sorted(AUTO_PR_CATEGORIES)})"
        )
    if len(proposal.file_scope) == 0:
        return "file_scope is empty — can't verify single-file constraint"
    if len(proposal.file_scope) > 1:
        return f"multi-file scope ({len(proposal.file_scope)} files) — bar requires single-file"
    if proposal.estimated_loc <= 0:
        return "estimated_loc unknown — can't verify size constraint"
    if proposal.estimated_loc >= AUTO_PR_LOC_CEILING:
        return f"estimated_loc {proposal.estimated_loc} exceeds ceiling {AUTO_PR_LOC_CEILING}"
    return None


def can_auto_pr(proposal: Proposal) -> bool:
    """True if this proposal clears the conservative autonomy bar."""
    return auto_pr_rejection_reason(proposal) is None
