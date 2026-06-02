"""Headless loop driver — the Phase 8 cascade-driven multi-task orchestrator.

Wraps the building blocks already in place:
- The cascade (`nightly_core.cascade.next_task`) picks the next plan to execute.
- The headless primitive (`host.run_headless`) dispatches one task to a host.
- The worktree primitive creates an isolated working directory per task.
- The plan-status lifecycle (`update_plan_status`) is the claim mechanism
  that lets concurrent dispatches avoid stomping on each other.

The driver is intentionally single-process: cross-process invocations of
`nightly run` against the same repo can race on plan-status updates. The
single-process semantics are the contract — that's the canonical "kick
off Nightly from cron once" shape.

Serial mode (`concurrency=1`) is the default. Multi-task parallelism is
opt-in via `concurrency=N`: each batch picks up to N tasks from the
cascade (claiming each by flipping the plan to `in_progress`), dispatches
them via `asyncio.gather`, then reconciles and loops.

The CONCLUDE marker (`<run>/CONCLUDE`) is respected at every batch
boundary so `nightly conclude` from another shell drains cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nightly_core.cascade import CascadeChoice, next_task
from nightly_core.contract import HostId, NightlyHostIntegration
from nightly_core.headless import HeadlessResult
from nightly_core.ideation import run_proposers
from nightly_core.paths import repo_root
from nightly_core.plans import (
    PlanRecord,
    PlanStatus,
    append_pr_feedback,
    read_plan,
    update_plan_status,
)
from nightly_core.proposers.base import Proposal
from nightly_core.runs import Run, TaskDir, current_run, new_task
from nightly_core.worktree import (
    GitRunner,
    WorktreeHandle,
    _resolve_base_branch,
    create_worktree,
)

__all__ = [
    "DriverConfig",
    "TaskOutcome",
    "build_task_prompt",
    "run_loop",
    "run_one_task",
]


_log = logging.getLogger(__name__)


# ── value types ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DriverConfig:
    """Configuration for one invocation of `run_loop`."""

    host_id: HostId = "claude"
    max_tasks: int | None = None
    """Stop after this many tasks have been attempted. `None` = unlimited."""

    concurrency: int = 1
    """Max parallel dispatches per batch. 1 = strictly serial."""

    timeout_per_task_s: float | None = None
    """Headless timeout passed to `host.run_headless` for each task."""

    base_branch: str = "main"
    """Branch to fork the per-task worktree from."""

    branch_prefix: str = "nightly/"

    worktree_root: str | None = None
    """Parent dir for per-task worktrees. `None` = sibling `<repo>-nightly/`.
    Set to a non-synced path to keep trees off iCloud/Dropbox/NFS."""


@dataclass(frozen=True)
class TaskOutcome:
    """The result of dispatching one task."""

    plan_path: Path
    worktree: WorktreeHandle | None
    headless: HeadlessResult | None
    cascade_source: str
    final_status: PlanStatus
    error: str | None = None


# ── prompt builder ────────────────────────────────────────────────────────


def build_task_prompt(
    plan: PlanRecord,
    task_dir: Path,
    *,
    cascade_choice: CascadeChoice | None = None,
) -> str:
    """Build a focused prompt to send to `host.run_headless` for one task.

    Headless invocations don't pre-load the Skill the way an interactive
    session does, so we inline the load-bearing constraints right in the
    prompt. The body of `plan.md` already carries the task's success
    criteria and file scope.

    `cascade_choice` lets the driver tell the agent which cascade step
    produced this task — most importantly, whether it came from the
    auto-ideate fallback (in which case the agent lands locally rather
    than opening a PR).
    """
    is_fallback = cascade_choice is not None and cascade_choice.source == "ideate_fallback"
    if is_fallback:
        landing_section = f"""\
## Landing instructions — LOCAL PROPOSAL (no PR)

This task came from the auto-ideate fallback path: the cascade exhausted
every human-supplied work source and surfaced the highest-scoring
proposal even though it did not clear Nightly's auto-PR autonomy bar
(category / size / file-scope checks). Do **not** open a pull request
for this task. Instead:

1. Implement the change in this worktree as usual.
2. Commit the changes locally on the worktree's branch.
3. Write a thorough `proposal.md` to `{task_dir}` so a reviewer can
   evaluate the change tomorrow morning. Include diff summary, test
   evidence, and the risks you weighed.
