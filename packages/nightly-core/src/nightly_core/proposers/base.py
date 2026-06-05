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

__all__ = [
    "STRATEGIC_CATEGORIES",
    "Proposal",
    "Proposer",
    "ProposerCategory",
    "StrategicCategory",
]


ProposerCategory = Literal[
    "todo_audit",
    "lint_debt",
    "type_holes",
    "dep_upgrade",
    "coverage_gap",
    "doc_drift",
    "synthesis",
]
"""Proposer-kind eligibility bucket the autonomy bar reads. Only `lint_debt`
and `dep_upgrade` are auto-PR-eligible per the brainstorm §06 conservative
defaults. `synthesis` (RFC 009) is *not* auto-PR-eligible — its proposals
always land as draft issues for human review."""


StrategicCategory = Literal[
    "cleaning",
    "refactoring",
    "housekeeping",
    "convenience",
    "capability",
]
"""RFC 009 §3 — five-category ordering the cascade respects when sorting
proposals. Operators want output reviewed in this priority sequence:

- `cleaning`     — dead code, redundant tests, abandoned scaffolding.
- `refactoring`  — long functions, repeated patterns, boundary drift.
- `housekeeping` — naming, layout, doc gaps, type-hint gaps, lint debt,
                   TODO/FIXME audits. The three Phase-5 narrow proposers
                   all backfill to `housekeeping` (they're individual-line
                   nits, not structural review).
- `convenience`  — CLI ergonomics, error messages, auto-completion.
- `capability`   — new cascade sources / specialists / proposers /
                   performance.

The cascade sorts by `(STRATEGIC_CATEGORIES.index(strategic_category),
-score)` so a `cleaning` proposal at score 1.2 outranks a `capability`
proposal at score 1.8 — fixing what's broken before inventing new
things. Operators can opt out via `ideate.category_ordering: false`
in `.nightly/config.yml` (RFC 009 §B3)."""

STRATEGIC_CATEGORIES: tuple[StrategicCategory, ...] = (
    "cleaning",
    "refactoring",
    "housekeeping",
    "convenience",
    "capability",
)
"""Tuple form of the StrategicCategory Literal. The ordering IS the
operator-stated priority — list index = strategic rank."""


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

    strategic_category: StrategicCategory = "housekeeping"
    """RFC 009 §3 — operator-priority bucket for cascade ordering. The
    cascade sorts by `(STRATEGIC_CATEGORIES.index(strategic_category),
    -score)` so cleaning ships before capability even at a lower score.
    Default `"housekeeping"` keeps backward compat for the three Phase-5
    narrow proposers (lint_debt, todo_fixme, type_holes) — they're
    individual-line nits, all housekeeping flavor."""

    @property
    def slug(self) -> str:
        """Filesystem-safe slug derived from the title."""
        return slugify(self.title)

    @property
    def fingerprint(self) -> str:
        """Stable identity used by the cascade to dedupe re-detected work.

        Two proposals from the same proposer, targeting the same category
        and primary scope file, refer to the same underlying signal —
        even if the title or LOC count drifts as the source mutates.
        Picked deliberately narrow (proposer + category + primary scope)
        so the dedupe doesn't over-trigger: different files = different
        fingerprints = both get proposed.

        Empty `file_scope` falls back to the title slug — better than
        nothing for proposers that don't carry scope yet (todo_audit
        once it gains file context). Past failure (issue #2): without
        this, `type_holes` re-detected the same `Any` usages in the same
        file every cascade pass because the prior proposal's local
        branch never reached `main`.

        RFC 009 §5 — synthesis proposals override this with a content-
        hashed fingerprint so two non-deterministic LLM runs proposing
        the same conceptual change dedupe correctly. That logic lives
        in `SynthesisProposer` itself; this default suits the three
        deterministic Phase-5 proposers.
        """
        primary_scope = self.file_scope[0] if self.file_scope else self.slug
        return f"{self.proposer}:{self.category}:{primary_scope}"


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
