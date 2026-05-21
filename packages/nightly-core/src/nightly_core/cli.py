"""Nightly CLI entry point.

Commands as of Phase 8:
- `nightly version`     — print version
- `nightly info`        — short status / phase summary
- `nightly init`        — bootstrap .nightly/ + install host launcher
- `nightly status`      — report what Nightly knows about this repo
- `nightly uninstall`   — remove the host launcher
- `nightly start`       — create a new run (optionally seed first task)
- `nightly conclude`    — mark the current run as concluding (non-blocking)
- `nightly task`        — create a new task inside the current run
- `nightly specialist`  — print the system prompt for a specialist role
- `nightly brief`       — render briefing.html for the current or named run
- `nightly next`        — walk the priority cascade and recommend the next task
- `nightly triage`      — print ranked open GitHub issues
- `nightly plans`       — list every plan across runs with status
- `nightly propose`     — dry-run the proposer suite; list ideation candidates
- `nightly ideate`      — run proposers and write draft issues to disk
- `nightly headless`    — spawn a host CLI non-interactively (cron / CI)
- `nightly run`         — drive the cascade headless; multi-task parallel
- `nightly feedback`    — show PR feedback for a branch (default: HEAD)
- `nightly rescue`      — preview the next pr_rescue candidate without dispatching
- `nightly keepalive`   — print think-harder strategies when the cascade is empty

This is the planned-phase CLI surface complete (Phases 0-8).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from nightly_core._version import __version__
from nightly_core.autonomy import can_auto_pr
from nightly_core.briefing import write_briefing
from nightly_core.cascade import next_task as cascade_next
from nightly_core.cascade import pick_pr_rescue
from nightly_core.contract import (
    HostId,
    InstallScope,
    NightlyHostIntegration,
    SpecialistRole,
)
from nightly_core.driver import DriverConfig, run_loop
from nightly_core.ideation import run_proposers, write_drafts
from nightly_core.keepalive import KEEPALIVE_STRATEGIES, pick_keepalive
from nightly_core.paths import nightly_dir, planning_dir, repo_root, run_dir
from nightly_core.plans import append_pr_feedback, list_plans
from nightly_core.pr_feedback import fetch_feedback
from nightly_core.rules import seed_rules
from nightly_core.runs import (
    conclude_run,
    current_run,
    list_runs,
    new_task,
    start_run,
)
from nightly_core.specialists import specialist_prompt
from nightly_core.triage import rank_issues

app = typer.Typer(
    name="nightly",
    help="Nightly — continuously-running, host-native coding agent.",
    no_args_is_help=True,
    add_completion=False,
)


_DEFAULT_CONFIG_YML = """\
# .nightly/config.yml — written by `nightly init`. Edit as needed.
# See `.nightly/config.yml.example` (if present) for the full schema, or
# `.planning/brainstorm.html` §05 for the design rationale.

hosts:
  - claude

git:
  branch_prefix: nightly/
  wip_prefix:    nightly/wip-
  protected:     [main, master, "release/*"]

refuse:
  destructive_git:        true
  production_state:       true
  external_communication: true
  network_egress_unknown: true
  scope_creep:            true
  bypass_test_or_type:    true

# pr_feedback governs the `pr_rescue` cascade step (Phase 9).
# - `enabled` flips the whole feature off without removing the block.
# - `review_bots` extends the default bot allowlist (CodeRabbit, Cursor BugBot,
#   Copilot reviewer, Greptile, Amp, etc.) with project-specific accounts.
# - `treat_bots_as_human` flips a bot login into the "human" bucket — useful
#   for an internally-trusted automation that should outrank ordinary bots.
pr_feedback:
  enabled:              true
  review_bots:          []
  treat_bots_as_human:  []