4. Set the plan's frontmatter to `status: done`. The branch remains
   unmerged; the morning briefing will surface it for human review.

The whole point of the local-proposal downgrade is that the work is
preserved without burdening human reviewers with an auto-opened PR
that didn't clear the bar.
"""
    else:
        landing_section = f"""\
## Landing instructions

Commit your changes on this worktree's branch and open a PR (the
morning workflow will surface it for review). Write a `proposal.md`
to `{task_dir}` summarizing what you changed, the test plan, and any
risks — even when a PR is opened, the local proposal is the audit
trail.
"""

    if plan.depends_on_pr is not None and not is_fallback:
        landing_section += f"""\

## Declared dependency — base = PR #{plan.depends_on_pr}

This plan declares `depends_on_pr: {plan.depends_on_pr}` in its
frontmatter, so the worktree is intentionally based on PR
#{plan.depends_on_pr}'s head ref rather than `main`. The stacked
geometry is deliberate, not accidental.

When you open the PR (step above), the PR body **must begin** with this
line, exactly:

    Depends on #{plan.depends_on_pr}

That single line tells reviewers (and the morning briefing's geometry
panel) why the base is non-`main`, distinguishes this PR from an
accidental stack, and lets GitHub auto-retarget downstream PRs when
#{plan.depends_on_pr} merges. Do not omit it — RFC 004 §B requires it
so the operator can see declared dependencies at a glance.
"""

    return f"""\
You are Nightly running headlessly on a single task. Your current working
directory is a fresh git worktree forked from the project's main branch.

**Task plan:** `{plan.path}`
**Task folder:** `{task_dir}`

## Core directive

**If you can name a recommendation, execute it.** Do not ask for
clarification, do not pause for confirmation, do not write `uncertainty.md`
entries for ordinary judgment calls (naming, ordering, idiom, library
choice). `uncertainty.md` is reserved for refusal-policy gaps only.

## What to do

1. Read the plan body below carefully.
2. Implement the change inside this worktree. Edit only files in the
   plan's declared file scope.
3. Run the project's tests (`make test`, `pytest`, `npm test`, etc.) and
   confirm they pass.
4. **Run `nightly verify`** — Nightly detects this repo's linters and
   formatters (ruff, black, eslint, prettier, gofmt, rustfmt, …) and
   runs them. Do not declare the task done or open a PR while
   `nightly verify` returns non-zero. Fix the failures first.
5. **Check `nightly ci`** for any prior Nightly PRs with failed checks
   on the remote. CI failures are work — if any open Nightly PR shows
   red, finish this task first, then the cascade will route to
   `pr_rescue` on its own. You do NOT need to block on CI; keep working
   while it runs.

{landing_section}

6. Write `uncertainty.md` to `{task_dir}` **only** if you hit a
   refusal-policy gap. Otherwise leave the file out — recording every
   judgment call is exactly the loophole the new contract closes.
7. Update the plan's YAML frontmatter: `status: done` on a clean land,
   `status: parked` only when a refusal-policy block actually prevents
   completion.

## Refusal policy (do NOT run autonomously)

Destructive git · production-state changes · external comms / publishing ·
network egress to unknown domains · scope creep (edits outside declared
file scope, CI/CD changes, LICENSE edits) · bypassing test or type
safety (new `# type: ignore`, deleted tests, weakened types).

When you would attempt one, write the attempt to
`<run>/proposed/approvals/<id>.md` and either route around it or roll back.

## Plan body

