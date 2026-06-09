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
# escalate to more radical re-examinations. Every prompt is imperative
# — the contract is "if you can recommend, execute," so these prompts
# command a concrete next action rather than inviting deliberation.
KEEPALIVE_STRATEGIES: tuple[KeepaliveStrategy, ...] = (
    KeepaliveStrategy(
        name="reread_planning",
        prompt=(
            "Open the highest-priority file under `.planning/` (RFC closest to "
            "accepted, newest ADR, most recent brainstorm). Pick the first "
            "design intent that isn't already implemented and scope it as a "
            "Nightly task with `nightly task <slug>`. Start work this turn — "
            "do not write a separate plan-of-plans first."
        ),
        applies_when=("`.planning/` exists and contains files the recent runs did not reference."),
    ),
    KeepaliveStrategy(
        name="mine_uncertainty",
        prompt=(
            "Walk every `uncertainty.md` across past task directories. Each "
            "entry is a refusal-policy gap. Pick the most consequential one, "
            "scope a task to address the underlying refusal (e.g. document a "
            "destructive op proposal, file an approval request), and start "
            "executing this turn."
        ),
        applies_when=(
            "Past runs have written `uncertainty.md` files for refusal-policy "
            "gaps that have not yet been reconciled."
        ),
    ),
    KeepaliveStrategy(
        name="revive_parked",
        prompt=(
            "List every plan with `status: parked` or `status: blocked: approval`. "
            "Pick the first one whose blocker has resolved (credential now "
            "exists, dependent PR landed, conflict moot) and update its "
            "status to `in_progress` — then continue work on it this turn. If "
            "no blocker has cleared, pick the staleest parked plan and mark "
            "it permanently parked with a one-line note."
        ),
        applies_when=(
            "There exist `parked` or `blocked: approval` plans whose context "
            "may have changed since they were stashed."
        ),
    ),
    KeepaliveStrategy(
        name="merge_near_misses",
        prompt=(
            "Walk past proposals and parked plans for two that share theme "
            "(overlapping file scope, similar category, complementary "
            "refactors). Combine them into a single task plan with "
            "`nightly task <slug>` and start executing this turn. If no two "
            "compose cleanly, escalate to `radical_reread`."
        ),
        applies_when=(
            "Recent runs have produced multiple proposals that were below the "
            "autonomy bar individually but might combine cleanly."
        ),
    ),
    KeepaliveStrategy(
        name="closed_pr_inspiration",
        prompt=(
            "List the last ~10 closed Nightly PRs. For each, scan reviewer "
            "threads (human, CodeRabbit, Cursor, Copilot, Greptile) for "
            "suggestions that were out of scope at the time but in scope now. "
            "Pick the first such suggestion, scope it as a task citing the "
            "PR + comment URL, and start executing this turn."
        ),
        applies_when=(
            "A GitHub remote exists and Nightly has authored merged or closed PRs in the past."
        ),
    ),
    KeepaliveStrategy(
        name="radical_reread",
        prompt=(
            "Re-read `AGENTS.md` / `CLAUDE.md` and the top-level `README.md` "
            "as if you'd never seen this repo. Pick the first thing that "
            "looks weird, surprising, or under-documented and most likely to "
            "be a real bug or stale doc. Scope a task for it and start "
            "executing this turn — do not write a survey first."
        ),
        applies_when=(
            "All of the above have been exhausted in the current run. This "
            "is the last-resort re-engagement strategy."
        ),
    ),
    KeepaliveStrategy(
        name="plan_improvement",
        prompt=(
            "GENUINE WORK IS NEVER EXHAUSTED. Enter the planning phase: read "
            "the codebase as a fresh-eyes reader and ship ONE substantial "
            "improvement this turn. The cascade surfaces *human-sourced* work "
            "(RFCs, issues, open PRs); its absence does NOT mean the codebase "
            "is finished. Pick from these angles in rough priority order — "
            "usability (confusing CLI ergonomics, inconsistent flags, poor "
            "error messages, undiscoverable features, install friction), "
            "tests (uncovered branches, missing edge cases, integration gaps), "
            "features (small additive capabilities that compose with what "
            "exists), readability refactor (dead code, duplicated logic, "
            "overly-long functions, unclear names, missing type hints), or "
            "documentation paperwork (README drift, missing ADRs, stale "
            "examples, RFC checklists to reconcile). Assume defaults for "
            "every ambiguity — pick the option most consistent with the "
            "existing code and `.planning/` design intent. Then "
            "`nightly task <slug> -d '<title>'`, set `in_progress`, and "
            "start executing in THIS turn — open a worktree (or write "
            "inline for audit-only work), make the edits, run `nightly "
            "verify`, land a PR or local proposal. Do not write a "
            "plan-of-plans first. Do not scope a 'research' task. Do not "
            "park. Decision over deliberation. The morning briefing is "
            "where the operator reviews the choice; this turn is where "
            "you make it."
        ),
        applies_when=(
            "Universal fallback — applies to any repo with source code. "
            "When the cascade returned `nothing`, the proposer suite came "
            "up empty, and every other keep-alive angle is exhausted, this "
            "strategy still fires because reading the codebase always "
            "produces actionable improvements. This is the last rung of "
            "the keep-alive ladder before refusing to act."
        ),
    ),
)


