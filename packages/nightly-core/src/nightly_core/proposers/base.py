"""The Proposer contract and Proposal value type.

A `Proposer` scans the repo and yields zero or more `Proposal` records.
The ideation orchestrator (`nightly_core.ideation`) runs every registered
proposer, ranks the resulting proposals by score, writes them as draft
issues to `<run>/proposed/issues/`, and returns the top auto-PR-eligible
proposal (if any) for the cascade to act on.

`Proposal.category` is the key the autonomy bar reads to decide whether
a proposal may be auto-promoted to a real task vs. left for human review.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nightly_core.runs import slugify

__all__ = ["Proposal", "Proposer", "ProposerCategory"]


ProposerCategory = Literal[
    "todo_audit",
    "lint_debt",
    "type_holes",
    "dep_upgrade",
    "coverage_gap",
    "doc_drift",
]
"""Buckets the autonomy bar reads. Only `lint_debt` and `dep_upgrade` are
auto-PR-eligible per the brainstorm §06 conservative defaults."""


@dataclass(frozen=True)
class Proposal:
    """One ideation result — a draft issue Nightly suggests creating.

    Persisted as markdown under `<run>/proposed/issues/<slug>.md`. The
    morning briefing renders the list with rank-by-score order.
    """

    proposer: str
    """ID of the proposer that emitted this (matches `Proposer.id`)."""

    category: ProposerCategory

    title: str
    """One-line summary — also the filename slug source."""

    body: str
    """Markdown body. Should include enough context for a human reviewer
    to decide whether to accept, edit, or discard the proposal."""

    score: float
    """Ranking score within the proposals list. Higher = more important."""

    file_scope: tuple[str, ...] = field(default_factory=tuple)
    """Repo-relative paths this proposal would touch. Empty if unknown."""

    estimated_loc: int = 0
    """Rough LOC estimate (0 = unknown). The autonomy bar uses this."""

    @property
    def slug(self) -> str:
        """Filesystem-safe slug derived from the title."""
        return slugify(self.title)


class Proposer(ABC):
    """A proposer scans a repository and yields zero or more `Proposal`s."""

    id: str  # short identifier; should match `Proposal.proposer`

    @abstractmethod
    def propose(self, root: Path) -> Iterable[Proposal]:
        """Inspect `root` and yield proposals.

        Implementations should be best-effort and side-effect-free. Treat
        missing tools (no `ruff` on PATH, no source files of the relevant
        kind) as "nothing to propose" — return an empty iterable, not an
        exception.
        """
