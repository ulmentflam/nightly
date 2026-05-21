"""Proposer framework — Nightly's ideation engine.

When the priority cascade runs out of human-sourced work (no in-flight
plans, no unblocked tasks, no accepted RFC items, no nightly-eligible
issues), `nightly ideate` runs every registered proposer and writes the
ranked results as draft issues to `<run>/proposed/issues/`. Proposals
that pass the conservative autonomy bar (see `nightly_core.autonomy`)
can be auto-promoted to a real task; everything else surfaces in the
briefing for human review.
"""

from nightly_core.proposers.base import (
    Proposal,
    Proposer,
    ProposerCategory,
)
from nightly_core.proposers.lint_debt import LintDebtProposer
from nightly_core.proposers.registry import default_proposers
from nightly_core.proposers.todo_fixme import TodoFixmeProposer
from nightly_core.proposers.type_holes import TypeHoleProposer

__all__ = [
    "LintDebtProposer",
    "Proposal",
    "Proposer",
    "ProposerCategory",
    "TodoFixmeProposer",
    "TypeHoleProposer",
    "default_proposers",
]
