"""Tests for the Phase 8 loop driver."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from nightly_core.contract import AuthStatus, HostId, InstallScope, SubAgentResult
from nightly_core.contract import NightlyHostIntegration as _Integration
from nightly_core.driver import (
    DriverConfig,
    TaskOutcome,
    build_task_prompt,
    run_loop,
    run_one_task,
)
from nightly_core.headless import HeadlessResult
from nightly_core.plans import read_plan, update_plan_status
from nightly_core.runs import new_task, start_run

# ── test doubles ──────────────────────────────────────────────────────────


class _FakeHost(_Integration):
    """In-process fake that yields a controllable HeadlessResult."""

    host_id: HostId = "claude"

    def __init__(self, *, results: list[HeadlessResult]) -> None:
        self._results = list(results)
        self.dispatch_calls: list[dict[str, Any]] = []

    async def install(self, scope: InstallScope) -> None:
        return None

    async def uninstall(self, scope: InstallScope) -> None:
        return None

    def is_installed(self, scope: InstallScope) -> bool:
        return False

    def session_id(self) -> str:
        return "fake"

    async def dispatch_sub_agent(self, **_: object) -> SubAgentResult:
        raise NotImplementedError

    async def request_approval(self, q: str, choices: list[str]) -> str:
        raise NotImplementedError

    async def auth_status(self) -> AuthStatus:
        return AuthStatus(ok=True)

    async def run_headless(self, prompt, *, cwd=None, timeout_s=None) -> HeadlessResult:  # type: ignore[override]
        # Record what was dispatched so tests can assert per-task prompts.
        self.dispatch_calls.append({"prompt": prompt, "cwd": cwd, "timeout_s": timeout_s})
        # Pop sequentially so the i-th dispatch returns the i-th result.
        if self._results:
            return self._results.pop(0)
        return HeadlessResult(host_id=self.host_id, output="ok", exit_code=0, elapsed_ms=0)


def _ok_result(host_id: str = "claude") -> HeadlessResult:
    return HeadlessResult(host_id=host_id, output='{"ok":true}', exit_code=0, elapsed_ms=1)


def _fail_result(host_id: str = "claude") -> HeadlessResult:
    return HeadlessResult(
        host_id=host_id,
        output="",
        stderr="boom",
        exit_code=1,
        elapsed_ms=1,
    )


def _git_runner_factory() -> tuple[Any, dict[str, Any]]:
    """Build a git runner that always succeeds and captures every call."""
    captured: dict[str, Any] = {"calls": []}

    async def runner(args: Sequence[str], cwd: Path | None) -> tuple[bytes, bytes, int]:
        captured["calls"].append((list(args), cwd))
        # No-op git is fine for driver tests; we don't inspect repo state.
        return b"", b"", 0

    return runner, captured


# ── build_task_prompt ─────────────────────────────────────────────────────


def test_build_task_prompt_includes_plan_body_and_constraints(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha", description="Fix the login bug")
    plan = read_plan(task.path / "plan.md")

    prompt = build_task_prompt(plan, task.path)

    # Plan body inlined
    assert "Fix the login bug" in prompt
    # Load-bearing constraints repeated
    assert "Refusal policy" in prompt
    assert "uncertainty.md" in prompt
    assert "proposal.md" in prompt
    assert "status: done" in prompt
    assert "status: parked" in prompt
    # Paths surfaced explicitly
    assert str(plan.path) in prompt
    assert str(task.path) in prompt
    # New post-Phase-9l directives
    assert "nightly verify" in prompt
    assert "nightly ci" in prompt
    assert "recommendation" in prompt


def test_build_task_prompt_fallback_says_no_pr(tmp_path: Path) -> None:
    """ideate_fallback prompts must explicitly tell the agent: no PR."""
    from nightly_core.cascade import CascadeChoice

    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    plan = read_plan(task.path / "plan.md")
    choice = CascadeChoice(
        source="ideate_fallback",
        summary="work on proposed (fallback): apply ruff fix",
        rationale="below auto-PR bar, dispatching as local proposal",
    )

    prompt = build_task_prompt(plan, task.path, cascade_choice=choice)
    assert "LOCAL PROPOSAL" in prompt or "no PR" in prompt.lower()
    assert (
        "do **not** open a pull request" in prompt.lower() or "do not open a pr" in prompt.lower()
    )
    # Strict-ideate prompt (no choice) should NOT carry the downgrade
    strict_prompt = build_task_prompt(plan, task.path)
    assert "LOCAL PROPOSAL" not in strict_prompt


def _stamp_depends_on_pr(plan_path: Path, pr_number: int) -> None:
    """Inject `depends_on_pr: N` into an existing plan's frontmatter."""
    from nightly_core.plans import parse_frontmatter, render_frontmatter

    text = plan_path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    metadata["depends_on_pr"] = str(pr_number)
    plan_path.write_text(render_frontmatter(metadata, body), encoding="utf-8")