def render_strategies() -> str:
    """Return all keep-alive strategies as a single markdown document."""
    lines: list[str] = []
    lines.append("# Keep-alive strategies")
    lines.append("")
    lines.append(
        "GENUINE WORK IS NEVER EXHAUSTED. When `nightly next` returns "
        "`nothing` and `nightly ideate` writes no auto-PR-eligible "
        "proposals, walk these strategies in order — and even when every "
        "specific signal is exhausted, the `plan_improvement` universal "
        "fallback applies to any repo with source code (read the codebase, "
        "scope a usability/test/feature/refactor/docs improvement, ship "
        "it). Inspired by Karpathy's "
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
    The walk falls through in preference order: most-specific signal
    (planning files → uncertainty → parked plans → entry docs) first,
    then the `plan_improvement` universal fallback when nothing else
    applies. `plan_improvement` applies to any repo with source code,
    so the only way to get `None` back is in a tmp / fixture path with
    literally no files — the function is otherwise total.
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
    # Universal fallback — applies to any repo with source code. The
    # `plan_improvement` strategy is the keep-alive layer's expression
    # of "GENUINE WORK IS NEVER EXHAUSTED": even in a repo with no
    # planning files, no past runs, no entry docs, reading the source
    # always produces actionable improvements.
    if _has_source_code(root):
        return _strategy("plan_improvement")
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


# Suffix set covers the languages Nightly's proposers + most host repos
# touch — Python (host package), TS/JS (frontend / agent shells), Go /
# Rust (other host codebases), plus Markdown so a docs-only repo still
# gets the `plan_improvement` rung.
_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".rb",
        ".md",
    }
)


def _has_source_code(root: Path | None) -> bool:
    """True iff the repo contains at least one source-like file.

    Walks the top two levels only — we don't need to scan the whole tree;
    we just need to know there's *something* readable. Skips dotted
    directories (`.git`, `.venv`, `.nightly`) and `node_modules` to keep
    the probe cheap even in large repos.
    """
    base = (root or Path.cwd()).resolve()
    if not base.is_dir():
        return False
    skip = {".git", ".venv", ".nightly", "node_modules", "__pycache__"}
    for entry in base.iterdir():
        if entry.is_file() and entry.suffix in _SOURCE_SUFFIXES:
            return True
        if entry.is_dir() and entry.name not in skip and not entry.name.startswith("."):
            for sub in entry.iterdir():
                if sub.is_file() and sub.suffix in _SOURCE_SUFFIXES:
                    return True
    return False


assert len({s.name for s in KEEPALIVE_STRATEGIES}) == len(KEEPALIVE_STRATEGIES), (
    "keepalive strategy names must be unique"
)
