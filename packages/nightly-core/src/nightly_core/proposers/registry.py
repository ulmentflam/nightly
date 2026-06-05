"""Default proposer registry.

`default_proposers()` is what `nightly_core.ideation.run_proposers` calls
when no explicit list is provided. Tests can substitute their own list
for hermetic, fast runs.
"""

from __future__ import annotations

from nightly_core.proposers.base import Proposer
from nightly_core.proposers.lint_debt import LintDebtProposer
from nightly_core.proposers.synthesis import SynthesisProposer
from nightly_core.proposers.todo_fixme import TodoFixmeProposer
from nightly_core.proposers.type_holes import TypeHoleProposer

__all__ = ["default_proposers"]


def default_proposers(*, force_synthesis: bool = False) -> list[Proposer]:
    """Return a fresh list of every proposer enabled by default.

    Phase 5 shipped three narrow programmatic proposers (`todo_fixme`,
    `lint_debt`, `type_holes`) — fast, deterministic, but limited to
    static-analysis nits. RFC 009 adds `SynthesisProposer`: LLM-driven
    strategic review across five categories (cleaning / refactoring /
    housekeeping / convenience / capability). Synthesis is best-effort
    (degrades to empty if the host CLI isn't on PATH or fails) and
    throttle-gated via `.nightly/runs/<id>/synthesis.json` cache; the
    three narrow proposers keep running alongside so the morning
    briefing always has *something* in `proposed/issues/` even if
    synthesis fails.

    `force_synthesis=True` (driven from `nightly ideate --force` /
    `nightly propose --force`) bypasses the cache and forces a fresh
    LLM spawn — useful when the operator wants to refresh the
    strategic review mid-session.
    """
    return [
        TodoFixmeProposer(),
        LintDebtProposer(),
        TypeHoleProposer(),
        SynthesisProposer(force=force_synthesis),
    ]