"""


_NIGHTLY_SUBDIRS: tuple[str, ...] = ("runs", "plans", "atlas", "memory", "prompts")

# Display tuning for `nightly triage` — wider issue titles get elided.
_TRIAGE_TITLE_MAX = 50
_TRIAGE_TITLE_ELIDE_AT = 47


# Hosts implemented so far. Cursor + Antigravity land in Phase 6 and will
# be added to this set then. Each loader is a thin lambda that lazy-imports
# its host package so nightly-core never depends on its sub-packages at
# load time (the sub-packages depend on nightly-core — would be a cycle).
_HOST_LOADERS: dict[HostId, Callable[[Path | None], NightlyHostIntegration]] = {}


def _register_host_loaders() -> None:
    """Populate `_HOST_LOADERS` with lazy importers for every supported host."""

    def _claude(root: Path | None) -> NightlyHostIntegration:
        from nightly_host_claude import ClaudeHostIntegration  # noqa: PLC0415

        return ClaudeHostIntegration(root=root)

    def _codex(root: Path | None) -> NightlyHostIntegration:
        from nightly_host_codex import CodexHostIntegration  # noqa: PLC0415

        return CodexHostIntegration(root=root)

    def _opencode(root: Path | None) -> NightlyHostIntegration:
        from nightly_host_opencode import OpencodeHostIntegration  # noqa: PLC0415

        return OpencodeHostIntegration(root=root)

    def _cursor(root: Path | None) -> NightlyHostIntegration:
        from nightly_host_cursor import CursorHostIntegration  # noqa: PLC0415

        return CursorHostIntegration(root=root)

    def _antigravity(root: Path | None) -> NightlyHostIntegration:
        from nightly_host_antigravity import AntigravityHostIntegration  # noqa: PLC0415

        return AntigravityHostIntegration(root=root)

    _HOST_LOADERS["claude"] = _claude
    _HOST_LOADERS["codex"] = _codex
    _HOST_LOADERS["opencode"] = _opencode
    _HOST_LOADERS["cursor"] = _cursor
    _HOST_LOADERS["antigravity"] = _antigravity


_register_host_loaders()


def _load_host(host_id: HostId, root: Path | None = None) -> NightlyHostIntegration:
    """Look up and instantiate the host integration for `host_id`.

    Each loader does a lazy import so we never trigger a package load
    cycle. Hosts not in `_HOST_LOADERS` raise `BadParameter` cleanly with
    a pointer to the build plan in the brainstorm.
    """
    loader = _HOST_LOADERS.get(host_id)
    if loader is None:
        msg = (
            f"Host '{host_id}' is not yet implemented. "
            f"Phase 6 supports {sorted(_HOST_LOADERS)}. "
            "See .planning/brainstorm.html §11 for the build plan."
        )
        raise typer.BadParameter(msg)
    return loader(root)


def _bootstrap_nightly_dir(root: Path) -> tuple[Path, list[str]]:
    """Create .nightly/ folder shape if absent. Returns (path, created relpaths)."""
    nightly = nightly_dir(root)
    created: list[str] = []
    for sub in _NIGHTLY_SUBDIRS:
        path = nightly / sub
        if not path.exists():
            path.mkdir(parents=True)
            created.append(str(path.relative_to(root)))
    return nightly, created


def _ensure_config(nightly: Path) -> bool:
    """Write a default config.yml if absent. Returns True if it was written."""
    config = nightly / "config.yml"
    if config.exists():
        return False
    config.write_text(_DEFAULT_CONFIG_YML, encoding="utf-8")
    return True


def _format_path_for_display(path: Path, root: Path) -> str:
    """Show repo-relative if possible, else absolute (for user-scope paths)."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _current_branch(root: Path) -> str | None:
    """Best-effort `git branch --show-current`. None if git missing or detached HEAD."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    name = result.stdout.strip()
    return name or None


def _require_current_run(root: Path):
    """Get the current run or exit cleanly with a helpful message."""
    run = current_run(root)
    if run is None:
        typer.echo(
            "no active run — start one with `nightly start [task description]`",
            err=True,
        )
        raise typer.Exit(code=1)
    return run


# ── commands ──────────────────────────────────────────────────────────────


@app.command()
def version() -> None:
    """Print the Nightly version."""
    typer.echo(f"nightly {__version__}")


@app.command()
def info() -> None:
    """Brief intro and current phase."""
    typer.echo(f"Nightly {__version__} — Phases 0-8 complete.")
    typer.echo("Run `nightly init` to install the host skill; then ask Nightly")
    typer.echo("in your host to run a task — or use `nightly run` for headless.")
    typer.echo("See .planning/brainstorm.html for the design.")


@app.command()
def init(
    host: Annotated[HostId, typer.Option(help="Host to install Nightly into.")] = "claude",
    scope: Annotated[
        InstallScope,
        typer.Option(help="Install at repo-local 'project' scope or user-global 'user' scope."),
    ] = "project",
    rules: Annotated[
        bool,
        typer.Option(
            "--rules/--no-rules",
            help="Seed Nightly's autonomy contract into AGENTS.md + CLAUDE.md. Default: on.",
        ),
    ] = True,
) -> None:
    """Bootstrap .nightly/, write default config, install the host launcher."""
    root = repo_root()
    typer.echo(f"repo: {root}")

    _, created = _bootstrap_nightly_dir(root)
    for d in created:
        typer.echo(f"  ✓ created {d}/")
    if not created:
        typer.echo("  · .nightly/ scaffold already present")

    nightly = nightly_dir(root)
    if _ensure_config(nightly):
        typer.echo(f"  ✓ wrote {_format_path_for_display(nightly / 'config.yml', root)} (defaults)")
    else:
        typer.echo(f"  · {_format_path_for_display(nightly / 'config.yml', root)} already present")

    integration = _load_host(host, root=root)
    asyncio.run(integration.install(scope))
    target = integration.skill_path(scope)  # type: ignore[attr-defined]
    typer.echo(f"  ✓ installed {host} skill ({scope}) at {_format_path_for_display(target, root)}")

    if rules:
        for outcome in seed_rules(root):
            verb = {
                "created": "✓ created",
                "updated": "✓ updated",
                "unchanged": "·",
                "skipped": "·",
            }[outcome.action]
            rel = _format_path_for_display(outcome.path, root)
            note = (
                "(nightly autonomy contract)"
                if outcome.action in {"created", "updated"}
                else "already current"
            )
            typer.echo(f"  {verb} {rel} {note}")

    typer.echo("")
    typer.echo("→ Open your host in this repo and ask Nightly to run a task on something.")


@app.command()
def uninstall(
    host: Annotated[HostId, typer.Option(help="Host to uninstall from.")] = "claude",
    scope: Annotated[InstallScope, typer.Option(help="Scope to remove from.")] = "project",
) -> None:
    """Remove the host launcher (Skill / command / agent file)."""
    root = repo_root()
    integration = _load_host(host, root=root)
    target = integration.skill_path(scope)  # type: ignore[attr-defined]
    if not integration.is_installed(scope):
        typer.echo(
            f"· {host} skill not installed at {scope} scope "
            f"({_format_path_for_display(target, root)})"
        )
        return
    asyncio.run(integration.uninstall(scope))
    typer.echo(f"✓ removed {host} skill ({scope}) from {_format_path_for_display(target, root)}")


@app.command()
def status() -> None:
    """Report what Nightly knows about this repo."""
    root = repo_root()
    nightly = nightly_dir(root)
    planning = planning_dir(root)

    typer.echo(f"nightly {__version__}")
    typer.echo(f"  repo:      {root}")
    typer.echo(
        f"  .nightly/:  {'✓ present' if nightly.exists() else '✗ missing — run nightly init'}"
    )
    typer.echo(f"  .planning/: {'✓ present' if planning.exists() else '· absent (optional)'}")

    typer.echo("  hosts:")
    for hid in sorted(_HOST_LOADERS):
        integration = _load_host(hid, root=root)
        for scope in ("project", "user"):
            mark = "✓" if integration.is_installed(scope) else "✗"
            path = integration.skill_path(scope)  # type: ignore[attr-defined]
            typer.echo(f"    {hid:<10} {scope:<7} {mark} {_format_path_for_display(path, root)}")

    typer.echo("  runs:")
    run = current_run(root)
    if run is None:
        typer.echo("    · no active run (start with `nightly start`)")
    else:
        marker = "concluded" if run.is_concluded else "active"
        typer.echo(f"    ✓ {run.id}  [{marker}]")
    all_runs = list_runs(root)
    if len(all_runs) > 1:
        typer.echo(f"    ({len(all_runs)} run(s) total)")


# ── Phase 2 commands ──────────────────────────────────────────────────────


@app.command()
def start(
    task: Annotated[
        str | None,
        typer.Argument(help="Optional task description; if given, seeds tasks/0001-<slug>/."),
    ] = None,
) -> None:
    """Create a new run and update .nightly/runs/CURRENT.

    Per the always-advance principle, starting a new run while another is
    active is allowed — the old run remains on disk and the CURRENT pointer
    moves. To formally end the prior run, use `nightly conclude` first.
    """
    root = repo_root()
    if not nightly_dir(root).is_dir():
        typer.echo("repo not initialized — run `nightly init` first", err=True)
        raise typer.Exit(code=1)

    run = start_run(root, task=task)
    typer.echo(f"✓ started run {run.id}")
    if task:
        first_task = run.path / "tasks"
        seeded = next(iter(first_task.iterdir()), None)
        if seeded is not None:
            typer.echo(
                f"✓ seeded task {seeded.name} (plan stub at "
                f"{_format_path_for_display(seeded / 'plan.md', root)})"
            )
    typer.echo("→ Open Claude Code in this repo; the Nightly skill picks it up from disk.")


@app.command()
def conclude() -> None:
    """Mark the current run as concluding. Does not block — drains naturally."""
    root = repo_root()
    run = current_run(root)
    if run is None:
        typer.echo("· no active run to conclude", err=True)
        raise typer.Exit(code=1)
    if run.is_concluded:
        typer.echo(f"· run {run.id} is already concluded")
        return
    conclude_run(root)
    typer.echo(f"✓ run {run.id} marked concluding")
    typer.echo("  The agent will finish its current task (or stash WIP), render the briefing,")
    typer.echo("  and exit. Never SIGKILL — always advance.")


@app.command()
def task(
    slug: Annotated[str, typer.Argument(help="Task slug (lowercase, dashes).")],
    description: Annotated[
        str | None,
        typer.Option("--description", "-d", help="One-line task description for plan.md."),
    ] = None,
) -> None:
    """Create a new task under the current run."""
    root = repo_root()
    run = _require_current_run(root)
    created = new_task(run, slug=slug, description=description)
    typer.echo(
        f"✓ task {created.path.name} ready at {_format_path_for_display(created.path, root)}"
    )


@app.command()
def specialist(
    role: Annotated[
        SpecialistRole,
        typer.Argument(help="Specialist role: implementer | tester | reviewer | researcher."),
    ],
) -> None:
    """Print the system prompt for a specialist role.

    Inside Claude Code, the Nightly skill uses this to seed a Task-tool
    sub-agent with the right role-specific instructions.
    """
    typer.echo(specialist_prompt(role), nl=False)


@app.command()
def brief(
    run_id: Annotated[
        str | None,
        typer.Option("--run", help="Specific run id; default is the current run."),
    ] = None,
) -> None:
    """Render briefing.html for a run (current by default)."""
    root = repo_root()
    if run_id is None:
        run = _require_current_run(root)
    else:
        path = run_dir(run_id, root)
        if not path.is_dir():
            typer.echo(f"no such run: {run_id}", err=True)
            raise typer.Exit(code=1)
        from nightly_core.runs import Run  # noqa: PLC0415 - local to avoid circulars

        run = Run(id=run_id, path=path, is_concluded=(path / "CONCLUDE").is_file())
    target = write_briefing(run)
    typer.echo(f"✓ rendered {_format_path_for_display(target, root)}")


@app.command(name="next")
def show_next() -> None:
    """Walk the priority cascade and recommend the next task.

    Prints the matched cascade step, a one-line summary, the target path
    (if any), and a fuller rationale. The agent uses this to decide what
    to work on next without having to inspect the on-disk state itself.
    """
    root = repo_root()
    choice = cascade_next(root)
    typer.echo(f"source:   {choice.source}")
    typer.echo(f"summary:  {choice.summary}")
    if choice.target_path is not None:
        typer.echo(f"target:   {_format_path_for_display(choice.target_path, root)}")
    if choice.score is not None:
        typer.echo(f"score:    {choice.score:.2f}")
    if choice.rationale:
        typer.echo("")
        typer.echo(choice.rationale)


@app.command()
def triage(
    top: Annotated[
        int,
        typer.Option("--top", "-n", help="Show only the top N ranked issues."),
    ] = 10,
) -> None:
    """Print ranked open GitHub issues.

    Requires the `gh` CLI and a GitHub remote. Returns nothing (empty
    output) when either is missing — triage is best-effort.
    """
    root = repo_root()
    rankings = rank_issues(root)
    if not rankings:
        typer.echo("· no open issues found (or `gh` CLI unavailable)")
        return
    shown = rankings[:top]
    typer.echo(f"{'score':>5}  {'issue':>5}  status  title")
    typer.echo("-" * 60)
    for r in shown:
        status = "skip" if r.skip_reason else " ok "
        suffix = f"  ({r.skip_reason})" if r.skip_reason else ""
        title = (
            r.title
            if len(r.title) < _TRIAGE_TITLE_MAX
            else r.title[:_TRIAGE_TITLE_ELIDE_AT] + "..."
        )
        typer.echo(f"{r.score:>5.2f}  #{r.number:<5} [{status}]  {title}{suffix}")
    if len(rankings) > top:
        typer.echo(f"\n({len(rankings) - top} more — pass --top to widen)")


@app.command()
def plans() -> None:
    """List every plan across all runs with status."""
    root = repo_root()
    records = list_plans(root)
    if not records:
        typer.echo("· no plans found (start one with `nightly start <task>`)")
        return
    typer.echo(f"{'status':<20} {'run':<26} slug")
    typer.echo("-" * 60)
    for p in records:
        typer.echo(f"{p.status:<20} {p.run_id:<26} {p.slug}")


@app.command()
def propose(
    top: Annotated[
        int,
        typer.Option("--top", "-n", help="Show only the top N proposals."),
    ] = 20,
) -> None:
    """Run the proposer suite as a dry-run — list candidates without writing.

    Use `nightly ideate` to actually persist drafts under
    `<run>/proposed/issues/` for human review.
    """
    root = repo_root()
    proposals = run_proposers(root)
    if not proposals:
        typer.echo("· no proposals — every proposer came up empty")
        return
    typer.echo(f"{'score':>5}  {'auto':>5}  {'proposer':<14}  title")
    typer.echo("-" * 76)
    for proposal in proposals[:top]:
        mark = " ok" if can_auto_pr(proposal) else "skip"
        title = (
            proposal.title
            if len(proposal.title) < _TRIAGE_TITLE_MAX
            else proposal.title[:_TRIAGE_TITLE_ELIDE_AT] + "..."
        )
        typer.echo(f"{proposal.score:>5.2f}  {mark:>5}  {proposal.proposer:<14}  {title}")
    if len(proposals) > top:
        typer.echo(f"\n({len(proposals) - top} more — pass --top to widen)")


@app.command()
def run(
    host: Annotated[HostId, typer.Option(help="Which host to spawn for each task.")] = "claude",
    max_tasks: Annotated[
        int | None,
        typer.Option("--max-tasks", "-n", help="Stop after this many tasks. Default: unlimited."),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            "-j",
            help="Parallel dispatch limit per batch. 1 = strictly serial (default).",
        ),
    ] = 1,
    timeout_per_task: Annotated[
        float | None,
        typer.Option(
            "--timeout-per-task",
            help="Per-task headless timeout in seconds. Default: no timeout.",
        ),
    ] = None,
) -> None:
    """Drive the cascade in headless mode until exhaustion or `nightly conclude`.

    Walks the priority cascade, dispatches each task in an isolated git
    worktree via the host's non-interactive CLI, reconciles plan status,
    and loops. `--concurrency N` opt-in parallelism dispatches up to N
    tasks per batch via `asyncio.gather`.

    Single-process by contract — running two `nightly run` against the
    same repo can race on plan-status updates.
    """
    root = repo_root()
    integration = _load_host(host, root=root)
    cfg = DriverConfig(
        host_id=host,
        max_tasks=max_tasks,
        concurrency=max(1, concurrency),
        timeout_per_task_s=timeout_per_task,
    )

    outcomes = asyncio.run(run_loop(root=root, host=integration, config=cfg))

    if not outcomes:
        typer.echo("· no work dispatched (cascade returned `nothing` or CONCLUDE present)")
        return

    typer.echo(
        f"✓ dispatched {len(outcomes)} task(s) on host {host} (concurrency={cfg.concurrency}):"
    )
    for outcome in outcomes:
        slug = outcome.plan_path.parent.name
        elapsed = outcome.headless.elapsed_ms if outcome.headless else 0
        mark = "✓" if outcome.final_status == "done" else "·"
        typer.echo(
            f"  {mark} {slug:<40} {outcome.final_status:<10} "
            f"{outcome.cascade_source:<20} ({elapsed}ms)"
        )
        if outcome.error:
            typer.echo(f"    └─ error: {outcome.error}")


@app.command()
def headless(
    prompt: Annotated[
        str,
        typer.Argument(help="Prompt to send to the host's non-interactive CLI."),
    ],
    host: Annotated[HostId, typer.Option(help="Which host to spawn.")] = "claude",
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd", help="Working directory for the spawned host. Defaults to repo root."
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Wall-clock timeout in seconds. Default: no timeout."),
    ] = None,
) -> None:
    """Spawn a host's non-interactive CLI and print the result.

    Subscription credentials propagate through the environment — the
    spawned CLI reads its own cached creds from `~/.<host>/...`. Set the
    host's API key env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.)
    before invoking when running from a sandboxed CI environment.

    Phase 7 ships single-shot invocation. Phase 8 will wrap this in a
    cascade-driven loop (start → next → headless → land → brief).
    """
    root = repo_root()
    integration = _load_host(host, root=root)
    workdir = cwd or root

    result = asyncio.run(integration.run_headless(prompt, cwd=workdir, timeout_s=timeout))

    if not result.ok:
        typer.echo(
            f"✗ {host} headless run failed (exit {result.exit_code}, "
            f"{result.elapsed_ms}ms): {result.error or 'see stderr'}",
            err=True,
        )
        if result.stderr:
            typer.echo(result.stderr, err=True)
        raise typer.Exit(code=1)

    typer.echo(f"✓ {host} headless run ok ({result.elapsed_ms}ms)", err=True)
    # Output the host's stdout verbatim to stdout — typically JSON the
    # caller pipes into jq or saves to a file.
    typer.echo(result.output, nl=False)


@app.command()
def ideate() -> None:
    """Run the proposer suite and write draft issues to the current run.

    Writes one markdown file per proposal under
    `.nightly/runs/<id>/proposed/issues/`, ordered by score. The briefing
    surfaces them in the morning report. If any proposal clears the
    autonomy bar, the cascade will pick it on the next `nightly next`.
    """
    root = repo_root()
    run = _require_current_run(root)
    proposals = run_proposers(root)
    if not proposals:
        typer.echo("· no proposals — every proposer came up empty")
        return
    paths = write_drafts(run, proposals)
    auto_eligible = sum(1 for p in proposals if can_auto_pr(p))
    typer.echo(
        f"✓ wrote {len(paths)} proposal(s) to "
        f"{_format_path_for_display(run.path / 'proposed' / 'issues', root)}"
    )
    typer.echo(
        f"  {auto_eligible} auto-PR-eligible · {len(proposals) - auto_eligible} for human review"
    )
    typer.echo("→ run `nightly next` to pick the top auto-eligible one (if any).")


@app.command()
def feedback(
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Branch whose PR to inspect. Default: current HEAD."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help=(
                "When set, append a `## Feedback round N` section to the matching "
                "plan and stamp pr_last_reconciled_at. Default: dry-run (print only)."
            ),
        ),
    ] = False,
) -> None:
    """Show PR feedback for `branch` — reviews, inline comments, check failures.

    Best-effort: requires `gh` and a GitHub remote. Prints nothing if the
    branch has no PR or `gh` is unavailable. With `--apply`, writes the
    feedback into the plan body so the agent can act on it in the next
    `nightly run` cycle.
    """
    root = repo_root()
    target = branch or _current_branch(root)
    if not target:
        typer.echo(
            "no branch specified and could not determine current branch "
            "(detached HEAD or git unavailable)",
            err=True,
        )
        raise typer.Exit(code=1)

    items = fetch_feedback(target, root=root)
    if not items:
        typer.echo(f"· no feedback for branch '{target}' (or no PR / no gh)")
        return

    typer.echo(f"branch:  {target}")
    typer.echo(f"items:   {len(items)}")
    blocking = sum(1 for f in items if f.is_blocking)
    bot = sum(1 for f in items if f.author_is_bot)
    typer.echo(f"breakdown: {blocking} blocking · {len(items) - bot} human · {bot} bot")
    typer.echo("-" * 60)
    for f in items:
        flag = " ! " if f.is_blocking else "   "
        who = f"{f.author_login}{'  [bot]' if f.author_is_bot else ''}"
        locator = ""
        if f.file_ref:
            locator = f" @ {f.file_ref}"
            if f.line_ref:
                locator += f":{f.line_ref}"
        head = f.body.splitlines()[0] if f.body else ""
        if len(head) > _TRIAGE_TITLE_MAX:
            head = head[: _TRIAGE_TITLE_ELIDE_AT] + "..."
        typer.echo(f"{flag}{f.kind:<15} {who:<28}{locator}")
        typer.echo(f"   {head}")

    if apply:
        # Reuse the cascade's branch→plan matcher so `--apply` lands the
        # feedback on the same plan the cascade would have picked.
        from nightly_core.cascade import _match_plan_to_branch  # noqa: PLC0415

        plan = _match_plan_to_branch(target, root)
        if plan is None:
            typer.echo("", err=True)
            typer.echo(
                f"✗ could not match branch '{target}' to a plan — "
                "feedback not appended.",
                err=True,
            )
            raise typer.Exit(code=1)
        record = append_pr_feedback(plan.path, items)
        typer.echo("")
        typer.echo(
            f"✓ appended feedback to {_format_path_for_display(record.path, root)} "
            f"(stamped pr_last_reconciled_at)"
        )


@app.command()
def rescue() -> None:
    """Preview the next pr_rescue candidate without dispatching it.

    Same logic the cascade uses — finds the highest-priority Nightly-authored
    open PR with feedback newer than its plan's last reconcile stamp.
    Prints `None` if no PR has unaddressed feedback.
    """
    root = repo_root()
    candidate = pick_pr_rescue(root)
    if candidate is None:
        typer.echo("· no PR rescue candidate (no Nightly PRs with new feedback)")
        return
    typer.echo(f"branch:    {candidate.branch}")
    typer.echo(f"pr:        #{candidate.pr_number}  {candidate.pr_url}")
    typer.echo(f"summary:   {candidate.summary}")
    typer.echo(f"blocking:  {candidate.has_blocking}")
    if candidate.plan_path is not None:
        typer.echo(f"plan:      {_format_path_for_display(candidate.plan_path, root)}")
    else:
        typer.echo("plan:      (no match — agent must read PR fresh)")
    typer.echo("-" * 60)
    for f in candidate.feedback:
        flag = " ! " if f.is_blocking else "   "
        who = f"{f.author_login}{'  [bot]' if f.author_is_bot else ''}"
        head = f.body.splitlines()[0] if f.body else ""
        if len(head) > _TRIAGE_TITLE_MAX:
            head = head[: _TRIAGE_TITLE_ELIDE_AT] + "..."
        typer.echo(f"{flag}{f.kind:<15} {who:<28}  {head}")


@app.command()
def keepalive(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help=(
                "Print just one strategy by name (e.g. revive_parked). "
                "Default: print every strategy and highlight the recommended one."
            ),
        ),
    ] = None,
) -> None:
    """Print think-harder strategies for when the cascade returns `nothing`.

    Inspired by Karpathy's autoresearch NEVER STOP doctrine: when no
    obvious work remains, walk these strategies before rendering the
    briefing. With no flags, prints every strategy and marks the one
    `pick_keepalive` would auto-select. With `--name <slug>`, prints
    just that strategy's prompt (useful for piping into a sub-agent).
    """
    root = repo_root()
    if name is not None:
        for strategy in KEEPALIVE_STRATEGIES:
            if strategy.name == name:
                typer.echo(strategy.prompt)
                return
        typer.echo(
            f"unknown strategy: '{name}'. Available: "
            f"{', '.join(s.name for s in KEEPALIVE_STRATEGIES)}",
            err=True,
        )
        raise typer.Exit(code=1)

    recommended = pick_keepalive(root)
    rec_name = recommended.name if recommended is not None else None
    typer.echo("# Keep-alive strategies")
    typer.echo("")
    typer.echo(
        "Walk these in order when `nightly next` returns `nothing` — "
        "do not render the briefing and exit until every strategy comes up empty. "
        "Inspired by Karpathy's autoresearch "
        "(https://github.com/karpathy/autoresearch) NEVER STOP doctrine."
    )
    typer.echo("")
    for strategy in KEEPALIVE_STRATEGIES:
        marker = " ← recommended" if strategy.name == rec_name else ""
        typer.echo(f"## {strategy.name}{marker}")
        typer.echo("")
        typer.echo(f"*Applies when:* {strategy.applies_when}")
        typer.echo("")
        typer.echo(strategy.prompt)
        typer.echo("")


if __name__ == "__main__":
    app()
