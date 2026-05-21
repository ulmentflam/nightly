"""Default proposer registry.

`default_proposers()` is what `nightly_core.ideation.run_proposers` calls
when no explicit list is provided. Tests can substitute their own list
for hermetic, fast runs.
"""

from __future__ import annotations

from nightly_core.proposers.base import Proposer
from nightly_core.proposers.lint_debt import LintDebtProposer
from nightly_core.proposers.todo_fixme import TodoFixmeProposer
from nightly_core.proposers.type_holes import TypeHoleProposer

__all__ = ["default_proposers"]


def default_proposers() -> list[Proposer]:
    """Return a fresh list of every proposer enabled by default in Phase 5."""
    return [
        TodoFixmeProposer(),
        LintDebtProposer(),
        TypeHoleProposer(),
    ]