{plan.body}
"""


# ── per-task dispatch ─────────────────────────────────────────────────────


async def run_one_task(  # noqa: PLR0913 - per-task dispatch needs every dimension
    *,
    root: Path,
    host: NightlyHostIntegration,
    plan: PlanRecord,
    timeout_per_task_s: float | None = None,
    base_branch: str = "main",
    branch_prefix: str = "nightly/",
    worktree_root: str | None = None,
    git_runner: GitRunner | None = None,
    cascade_choice: CascadeChoice | None = None,
) -> TaskOutcome:
    """Claim → create worktree → dispatch → reconcile status.

    Errors are caught and surfaced via `TaskOutcome.error` so the loop
    driver can keep going. The plan's status is always restored to a
    terminal state (`done` or `parked`) even if the agent crashed
    mid-task.

    Claim is done via the transient `dispatching` status, which the
    cascade explicitly skips — that's how multi-task batches avoid
    re-picking the same plan within one batch.

    `cascade_choice` is forwarded to `build_task_prompt` so the agent
    gets the right landing instructions — most importantly, the local-
    proposal downgrade for `ideate_fallback` work.
    """
    update_plan_status(plan.path, "dispatching")

    worktree: WorktreeHandle | None = None
    headless: HeadlessResult | None = None
    error: str | None = None

    try:
        effective_base = _resolve_base_branch(
            depends_on_pr=plan.depends_on_pr,
            default_base=base_branch,
            root=root,
        )
        worktree = await create_worktree(
            root,
            slug=plan.slug,
            base_branch=effective_base,
            branch_prefix=branch_prefix,
            worktree_root=worktree_root,
            runner=git_runner,
        )
        prompt = build_task_prompt(plan, plan.path.parent, cascade_choice=cascade_choice)
        headless = await host.run_headless(
            prompt,
            cwd=worktree.path,
            timeout_s=timeout_per_task_s,
        )
    except Exception as exc:
        _log.warning("task %s dispatch failed: %s", plan.slug, exc)
        error = f"dispatch error: {exc}"

    # Reconcile: the agent may have updated the plan status itself. If it
    # left the claim sentinel in place, infer the terminal status from
    # the headless result.
    current = read_plan(plan.path)
    if current.status == "dispatching":
        if headless is not None and headless.ok:
            update_plan_status(plan.path, "done")
        else:
            update_plan_status(plan.path, "parked")
    final_status = read_plan(plan.path).status

    return TaskOutcome(
        plan_path=plan.path,
        worktree=worktree,
        headless=headless,
        cascade_source="",  # the loop fills this in
        final_status=final_status,
        error=error,
    )


# ── batch pickup + parallel dispatch ──────────────────────────────────────


def _conclude_marker(run: Run | None) -> Path | None:
    if run is None:
        return None
    return run.path / "CONCLUDE"


def _is_concluding(run: Run | None) -> bool:
    marker = _conclude_marker(run)
    return marker is not None and marker.is_file()


_IDEATE_SOURCES: frozenset[str] = frozenset({"ideate", "ideate_fallback"})


def _materialize_proposal_as_plan(
    *,
    root: Path,
    proposal: Proposal,
    source: str,
) -> PlanRecord | None:
    """Turn a Proposal into a real `tasks/NNNN-<slug>/plan.md` in the current run.

    Returns the freshly-created plan record (status `ready`), or None if
    no run is active. The plan body carries the proposal's body verbatim
    plus a header noting the cascade source (so a reviewer can see this
    came from auto-ideate vs auto-ideate-fallback).
    """
    run = current_run(root)
    if run is None:
        _log.info("no active run; cannot materialize proposal %r", proposal.title)
        return None
    task: TaskDir = new_task(run, slug=proposal.slug, description=proposal.title)
    plan_path = task.path / "plan.md"
    plan = read_plan(plan_path)
    # Replace the placeholder body with the proposal content, prefixed
    # with a one-line provenance note. Stamp the proposal fingerprint
    # into the frontmatter so the cascade can dedupe re-detected work
    # next pass (issue #2).
    provenance = (
        f"_Proposal materialized from cascade source `{source}` — "
        f"proposer={proposal.proposer}, category={proposal.category}, "
        f"score={proposal.score:.2f}, estimated_loc={proposal.estimated_loc}._\n\n"
    )
    new_body = f"\n{provenance}{proposal.body.rstrip()}\n"
    from nightly_core.plans import (  # noqa: PLC0415 - local
        PROPOSER_FINGERPRINT_KEY,
        render_frontmatter,
    )

    metadata = dict(plan.metadata)
    metadata[PROPOSER_FINGERPRINT_KEY] = proposal.fingerprint

    plan_path.write_text(
        render_frontmatter(metadata, new_body),
        encoding="utf-8",
    )
    return read_plan(plan_path)


async def _pick_batch(
    root: Path,
    batch_size: int,
) -> list[tuple[PlanRecord, CascadeChoice]]:
    """Pull up to `batch_size` tasks from the cascade, claiming each.

    Returns (plan, cascade_choice) tuples. May return fewer than
    `batch_size` if the cascade runs out. The cascade source is
    preserved on the choice for the outcome record.

    For `ideate` / `ideate_fallback` sources the cascade returns a
    Proposal rather than a plan file — we materialize the proposal into
    a real `tasks/NNNN-<slug>/plan.md` first so the rest of the
    dispatch pipeline (worktree, headless, status reconcile) works
    uniformly.
    """
    out: list[tuple[PlanRecord, CascadeChoice]] = []
    for _ in range(batch_size):
        choice = next_task(root)
        if choice.source == "nothing":
            break

        plan: PlanRecord | None = None
        if choice.target_path is not None and choice.target_path.is_file():
            plan = read_plan(choice.target_path)
        elif choice.source in _IDEATE_SOURCES:
            # Run the proposer suite again to fetch the top proposal —
            # the cascade returned a CascadeChoice with `score` but the
            # Proposal object is not on the choice itself. Cheap to redo.
            proposals = run_proposers(root)
            if not proposals:
                _log.info("ideate source %s but proposer suite is empty", choice.source)
                break
            proposal = (
                proposals[0]
                if choice.source == "ideate_fallback"
                else next((p for p in proposals if p.score == choice.score), proposals[0])
            )
            plan = _materialize_proposal_as_plan(root=root, proposal=proposal, source=choice.source)

        if plan is None:
            _log.info(
                "cascade returned %s but no plan available (target=%s); skipping this batch",
                choice.source,
                choice.target_path,
            )
            break

        # Claim the plan so the next cascade tick won't pick it again.
        # `dispatching` is the transient sentinel the cascade explicitly
        # skips — using `in_progress` here would let the cascade re-pick
        # the same plan on the next iteration of this batch loop.
        update_plan_status(plan.path, "dispatching")
        out.append((plan, choice))
    return out


_BatchOutcome = Literal["batch_done", "no_more_work", "concluded", "max_tasks"]


async def run_loop(
    *,
    root: Path | None = None,
    host: NightlyHostIntegration,
    config: DriverConfig | None = None,
    git_runner: GitRunner | None = None,
) -> list[TaskOutcome]:
    """Drive the cascade in headless mode until exhaustion or conclude.

    Returns the list of `TaskOutcome` records, one per dispatched task,
    in the order they completed.
    """
    cfg = config or DriverConfig()
    root_path = (root or repo_root()).resolve()
    results: list[TaskOutcome] = []
    semaphore = asyncio.Semaphore(max(1, cfg.concurrency))

    while True:
        # Check budgets at batch boundaries.
        run = current_run(root_path)
        if _is_concluding(run):
            _log.info("CONCLUDE marker found; exiting loop")
            break
        if cfg.max_tasks is not None and len(results) >= cfg.max_tasks:
            _log.info("max_tasks=%s reached; exiting loop", cfg.max_tasks)
            break

        remaining = cfg.max_tasks - len(results) if cfg.max_tasks is not None else cfg.concurrency
        batch_size = min(cfg.concurrency, max(1, remaining))
        batch = await _pick_batch(root_path, batch_size)
        if not batch:
            _log.info("cascade returned nothing; exiting loop")
            break

        async def _dispatch_one(plan: PlanRecord, choice: CascadeChoice) -> TaskOutcome:
            # PR rescue: append the new feedback to the plan body and stamp
            # `pr_last_reconciled_at` BEFORE the dispatch claim. The agent
            # picks up the amended plan in its SCOPE step.
            if choice.source == "pr_rescue" and choice.pr_feedback:
                amended = append_pr_feedback(plan.path, list(choice.pr_feedback))
                plan = amended  # re-read happens inside run_one_task too
            async with semaphore:
                outcome = await run_one_task(
                    root=root_path,
                    host=host,
                    plan=plan,
                    timeout_per_task_s=cfg.timeout_per_task_s,
                    base_branch=cfg.base_branch,
                    branch_prefix=cfg.branch_prefix,
                    worktree_root=cfg.worktree_root,
                    git_runner=git_runner,
                    cascade_choice=choice,
                )
            # Stamp the cascade source onto the outcome.
            return TaskOutcome(
                plan_path=outcome.plan_path,
                worktree=outcome.worktree,
                headless=outcome.headless,
                cascade_source=choice.source,
                final_status=outcome.final_status,
                error=outcome.error,
            )

        batch_outcomes = await asyncio.gather(
            *(_dispatch_one(plan, choice) for plan, choice in batch),
            return_exceptions=False,
        )
        results.extend(batch_outcomes)

    return results
