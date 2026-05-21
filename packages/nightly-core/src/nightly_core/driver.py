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
from nightly_core.paths import repo_root
from nightly_core.plans import (
    PlanRecord,
    PlanStatus,
    read_plan,
    update_plan_status,
)
from nightly_core.runs import Run, current_run
from nightly_core.worktree import GitRunner, WorktreeHandle, create_worktree

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


def build_task_prompt(plan: PlanRecord, task_dir: Path) -> str:
    """Build a focused prompt to send to `host.run_headless` for one task.

    Headless invocations don't pre-load the Skill the way an interactive
    session does, so we inline the load-bearing constraints right in the
    prompt. The body of `plan.md` already carries the task's success
    criteria and file scope.
    """
    return f"""\
You are Nightly running headlessly on a single task. Your current working
directory is a fresh git worktree forked from the project's main branch.

**Task plan:** `{plan.path}`
**Task folder:** `{task_dir}`

## What to do

1. Read the plan body below carefully.
2. Implement the change inside this worktree. Edit only files in the
   plan's declared file scope.
3. Run the project's local checks (`make check`, `pytest`, etc.) and
   confirm they pass before declaring done.
4. Write `proposal.md` to `{task_dir}` summarizing what you changed,
   the test plan, and any risks.
5. Write `uncertainty.md` to `{task_dir}` with non-empty sections:
   *Things I'm not sure about*, *Things that could break*,
   *Things I skipped on purpose*, *Approval needed for*.
6. Update the plan's YAML frontmatter: `status: done` on a clean land,
   `status: parked` if you couldn't complete it.

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
    git_runner: GitRunner | None = None,
) -> TaskOutcome:
    """Claim → create worktree → dispatch → reconcile status.

    Errors are caught and surfaced via `TaskOutcome.error` so the loop
    driver can keep going. The plan's status is always restored to a
    terminal state (`done` or `parked`) even if the agent crashed
    mid-task.

    Claim is done via the transient `dispatching` status, which the
    cascade explicitly skips — that's how multi-task batches avoid
    re-picking the same plan within one batch.
    """
    update_plan_status(plan.path, "dispatching")

    worktree: WorktreeHandle | None = None
    headless: HeadlessResult | None = None
    error: str | None = None

    try:
        worktree = await create_worktree(
            root,
            slug=plan.slug,
            base_branch=base_branch,
            branch_prefix=branch_prefix,
            runner=git_runner,
        )
        prompt = build_task_prompt(plan, plan.path.parent)
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


async def _pick_batch(
    root: Path,
    batch_size: int,
) -> list[tuple[PlanRecord, CascadeChoice]]:
    """Pull up to `batch_size` tasks from the cascade, claiming each.

    Returns (plan, cascade_choice) tuples. May return fewer than
    `batch_size` if the cascade runs out. The cascade source is
    preserved on the choice for the outcome record.
    """
    out: list[tuple[PlanRecord, CascadeChoice]] = []
    for _ in range(batch_size):
        choice = next_task(root)
        if choice.source == "nothing":
            break
        # For Phase 8 we only dispatch tasks that have a concrete plan
        # file on disk (resume_in_flight / unblocked_approval). The other
        # cascade sources (accepted_rfc, github_issue, ideate) need a
        # plan-creation step we don't ship in this phase.
        if choice.target_path is None or not choice.target_path.is_file():
            _log.info(
                "cascade returned %s but no plan file to dispatch (target=%s); "
                "skipping for Phase 8",
                choice.source,
                choice.target_path,
            )
            break
        plan = read_plan(choice.target_path)
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

        async def _dispatch_one(plan: PlanRecord, source: str) -> TaskOutcome:
            async with semaphore:
                outcome = await run_one_task(
                    root=root_path,
                    host=host,
                    plan=plan,
                    timeout_per_task_s=cfg.timeout_per_task_s,
                    base_branch=cfg.base_branch,
                    branch_prefix=cfg.branch_prefix,
                    git_runner=git_runner,
                )
            # Stamp the cascade source onto the outcome.
            return TaskOutcome(
                plan_path=outcome.plan_path,
                worktree=outcome.worktree,
                headless=outcome.headless,
                cascade_source=source,
                final_status=outcome.final_status,
                error=outcome.error,
            )

        batch_outcomes = await asyncio.gather(
            *(_dispatch_one(plan, choice.source) for plan, choice in batch),
            return_exceptions=False,
        )
        results.extend(batch_outcomes)

    return results