def test_build_task_prompt_omits_declared_dependency_when_unset(tmp_path: Path) -> None:
    """Plans without `depends_on_pr` must not carry the declaration section."""
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    plan = read_plan(task.path / "plan.md")
    prompt = build_task_prompt(plan, task.path)
    assert "Declared dependency" not in prompt
    assert "Depends on #" not in prompt


def test_build_task_prompt_injects_declared_dependency(tmp_path: Path) -> None:
    """Plans with `depends_on_pr: N` must include the declaration + PR-body directive."""
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    _stamp_depends_on_pr(task.path / "plan.md", 54)
    plan = read_plan(task.path / "plan.md")

    prompt = build_task_prompt(plan, task.path)
    assert "Declared dependency" in prompt
    assert "base = PR #54" in prompt
    # The literal directive the agent must copy into the PR body.
    assert "Depends on #54" in prompt
    # Explains the why so the agent doesn't second-guess it.
    assert "RFC 004" in prompt


def test_build_task_prompt_declared_dependency_suppressed_for_local_proposal(
    tmp_path: Path,
) -> None:
    """A declared dependency on a fallback (LOCAL PROPOSAL) task should be
    suppressed — fallback tasks land locally, never open a PR, so the
    `Depends on #N` line has no PR body to attach to."""
    from nightly_core.cascade import CascadeChoice

    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    _stamp_depends_on_pr(task.path / "plan.md", 54)
    plan = read_plan(task.path / "plan.md")
    choice = CascadeChoice(
        source="ideate_fallback",
        summary="fallback",
        rationale="below bar",
    )
    prompt = build_task_prompt(plan, task.path, cascade_choice=choice)
    assert "LOCAL PROPOSAL" in prompt
    # Declaration section must not appear — the PR body line would be moot.
    assert "Declared dependency" not in prompt


# ── run_one_task ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_one_task_happy_path(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    plan = read_plan(task.path / "plan.md")

    host = _FakeHost(results=[_ok_result()])
    git_runner, _ = _git_runner_factory()

    outcome = await run_one_task(
        root=tmp_path,
        host=host,
        plan=plan,
        git_runner=git_runner,
    )
    assert isinstance(outcome, TaskOutcome)
    assert outcome.error is None
    assert outcome.headless is not None
    assert outcome.headless.ok
    # Plan status reconciled to `done` because the agent didn't update it
    # itself and the headless run succeeded.
    assert outcome.final_status == "done"
    assert read_plan(plan.path).status == "done"
    # Host saw the right cwd (the worktree path, not the original root)
    assert host.dispatch_calls[0]["cwd"] is not None


