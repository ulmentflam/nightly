"""Keep-alive strategies — what to do when the cascade returns `nothing`.

Borrowed in spirit from Andrej Karpathy's
[`autoresearch`](https://github.com/karpathy/autoresearch) program.md,
which sets a `NEVER STOP` directive for the experimental research loop:

> If you run out of ideas, think harder — read papers referenced in the
> code, re-read the in-scope files for new angles, try combining
> previous near-misses, try more radical architectural changes. The
> loop runs until the human interrupts you, period.

Nightly already has the always-advance principle and the refusal policy
as the only stop condition, but historically when the cascade returned
`nothing` the agent would render the briefing and exit. That's the
*formal* end-of-run, but Karpathy's framing is that the agent should
attempt re-engagement first — re-read context, mine parked / blocked
plans, look at recently-closed PR reviewer notes for inspiration —
before declaring exhaustion.

This module enumerates concrete re-engagement strategies. `pick_keepalive`
returns the first applicable strategy; the CLI's `nightly keepalive`
prints all strategies so the agent can pick (or combine) them. The
cascade itself doesn't auto-dispatch keepalive work — the human-facing
contract is still "render briefing and exit when no work remains" —
but the agent is told to **invoke keepalive first** before believing
the cascade.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nightly_core.paths import planning_dir
from nightly_core.plans import list_plans

__all__ = [
    "KEEPALIVE_STRATEGIES",
    "KeepaliveStrategy",
    "pick_keepalive",
    "render_strategies",
]


@dataclass(frozen=True)
class KeepaliveStrategy:
    """One way to re-engage when the cascade returns `nothing`.

    Each strategy is a self-contained prompt the agent can act on with no
    further input. `name` is a stable short slug for telemetry; `prompt`
    is the verbatim instruction; `applies_when` is a free-text note for
    the operator about when this strategy is most useful (not a gate —
    the agent picks for itself).
    """

    name: str
    prompt: str
    applies_when: str


# The strategies are ordered by Karpathy's "think harder" sequence:
# first re-read the in-scope files, then mine past near-misses, then
# escalate to more radical re-examinations.
KEEPALIVE_STRATEGIES: tuple[KeepaliveStrategy, ...] = (
    KeepaliveStrategy(
        name="reread_planning",
        prompt=(
            "Re-read every file under `.planning/` (RFCs, ADRs, conventions, "
            "brainstorms). Look for design intent the proposer suite did not "
            "surface — half-finished sketches, deferred ideas, 'someday' "
            "notes, conventions that have drifted from the code. Pick one "
            "and scope it as a Nightly task."
        ),
        applies_when=(
            "`.planning/` exists and contains files the recent runs did not "
            "reference."
        ),
    ),
    KeepaliveStrategy(
        name="mine_uncertainty",
        prompt=(
            "Walk every `uncertainty.md` across past task directories. Each "
            "entry is a place where a previous agent picked a default rather "
            "than asking. Many of those defaults are now stale — the code "
            "around them has changed. Pick the most consequential one to "
            "revisit and turn it into a follow-up task."
        ),
        applies_when=(
            "Past runs have written `uncertainty.md` files that have not been "
            "reconciled in a follow-up."
        ),
    ),
    KeepaliveStrategy(
        name="revive_parked",
        prompt=(
            "List every plan with `status: parked` or `status: blocked: approval`. "
            "For each, decide: (a) has the blocker resolved? (the missing "
            "credential exists now, the dependent PR landed, the conflict is "
            "moot) — if so, unblock; (b) is the task still wanted? — if not, "
            "park it permanently with a note. Don't leave parked plans rotting."
        ),
        applies_when=(
            "There exist `parked` or `blocked: approval` plans whose context "
            "may have changed since they were stashed."
        ),
    ),
    KeepaliveStrategy(
        name="merge_near_misses",
        prompt=(
            "Walk past proposals and parked plans for near-misses that share "
            "a theme (overlapping file scope, similar refusal categories, "
            "complementary refactors). Two below-the-bar proposals can "
            "sometimes combine into one above-the-bar task. Sketch the "
            "merger as a new task plan."
        ),
        applies_when=(
            "Recent runs have produced multiple proposals that were below the "
            "autonomy bar individually but might combine cleanly."
        ),
    ),
    KeepaliveStrategy(
        name="closed_pr_inspiration",
        prompt=(
            "Read the last ~10 closed Nightly PRs' review threads (human, "
            "CodeRabbit, Cursor, Copilot, Greptile). Look for reviewer "
            "suggestions that were not actioned because they were out of "
            "scope at the time. Pick one that is *now* in scope and turn it "
            "into a task. Cite the PR + comment URL in the plan."
        ),
        applies_when=(
            "A GitHub remote exists and Nightly has authored merged or "
            "closed PRs in the past."
        ),
    ),
    KeepaliveStrategy(
        name="radical_reread",
        prompt=(
            "Re-read `AGENTS.md` / `CLAUDE.md` and the top-level `README.md` "
            "as if you'd never seen this repo. Note three things that look "
            "weird, surprising, or under-documented. Pick the one most likely "
            "to be a real bug, missing test, or stale doc — and scope a task "
            "to fix it. This is the 'fresh eyes' strategy: assume the "
            "proposer suite missed something obvious."
        ),
        applies_when=(
            "All of the above have been exhausted in the current run. This "
            "is the last-resort re-engagement strategy."
        ),
    ),
)


def render_strategies() -> str:
    """Return all keep-alive strategies as a single markdown document."""
    lines: list[str] = []
    lines.append("# Keep-alive strategies")
    lines.append("")
    lines.append(
        "When `nightly next` returns `nothing` and `nightly ideate` writes "
        "no auto-PR-eligible proposals, walk these strategies in order "
        "before rendering the briefing and exiting. Inspired by Karpathy's "
        "[autoresearch](https://github.com/karpathy/autoresearch) NEVER "
        "STOP / think-harder doctrine."
    )
    lines.append("")
    for strategy in KEEPALIVE_STRATEGIES:
        lines.append(f"## {strategy.name}")
        lines.append("")
        lines.append(f"*Applies when:* {strategy.applies_when}")
        lines.append("")
        lines.append(strategy.prompt)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def pick_keepalive(root: Path | None = None) -> KeepaliveStrategy | None:
    """Return the first strategy that has signal in the current repo.

    Each strategy's applicability is checked with a cheap on-disk probe.
    Returns `None` only when *no* strategy applies — which in practice
    means the radical_reread fallback always wins as long as the repo
    has at least one of `README.md` / `AGENTS.md` / `CLAUDE.md`.
    """
    if _has_planning_files(root):
        return _strategy("reread_planning")
    if _has_uncertainty_files(root):
        return _strategy("mine_uncertainty")
    if _has_parked_plans(root):
        return _strategy("revive_parked")
    # We don't probe for "near-misses" or "closed PRs" structurally — the
    # agent decides whether those are productive. The radical_reread
    # fallback always applies as long as the repo has any of the canonical
    # entry-point docs.
    if _has_entry_docs(root):
        return _strategy("radical_reread")
    return None


def _strategy(name: str) -> KeepaliveStrategy:
    for strategy in KEEPALIVE_STRATEGIES:
        if strategy.name == name:
            return strategy
    msg = f"unknown keepalive strategy: {name}"
    raise KeyError(msg)


def _has_planning_files(root: Path | None) -> bool:
    planning = planning_dir(root)
    if not planning.is_dir():
        return False
    return any(planning.rglob("*.md"))


def _has_uncertainty_files(root: Path | None) -> bool:
    # Walk every plan's parent directory for an `uncertainty.md` sibling.
    return any((plan.path.parent / "uncertainty.md").is_file() for plan in list_plans(root))


_PARKED_STATUSES = {"parked", "blocked: approval"}


def _has_parked_plans(root: Path | None) -> bool:
    return any(plan.status in _PARKED_STATUSES for plan in list_plans(root))


_ENTRY_DOCS = ("README.md", "AGENTS.md", "CLAUDE.md")


def _has_entry_docs(root: Path | None) -> bool:
    base = (root or Path.cwd()).resolve()
    return any((base / name).is_file() for name in _ENTRY_DOCS)


assert len({s.name for s in KEEPALIVE_STRATEGIES}) == len(KEEPALIVE_STRATEGIES), (
    "keepalive strategy names must be unique"
)