@pytest.mark.asyncio
async def test_run_one_task_failure_parks_plan(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    plan = read_plan(task.path / "plan.md")

    host = _FakeHost(results=[_fail_result()])
    git_runner, _ = _git_runner_factory()

    outcome = await run_one_task(
        root=tmp_path,
        host=host,
        plan=plan,
        git_runner=git_runner,
    )
    assert outcome.headless is not None
    assert not outcome.headless.ok
    # Failed headless → plan parked
    assert outcome.final_status == "parked"


@pytest.mark.asyncio
async def test_run_one_task_respects_agent_status_update(tmp_path: Path) -> None:
    """If the agent updated plan.md to a terminal status, don't override."""
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    plan_path = task.path / "plan.md"

    class _StatusFlippingHost(_FakeHost):
        async def run_headless(self, prompt, *, cwd=None, timeout_s=None):  # type: ignore[override]
            # Simulate the agent updating the plan during the run.
            update_plan_status(plan_path, "done")
            return await super().run_headless(prompt, cwd=cwd, timeout_s=timeout_s)

    host = _StatusFlippingHost(results=[_fail_result()])  # would otherwise park
    git_runner, _ = _git_runner_factory()

    outcome = await run_one_task(
        root=tmp_path,
        host=host,
        plan=read_plan(plan_path),
        git_runner=git_runner,
    )
    # Agent already said done — driver respects that even though headless failed.
    assert outcome.final_status == "done"


@pytest.mark.asyncio
async def test_run_one_task_recovers_from_dispatch_exception(tmp_path: Path) -> None:
    """A raise in run_headless lands the plan as `parked`, not crashing the loop."""
    run = start_run(tmp_path)
    task = new_task(run, slug="alpha")
    plan = read_plan(task.path / "plan.md")

    class _CrashingHost(_FakeHost):
        async def run_headless(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("simulated crash")

    host = _CrashingHost(results=[])
    git_runner, _ = _git_runner_factory()

    outcome = await run_one_task(
        root=tmp_path,
        host=host,
        plan=plan,
        git_runner=git_runner,
    )
    assert outcome.error is not None
    assert "simulated crash" in outcome.error
    assert outcome.final_status == "parked"
    assert outcome.headless is None


# ── run_loop ──────────────────────────────────────────────────────────────


def _seed_in_progress_plans(root: Path, slugs: list[str]) -> None:
    """Create a run and seed it with N tasks, each flipped to in_progress.

    in_progress is what the cascade's `resume_in_flight` step matches on,
    so the driver will pick these up in cascade order.
    """
    run = start_run(root)
    for slug in slugs:
        task = new_task(run, slug=slug)
        update_plan_status(task.path / "plan.md", "in_progress")


@pytest.mark.asyncio
async def test_run_loop_serial_dispatches_each_task_once(tmp_path: Path) -> None:
    _seed_in_progress_plans(tmp_path, ["alpha", "beta", "gamma"])
    host = _FakeHost(results=[_ok_result(), _ok_result(), _ok_result()])
    git_runner, _ = _git_runner_factory()

    outcomes = await run_loop(
        root=tmp_path,
        host=host,
        config=DriverConfig(concurrency=1),
        git_runner=git_runner,
    )
    assert len(outcomes) == 3
    assert all(o.final_status == "done" for o in outcomes)
    # Each dispatched exactly once
    assert len(host.dispatch_calls) == 3


@pytest.mark.asyncio
async def test_run_loop_respects_max_tasks(tmp_path: Path) -> None:
    _seed_in_progress_plans(tmp_path, ["a", "b", "c", "d", "e"])
    host = _FakeHost(results=[_ok_result()] * 5)
    git_runner, _ = _git_runner_factory()

    outcomes = await run_loop(
        root=tmp_path,
        host=host,
        config=DriverConfig(max_tasks=2),
        git_runner=git_runner,
    )
    assert len(outcomes) == 2


@pytest.mark.asyncio
async def test_run_loop_respects_conclude_marker(tmp_path: Path) -> None:
    """CONCLUDE on disk causes the next batch boundary to break the loop."""
    _seed_in_progress_plans(tmp_path, ["a", "b", "c"])
    run_id = (tmp_path / ".nightly" / "runs" / "CURRENT").read_text().strip()
    conclude = tmp_path / ".nightly" / "runs" / run_id / "CONCLUDE"
    conclude.write_text("", encoding="utf-8")  # mark concluded immediately

    host = _FakeHost(results=[_ok_result()] * 3)
    git_runner, _ = _git_runner_factory()

    outcomes = await run_loop(
        root=tmp_path,
        host=host,
        config=DriverConfig(concurrency=1),
        git_runner=git_runner,
    )
    # Loop saw CONCLUDE before picking up any task
    assert outcomes == []


@pytest.mark.asyncio
async def test_run_loop_concurrency_batches(tmp_path: Path) -> None:
    """concurrency=2 should pick up two tasks per batch."""
    _seed_in_progress_plans(tmp_path, ["a", "b", "c", "d"])
    host = _FakeHost(results=[_ok_result()] * 4)
    git_runner, _ = _git_runner_factory()

    outcomes = await run_loop(
        root=tmp_path,
        host=host,
        config=DriverConfig(concurrency=2),
        git_runner=git_runner,
    )
    assert len(outcomes) == 4
    # All four completed
    assert all(o.final_status == "done" for o in outcomes)


@pytest.mark.asyncio
async def test_run_loop_concurrency_actually_parallel(tmp_path: Path) -> None:
    """With concurrency=2, two slow dispatches should run together (not sequentially).

    We simulate slow dispatch with `asyncio.sleep(0.05)`. Serial run would
    take ≥ 0.2s for four tasks; concurrent run with N=2 should be ≤ 0.15s.
    """
    _seed_in_progress_plans(tmp_path, ["a", "b", "c", "d"])

    class _SlowHost(_FakeHost):
        async def run_headless(self, prompt, *, cwd=None, timeout_s=None):  # type: ignore[override]
            await asyncio.sleep(0.05)
            return await super().run_headless(prompt, cwd=cwd, timeout_s=timeout_s)

    host = _SlowHost(results=[_ok_result()] * 4)
    git_runner, _ = _git_runner_factory()

    import time

    start = time.monotonic()
    outcomes = await run_loop(
        root=tmp_path,
        host=host,
        config=DriverConfig(concurrency=2),
        git_runner=git_runner,
    )
    elapsed = time.monotonic() - start

    assert len(outcomes) == 4
    # 4 tasks * 50ms = 200ms serial; concurrency=2 should be ~100-150ms.
    assert elapsed < 0.18, f"expected concurrent dispatch < 0.18s, got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_run_loop_terminates_when_cascade_empty(tmp_path: Path) -> None:
    """A repo with no in-flight plans → loop returns immediately."""
    start_run(tmp_path)  # init the run but seed no tasks
    host = _FakeHost(results=[])
    git_runner, _ = _git_runner_factory()

    outcomes = await run_loop(
        root=tmp_path,
        host=host,
        config=DriverConfig(),
        git_runner=git_runner,
    )
    assert outcomes == []
    # No dispatch happened
    assert len(host.dispatch_calls) == 0


@pytest.mark.asyncio
async def test_run_loop_stamps_cascade_source(tmp_path: Path) -> None:
    """Each TaskOutcome should carry the cascade source that fired."""
    _seed_in_progress_plans(tmp_path, ["alpha"])
    host = _FakeHost(results=[_ok_result()])
    git_runner, _ = _git_runner_factory()

    outcomes = await run_loop(
        root=tmp_path,
        host=host,
        config=DriverConfig(),
        git_runner=git_runner,
    )
    assert len(outcomes) == 1
    assert outcomes[0].cascade_source == "resume_in_flight"


# ── Fix 2: materialize_proposal_as_plan stamps the fingerprint ────────────


def test_materialize_proposal_as_plan_writes_fingerprint(tmp_path: Path) -> None:
    """When the driver materializes a proposal into a plan, the plan
    must carry the proposal's fingerprint in its frontmatter — otherwise
    the cascade dedupe (issue #2) has no signal to skip duplicates on
    the next pass."""
    from nightly_core.driver import _materialize_proposal_as_plan
    from nightly_core.plans import PROPOSER_FINGERPRINT_KEY, read_plan
    from nightly_core.proposers.base import Proposal

    start_run(tmp_path)
    proposal = Proposal(
        proposer="lint_debt",
        category="lint_debt",
        title="Apply autofixable F401",
        body="# body",
        score=4.0,
        file_scope=("src/x.py",),
        estimated_loc=4,
    )
    plan = _materialize_proposal_as_plan(root=tmp_path, proposal=proposal, source="ideate")
    assert plan is not None
    fresh = read_plan(plan.path)
    assert fresh.metadata.get(PROPOSER_FINGERPRINT_KEY) == proposal.fingerprint
    assert fresh.proposer_fingerprint == "lint_debt:lint_debt:src/x.py"
