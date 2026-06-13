"""Nightly CLI entry point.

Full command surface:
- `nightly version`     — print version
- `nightly info`        — short identity summary
- `nightly init`        — bootstrap .nightly/ + install host launcher
- `nightly status`      — report what Nightly knows about this repo
- `nightly uninstall`   — remove the host launcher
- `nightly start`       — create a new run (optionally seed first task)
- `nightly conclude`    — mark the current run as concluding (non-blocking)
- `nightly task`        — create a new task inside the current run
- `nightly seed-rfc`    — stub an accepted RFC from an interactive seed
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
- `nightly session …`   — arm/disarm the SESSION_ACTIVE marker for the Stop hook
- `nightly hook stop`   — Stop-hook handler (invoked by .claude/settings and equivalents)
- `nightly stop`        — request immediate hard stop (next turn boundary)
- `nightly update`      — pull latest source and refresh installed hosts in this repo
- `nightly doctor`      — diagnose & repair a drifted nightly install (skills + scaffold)
- `nightly verify`      — detect & run the repo's linters / formatters / type checkers
- `nightly ci`          — print CI status across open Nightly PRs (failed = work)
- `nightly bug`         — bundle run state into a debug report; optionally open issue
- `nightly worktree …`  — create and inspect Nightly-owned git worktrees
- `nightly dispatch …`  — background-dispatch specialist sub-agents
- `nightly vault …`     — build and open the knowledge-graph dashboard
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from nightly_core._version import __version__
from nightly_core.autonomy import can_auto_pr
from nightly_core.briefing import write_briefing
from nightly_core.bug import DEFAULT_BUG_REPO
from nightly_core.bug import build_report as build_bug_report
from nightly_core.bug import gh_command as bug_gh_command
from nightly_core.bug import submit_report as submit_bug_report
from nightly_core.bug import write_report as write_bug_report
from nightly_core.cascade import next_task as cascade_next
from nightly_core.cascade import pick_pr_rescue
from nightly_core.ci_watch import PRCIStatus, list_ci_status
from nightly_core.config import load_git_config
from nightly_core.contract import (
    HostId,
    InstallScope,
    NightlyHostIntegration,
    SpecialistRole,
)
from nightly_core.doctor import DoctorReport, diagnose_and_repair
from nightly_core.driver import DriverConfig, run_loop
from nightly_core.ideation import run_proposers, write_drafts
from nightly_core.keepalive import KEEPALIVE_STRATEGIES, pick_keepalive
from nightly_core.keepalive_hook import (
    HOOK_FORMATS,
    arm_session,
    compute_stop_hook_decision,
    disarm_session,
    estimate_context_tokens,
    format_decision,
    log_heartbeat,
    parse_hook_input,
    request_stop,
)
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
from nightly_core.seed_rfc import SEED_SOURCES, write_seed_rfc
from nightly_core.specialists import specialist_prompt
from nightly_core.triage import rank_issues
from nightly_core.update import (
    UpdateReport,
    refresh_repo_install,
    update_install,
)
from nightly_core.verify import VerifyReport, run_verify

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
  base_branch:   main
  branch_prefix: nightly/
  wip_prefix:    nightly/wip-
  protected:     [main, master, "release/*"]
  # Where per-task worktrees are placed. Leave unset to nest them under a
  # sibling `<repo>-nightly/` dir. Set an absolute/`~` path to keep trees off a
  # synced filesystem — REQUIRED on macOS if this repo lives in iCloud Drive
  # (~/Documents, ~/Desktop), where FileProvider silently corrupts git state.
  # Nightly auto-relocates to ~/.cache/nightly/worktrees if it detects iCloud.
  # worktree_root: ~/.cache/nightly/worktrees

refuse:
  destructive_git:        true
  production_state:       true
  external_communication: true
  network_egress_unknown: true
  scope_creep:            true
  bypass_test_or_type:    true

# pr_feedback governs the `pr_rescue` cascade step.
# - `enabled` flips the whole feature off without removing the block.
# - `review_bots` extends the default bot allowlist (CodeRabbit, Cursor BugBot,
#   Copilot reviewer, Greptile, Amp, etc.) with project-specific accounts.
# - `treat_bots_as_human` flips a bot login into the "human" bucket — useful
#   for an internally-trusted automation that should outrank ordinary bots.
pr_feedback:
  enabled:              true
  review_bots:          []
  treat_bots_as_human:  []

# vault governs the .nightly/vault/ knowledge graph (RFC 003).
# - `enabled: false` skips the vault build step in `nightly brief`.
# - `open_on_brief: true` pops the dashboard in a browser at brief time.
vault:
  enabled:       true
  open_on_brief: false

# worktree governs the readiness probe (RFC 002).
# - `probe_enabled: false` skips the probe entirely.
# - `remediate_enabled: false` surfaces remediable failures rather than
#   auto-fixing via `uv sync` / `pre-commit install --install-hooks`.
worktree:
  probe_enabled:     true
  remediate_enabled: true

# ideate governs the proposer suite (RFC 009).
# - `category_ordering: false` reverts the cascade to score-only ordering
#   (pre-v0.0.6 behavior). With it on, cleaning proposals outrank
#   capability proposals even at lower scores — "fix what's broken
#   before inventing new things."
# - `synthesis.enabled: false` disables the LLM-driven SynthesisProposer
#   entirely; the three Phase-5 narrow proposers still run.
# - `synthesis.timeout_seconds` caps the host CLI spawn wall-clock.
# - `synthesis.max_proposals` caps total synthesis output so the morning
#   briefing stays readable.
ideate:
  category_ordering: true
  synthesis:
    enabled:          true
    timeout_seconds:  120
    max_proposals:    25

# agents governs how specialist sub-agents (implementer / tester /
# reviewer / researcher) are dispatched in interactive sessions.
# - `background_dispatch: true` (default, and the preferred setting for
#   Claude Code / Codex / Cursor / Antigravity sessions) — specialists
#   spawn as detached host processes via `nightly dispatch start <slug>
#   --role <role>` so the operator's chat stays free for other work.
#   Poll via `nightly dispatch status / tail / wait`.
# - `background_dispatch: false` — fall back to the host's native
#   Task-tool surface (blocking the calling chat until the sub-agent
#   returns). Use only when you explicitly want to watch the
#   specialist's progress in-band (debugging an unfamiliar host,
#   eyeballing a long-running review).
# `nightly run` headless ignores this preference — each task gets its
# own host process by construction.
agents:
  background_dispatch: true

# context governs the context-compaction feature (v0.0.12). Nothing can
# programmatically trigger Claude Code's /compact, so Nightly instead makes
# compaction lossless (a digest re-injected via the SessionStart hook) and
# nudges the live session toward hygiene before it bloats.
# - `budget_tokens` is a SOFT ceiling: when the keepalive hook estimates the
#   session exceeds it, it prepends a "context diet" nudge to the continuation
#   prompt (finish delicate work first, lean on the digest, background heavy
#   work). 0 disables budget steering.
# - `digest_every_turns` writes .nightly/runs/<id>/digest.md every N keepalive
#   turns so the SessionStart(compact) hook re-injects fresh state. 0 disables
#   the interval write (the digest is still written on every planning-phase
#   reroute regardless).
context:
  budget_tokens:      256000
  digest_every_turns: 1
"""


_NIGHTLY_SUBDIRS: tuple[str, ...] = ("runs", "plans", "atlas", "memory", "prompts")

# Display tuning for `nightly triage` — wider issue titles get elided.
_TRIAGE_TITLE_MAX = 50
_TRIAGE_TITLE_ELIDE_AT = 47


# Each loader is a thin lambda that lazy-imports its host package so
# nightly-core never depends on its sub-packages at load time (the
# sub-packages depend on nightly-core — would be a cycle).
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

    def _gemini(root: Path | None) -> NightlyHostIntegration:
        from nightly_host_gemini import GeminiHostIntegration  # noqa: PLC0415

        return GeminiHostIntegration(root=root)

    _HOST_LOADERS["claude"] = _claude
    _HOST_LOADERS["codex"] = _codex
    _HOST_LOADERS["opencode"] = _opencode
    _HOST_LOADERS["cursor"] = _cursor
    _HOST_LOADERS["antigravity"] = _antigravity
    _HOST_LOADERS["gemini"] = _gemini


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
            f"Supported hosts: {sorted(_HOST_LOADERS)}. "
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


def _context_status_line(root: Path, run_path: Path) -> str | None:
    """Render the `context:` status line, or None when there's nothing to show.

    The keepalive hook writes `keepalive.context` every turn boundary; an
    empty file means the estimate couldn't be measured that turn (no
    transcript / no usage), so we omit the line entirely rather than print a
    misleading `~0K`."""
    from nightly_core.config import load_context_config  # noqa: PLC0415 - lazy
    from nightly_core.keepalive_hook import CONTEXT_FILENAME  # noqa: PLC0415 - lazy

    ctx_file = run_path / CONTEXT_FILENAME
    if not ctx_file.is_file():
        return None
    try:
        raw_ctx = ctx_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw_ctx:
        return None
    try:
        tokens = int(raw_ctx)
    except ValueError:
        return None
    budget = load_context_config(root).budget_tokens
    return (
        f"  context:   ~{round(tokens / 1000)}K tokens at last turn "
        f"boundary (soft budget {round(budget / 1000)}K)"
    )


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
    """Print version, one-liner description, and where to go next."""
    typer.echo(f"Nightly {__version__} — continuously-running, host-native coding agent.")
    typer.echo("Run `nightly init` to install the host skill; then type `/nightly`")
    typer.echo("inside your host to start a session, or use `nightly run` for headless.")
    typer.echo("See .planning/brainstorm.html for the full design.")


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
    """Bootstrap .nightly/, write default config, install the host launcher.

    Scope semantics (dogfooding Issue #1 fix):
    - `--scope project` (default): bootstrap `.nightly/`, write
      `config.yml`, install the project-scope skill files into the
      current repo's `.claude/skills/` (or equivalent), merge the
      Stop-hook entry, and seed `AGENTS.md` / `CLAUDE.md`. This is
      the per-repo init.
    - `--scope user`: install the host's skill files into the
      *user-global* directory (`~/.claude/skills/`, `~/.codex/skills/`,
      `~/.gemini/commands/`, …) and **do nothing else**. No `.nightly/`
      scaffold, no `config.yml`, no rules file in the current repo —
      the README's "install once globally, then `/nightly-init` in
      each repo" flow assumes user-scope is a pure global install.
    """
    root = repo_root()
    typer.echo(f"repo: {root}")

    integration = _load_host(host, root=root)
    target = integration.skill_path(scope)  # type: ignore[attr-defined]

    if scope == "user":
        # Pure global install — touch nothing in the cwd.
        asyncio.run(integration.install(scope))
        typer.echo(
            f"  ✓ installed {host} skill ({scope}) at {_format_path_for_display(target, root)}"
        )
        typer.echo("")
        typer.echo(
            "→ User-scope install complete. In any repo, type `/nightly-init` "
            "inside the host to bootstrap that repo's `.nightly/` scaffold."
        )
        return

    # Project scope: full bootstrap.
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

    asyncio.run(integration.install(scope))
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

    # Surface the v0.0.7+ agents preference so the operator can eyeball
    # whether interactive specialist dispatch will background or block.
    from nightly_core.config import load_agents_config  # noqa: PLC0415 - lazy

    agents_cfg = load_agents_config(root)
    mode = "background" if agents_cfg.background_dispatch else "foreground (Task tool)"
    typer.echo(f"  agents:    dispatch={mode}")

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

    # Surface the latest context-size estimate (v0.0.12).
    if run is not None:
        line = _context_status_line(root, run.path)
        if line is not None:
            typer.echo(line)

    # Surface RESPAWN_REQUESTED prominently. If present, the prior
    # session ended involuntarily mid forced-continuation chain (host
    # override without progress, crash, or kill) with cascade work
    # pending — the operator (or the `/nightly` skill respawn-detection
    # step) should resume the cascade rather than treat this as a fresh
    # start. Mirrors how we surface SESSION_ACTIVE via the runs block.
    from nightly_core.keepalive_hook import read_respawn_marker  # noqa: PLC0415 - lazy

    respawn = read_respawn_marker(root)
    if respawn is not None:
        typer.echo("  respawn:")
        typer.echo(f"    ⚠ RESPAWN_REQUESTED at {respawn or '(unknown time)'}")
        typer.echo("    prior session ended involuntarily mid-chain with cascade work pending;")
        typer.echo("    re-invoke `/nightly` (or run `nightly next`) to resume.")


# ── run lifecycle ─────────────────────────────────────────────────────────


@app.command()
def start(
    task: Annotated[
        str | None,
        typer.Argument(help="Optional task description; if given, seeds tasks/0001-<slug>/."),
    ] = None,
) -> None:
    """Begin a new Nightly session — creates a run dir and sets it as current.

    Optionally seeds the first task from the positional argument so the
    cascade's `resume_in_flight` step picks it up immediately. Starting a
    new run while another is active is allowed — the prior run's artifacts
    stay on disk and CURRENT advances. To formally drain the prior run
    first, use `nightly conclude` before calling this.
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
    """Signal the agent to wrap up — finishes the current task, then renders the briefing and exits.

    Writes a CONCLUDE marker the Stop hook reads at the next turn boundary.
    The agent finishes whatever it's doing (never SIGKILL), then stops
    picking new cascade work. Use `nightly stop` for an immediate hard stop.
    """
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
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help=(
                "Transition the named task's plan.md to this status. Skips "
                "task creation when the slug already exists; pairs cleanly "
                "with the seven canonical statuses (ready, in_progress, "
                "dispatching, blocked: approval, done, parked)."
            ),
        ),
    ] = None,
    proposer_fingerprint: Annotated[
        str | None,
        typer.Option(
            "--proposer-fingerprint",
            "-f",
            help=(
                "Stamp this proposer fingerprint into the new plan's "
                "frontmatter so the cascade can dedupe re-detected work "
                "next pass. Pass the value emitted by `nightly next` for "
                "ideate / ideate_fallback picks. Ignored when --status is "
                "used (no plan creation occurs)."
            ),
        ),
    ] = None,
) -> None:
    """Create a new task — or transition an existing task's status — in the current run.

    Dogfooding Issue #9: the interactive Skill flow previously had no
    CLI verb for plan-status transitions. The agent had to edit the
    plan.md frontmatter by hand (or shell into the venv and call
    `update_plan_status` from Python). Adding `--status` here mirrors
    the rest of the CLI's verb shape and makes the lifecycle the
    SKILL.md describes (ready → in_progress → done/parked) callable
    in one line.
    """
    from nightly_core.plans import (  # noqa: PLC0415 - local
        PLAN_STATUSES,
        update_plan_status,
    )

    root = repo_root()
    run = _require_current_run(root)

    if status is not None:
        if status not in PLAN_STATUSES:
            typer.echo(
                f"unknown status '{status}'. Valid: {', '.join(PLAN_STATUSES)}",
                err=True,
            )
            raise typer.Exit(code=1)
        # Find the existing task by slug suffix (`tasks/NNNN-<slug>/`).
        tasks_dir = run.path / "tasks"
        match = next(
            (t for t in tasks_dir.iterdir() if t.is_dir() and t.name.endswith(f"-{slug}")),
            None,
        )
        if match is None:
            typer.echo(
                f"no task with slug `{slug}` in run {run.id}. "
                'Pass `-d "<description>"` to create one, or fix the slug.',
                err=True,
            )
            raise typer.Exit(code=1)
        plan_path = match / "plan.md"
        record = update_plan_status(plan_path, status)  # type: ignore[arg-type]
        typer.echo(f"✓ {match.name} → status: {record.status}")
        return

    created = new_task(run, slug=slug, description=description)
    if proposer_fingerprint:
        from nightly_core.plans import (  # noqa: PLC0415 - local
            PROPOSER_FINGERPRINT_KEY,
            read_plan,
            render_frontmatter,
        )

        plan_path = created.path / "plan.md"
        plan = read_plan(plan_path)
        metadata = dict(plan.metadata)
        metadata[PROPOSER_FINGERPRINT_KEY] = proposer_fingerprint
        plan_path.write_text(render_frontmatter(metadata, plan.body), encoding="utf-8")
    typer.echo(
        f"✓ task {created.path.name} ready at {_format_path_for_display(created.path, root)}"
    )


@app.command(name="seed-rfc")
def seed_rfc_cmd(
    title: Annotated[
        str,
        typer.Argument(help="Human-readable RFC title — also the basis for the auto-derived slug."),
    ],
    slug: Annotated[
        str | None,
        typer.Option(
            "--slug",
            help=(
                "Override the auto-derived kebab-case slug. Use when the "
                "title's slugified form is longer than you want, or when "
                "you need a slug that diverges from the title."
            ),
        ),
    ] = None,
    source: Annotated[
        str,
        typer.Option(
            "--source",
            help=(
                "Trigger that fired: interactive_seed (operator typed "
                "`/nightly <seed>`), interactive_context (operator typed "
                "bare `/nightly` and the agent distilled prior conversation "
                "into a title), or headless (programmatic caller). "
                "Recorded in the RFC's frontmatter for retro analytics."
            ),
        ),
    ] = "interactive_seed",
) -> None:
    """Stub an `accepted` RFC under `.planning/rfcs/` from an interactive seed (RFC 005).

    The agent invokes this when the operator's seed (or distilled
    conversation context) describes a feature or multi-step
    initiative — heavy enough to warrant RFC-shape work rather than
    a single throwaway task. The CLI writes the next-numbered RFC
    with `status: accepted` and a section-by-section skeleton; the
    agent then opens the file and fills in Context, Resolved
    decisions, and the Sized checklist in its first Edit pass.

    For one-line bugfix seeds, keep using `nightly start <seed>` —
    the single-task pathway is still the right shape for one-shot
    work. The host's `skill.md` carries the heuristic for when to
    pick which pathway (RFC 005 §Resolved-7).
    """
    if source not in SEED_SOURCES:
        typer.echo(
            f"unknown source '{source}'. Valid: {', '.join(SEED_SOURCES)}",
            err=True,
        )
        raise typer.Exit(code=1)

    root = repo_root()
    try:
        path = write_seed_rfc(root, title=title, slug=slug, source=source)
    except ValueError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except FileExistsError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✓ stubbed {_format_path_for_display(path, root)} (status: accepted)")
    typer.echo(
        "→ next: edit the body to flesh out Context, Resolved decisions, and "
        "the Sized checklist. The cascade picks up unchecked items on "
        "subsequent `nightly next` calls."
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

    # Build the vault knowledge graph alongside the briefing.
    # Failures are downgraded to a warning — the briefing should never
    # be blocked by a vault step that errors.
    from nightly_core.config import load_vault_config  # noqa: PLC0415

    vault_cfg = load_vault_config(root)
    if vault_cfg.enabled:
        try:
            from nightly_core.vault import build as vault_build_call  # noqa: PLC0415

            vault_result = vault_build_call(root)
            typer.echo(
                f"  vault: {vault_result.total_nodes} nodes, "
                f"dashboard at {_format_path_for_display(vault_result.dashboard.index_path, root)}"
            )
            if vault_cfg.open_on_brief:
                import webbrowser  # noqa: PLC0415

                webbrowser.open(vault_result.dashboard.index_path.as_uri())
        except Exception as exc:
            typer.echo(f"  vault: build failed ({exc}); briefing unaffected", err=True)


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
    if choice.proposer_fingerprint is not None:
        # Surfaced for ideate / ideate_fallback picks so the agent can pass
        # it through to `nightly task -f <fp>` when materializing the plan.
        # Without it, the next cascade pass re-detects the same proposal
        # (proposers are stateless against unmerged main) and the loop
        # guard ends up yielding. See issue #4.
        typer.echo(f"fingerprint: {choice.proposer_fingerprint}")
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
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Bypass the `synthesis.json` cache and force a fresh LLM "
                "spawn (RFC 009 §C2). Useful when the strategic review "
                "should refresh against new code mid-session."
            ),
        ),
    ] = False,
) -> None:
    """Run the proposer suite as a dry-run — list candidates without writing.

    Use `nightly ideate` to actually persist drafts under
    `<run>/proposed/issues/` for human review.
    """
    root = repo_root()
    proposals = run_proposers(root, force_synthesis=force)
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
    git_cfg = load_git_config(root)
    cfg = DriverConfig(
        host_id=host,
        max_tasks=max_tasks,
        concurrency=max(1, concurrency),
        timeout_per_task_s=timeout_per_task,
        base_branch=git_cfg.base_branch,
        branch_prefix=git_cfg.branch_prefix,
        worktree_root=git_cfg.worktree_root,
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
    """Spawn a host's non-interactive CLI with `prompt` and print the result.

    Subscription credentials propagate through the environment — the
    spawned CLI reads its own cached creds from `~/.<host>/...`. Set the
    host's API key env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.)
    before invoking when running from a sandboxed CI environment.

    For cascade-driven multi-task runs, use `nightly run` instead — it
    wraps this primitive in a start → next → headless → land → brief loop.
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
def ideate(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Bypass the `synthesis.json` cache and force a fresh LLM "
                "spawn (RFC 009 §C2). Useful when the strategic review "
                "should refresh against new code mid-session — without "
                "the flag, synthesis is throttled to once per session."
            ),
        ),
    ] = False,
) -> None:
    """Run the proposer suite and write draft issues to the current run.

    Writes one markdown file per proposal under
    `.nightly/runs/<id>/proposed/issues/`, ordered by score. The briefing
    surfaces them in the morning report. If any proposal clears the
    autonomy bar, the cascade will pick it on the next `nightly next`.

    Proposals whose fingerprint matches a `done` / `in_progress` /
    `blocked: approval` plan from any run are filtered out before write
    — same dedupe the cascade applies. Without this, ideating after a
    completed task surfaces a duplicate proposal in the morning
    briefing for work that already shipped (see issue #2's surrounding
    discussion and dogfooding Issue #10).
    """
    from nightly_core.cascade import _dedupe_proposals  # noqa: PLC0415 - lazy

    root = repo_root()
    run = _require_current_run(root)
    all_proposals = run_proposers(root, force_synthesis=force)
    if not all_proposals:
        typer.echo("· no proposals — every proposer came up empty")
        return
    proposals = _dedupe_proposals(all_proposals, root)
    deduped = len(all_proposals) - len(proposals)
    if not proposals:
        typer.echo(
            f"· no proposals after dedupe — {deduped} candidate(s) "
            "matched fingerprints of completed or in-flight work"
        )
        return
    paths = write_drafts(run, proposals)
    auto_eligible = sum(1 for p in proposals if can_auto_pr(p))
    typer.echo(
        f"✓ wrote {len(paths)} proposal(s) to "
        f"{_format_path_for_display(run.path / 'proposed' / 'issues', root)}"
    )
    typer.echo(
        f"  {auto_eligible} auto-PR-eligible · "
        f"{len(proposals) - auto_eligible} for human review"
        + (f" · {deduped} deduped (fingerprint matched completed work)" if deduped else "")
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
            head = head[:_TRIAGE_TITLE_ELIDE_AT] + "..."
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
                f"✗ could not match branch '{target}' to a plan — feedback not appended.",
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
            head = head[:_TRIAGE_TITLE_ELIDE_AT] + "..."
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
        "GENUINE WORK IS NEVER EXHAUSTED. Walk these in order when "
        "`nightly next` returns `nothing` — do NOT render the briefing "
        "and exit until every strategy comes up empty (and even then, the "
        "`plan_improvement` universal fallback applies to any repo with "
        "source code). Inspired by Karpathy's autoresearch "
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


# ── keep-alive hook + session + stop ─────────────────────────────────────


session_app = typer.Typer(
    name="session",
    help="Arm / disarm Nightly's Stop-hook keep-alive for this run.",
    no_args_is_help=True,
)
hook_app = typer.Typer(
    name="hook",
    help="Internal — Claude Code hook handlers (invoked by .claude/settings).",
    no_args_is_help=True,
)
worktree_app = typer.Typer(
    name="worktree",
    help="Create and inspect Nightly-owned git worktrees.",
    no_args_is_help=True,
)
dispatch_app = typer.Typer(
    name="dispatch",
    help=(
        "Background-dispatch specialist sub-agents so the interactive "
        "session stays free for the operator."
    ),
    no_args_is_help=True,
)
vault_app = typer.Typer(
    name="vault",
    help="Build and open the .nightly/vault/ knowledge graph (RFC 003).",
    no_args_is_help=True,
)
app.add_typer(session_app)
app.add_typer(hook_app)
app.add_typer(worktree_app)
app.add_typer(dispatch_app)
app.add_typer(vault_app)


@vault_app.command(name="index")
def vault_index() -> None:
    """Rebuild `_index.db` from the markdown vault. Idempotent; ≪ 1s."""
    from nightly_core.vault import rebuild_index, vault_root_for  # noqa: PLC0415

    root = repo_root()
    vault_root = vault_root_for(root)
    stats = rebuild_index(vault_root)
    typer.echo(
        f"✓ indexed {stats.node_count} nodes, {stats.edge_count} edges"
        + (f" ({stats.placeholder_count} dangling)" if stats.placeholder_count else "")
    )


@vault_app.command(name="build")
def vault_build() -> None:
    """Full pipeline: project runs → index → render encyclopedia + dashboard."""
    from nightly_core.vault import build as vault_build_call  # noqa: PLC0415

    root = repo_root()
    result = vault_build_call(root)
    typer.echo(
        f"✓ vault built — {len(result.projections)} runs, "
        f"{result.total_nodes} nodes, "
        f"{result.encyclopedia.pages_written} encyclopedia pages, "
        f"dashboard at {_format_path_for_display(result.dashboard.index_path, root)}"
    )


@vault_app.command(name="open")
def vault_open(
    encyclopedia: Annotated[
        bool,
        typer.Option("--encyclopedia", help="Open the encyclopedia (per-node prose pages)."),
    ] = False,
    dashboard: Annotated[
        bool,
        typer.Option("--dashboard", help="Open the dashboard (graph + filters). Default."),
    ] = False,
    build_first: Annotated[
        bool,
        typer.Option("--build/--no-build", help="Rebuild before opening."),
    ] = True,
) -> None:
    """Open the encyclopedia or dashboard in the default browser."""
    import webbrowser  # noqa: PLC0415

    from nightly_core.vault import build as vault_build_call  # noqa: PLC0415
    from nightly_core.vault import vault_root_for  # noqa: PLC0415

    root = repo_root()
    if build_first:
        vault_build_call(root)

    vault_root = vault_root_for(root)
    if encyclopedia and not dashboard:
        target = vault_root / "_site" / "index.html"
    else:
        target = vault_root / "_dashboard" / "index.html"

    if not target.is_file():
        typer.echo(f"target missing: {target}; run `nightly vault build`", err=True)
        raise typer.Exit(code=1)
    webbrowser.open(target.as_uri())
    typer.echo(f"opened {_format_path_for_display(target, root)}")


@vault_app.command(name="sync-prs")
def vault_sync_prs() -> None:
    """Walk `gh pr list` and mint vault nodes for any missing Nightly PRs."""
    from nightly_core.vault.project import backfill_prs  # noqa: PLC0415

    root = repo_root()
    paths = backfill_prs(root)
    typer.echo(f"✓ synced {len(paths)} PR node(s)")


@vault_app.command(name="sync-feedback")
def vault_sync_feedback() -> None:
    """Walk vault PR nodes and mint feedback nodes from `gh` review data."""
    from nightly_core.vault.project import backfill_feedback  # noqa: PLC0415

    root = repo_root()
    paths = backfill_feedback(root)
    typer.echo(f"✓ synced {len(paths)} feedback node(s)")


@worktree_app.command(name="create")
def worktree_create(
    slug: Annotated[
        str,
        typer.Argument(
            help=(
                "Task slug (lowercase, dashes). The created branch is "
                "`<branch_prefix><slug>-<short-ts>`; the worktree path "
                "is decided by `_resolve_worktree_base` (config-overridable, "
                "iCloud-aware)."
            ),
        ),
    ],
    base_branch: Annotated[
        str,
        typer.Option(
            "--base",
            help="Branch to fork from. Default: main.",
        ),
    ] = "main",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print where the worktree WOULD be created, but don't run git.",
        ),
    ] = False,
) -> None:
    """Create an isolated worktree for this task — config-aware, iCloud-safe.

    This is the verb the SKILL.md tells the agent to use during the
    ISOLATE step. Previously the SKILL prescribed a literal
    `git worktree add ../nightly-<slug>-<ts>` which ignored the
    `worktree_root` config knob shipped in eb4434c and the
    nest-under-<repo>-nightly default from 5369db0. A real modular-
    session bug surfaced: the agent created the worktree at the
    workspace root, then on recovery deleted the operator's intended
    `<repo>-worktrees/` directory thinking it was stray state.

    Reads `worktree_root` and `branch_prefix` from `.nightly/config.yml`
    when present; falls back to safe defaults otherwise. Always emits
    the resolved path + branch on stdout in `path=<abs>\\nbranch=<name>`
    shape so callers can parse without scraping `git worktree list`.
    """
    from nightly_core.worktree import (  # noqa: PLC0415 - lazy import
        DEFAULT_BRANCH_PREFIX,
        _resolve_worktree_base,
        _worktree_path,
        create_worktree,
        default_git_runner,
    )

    root = repo_root()
    cfg = load_git_config(root)
    branch_prefix = cfg.branch_prefix or DEFAULT_BRANCH_PREFIX
    worktree_root_cfg = cfg.worktree_root

    if dry_run:
        # Resolve placement without creating anything. Useful for the
        # agent to confirm "where will this land" before committing.
        try:
            base = asyncio.run(
                _resolve_worktree_base(
                    root, worktree_root=worktree_root_cfg, run=default_git_runner
                )
            )
        except Exception as exc:
            typer.echo(f"could not resolve worktree base: {exc!r}", err=True)
            raise typer.Exit(code=1) from None
        # Compute the per-task path the way create_worktree would.
        from nightly_core.worktree import _branch_name  # noqa: PLC0415 - lazy

        branch = _branch_name(slug, prefix=branch_prefix)
        path = _worktree_path(base, branch)
        typer.echo(f"path={path}")
        typer.echo(f"branch={branch}")
        typer.echo(f"base_branch={base_branch}")
        typer.echo(f"worktree_root={worktree_root_cfg or '(auto)'}")
        return

    try:
        handle = asyncio.run(
            create_worktree(
                root,
                slug,
                base_branch=base_branch,
                branch_prefix=branch_prefix,
                worktree_root=worktree_root_cfg,
            )
        )
    except RuntimeError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"path={handle.path}")
    typer.echo(f"branch={handle.branch}")
    typer.echo(f"base_branch={handle.base_branch}")


@worktree_app.command(name="list")
def worktree_list_cmd(
    branch_prefix: Annotated[
        str,
        typer.Option(
            "--prefix",
            help="Only show worktrees whose branch starts with this prefix.",
        ),
    ] = "nightly/",
) -> None:
    """List Nightly-owned worktrees (branches matching `--prefix`)."""
    from nightly_core.worktree import list_worktrees  # noqa: PLC0415 - lazy

    root = repo_root()
    try:
        handles = asyncio.run(list_worktrees(root, branch_prefix=branch_prefix))
    except RuntimeError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from None

    if not handles:
        typer.echo(f"· no worktrees with branch prefix `{branch_prefix}`")
        return
    for h in handles:
        typer.echo(f"{h.branch:<60} {h.path}")


@worktree_app.command(name="doctor")
def worktree_doctor_cmd(
    remediate: Annotated[
        bool,
        typer.Option(
            "--remediate",
            help="Auto-fix `missing_python_dep` / `missing_pre_commit_hook` failures.",
        ),
    ] = False,
) -> None:
    """Probe the worktree's pre-commit infrastructure and report readiness.

    Auto-remediates `missing_python_dep` (runs `uv sync --all-extras`) and
    `missing_pre_commit_hook` (runs `pre-commit install --install-hooks`) when
    `--remediate` is passed. Other failure kinds require operator action.

    Exit codes:
    - 0  → ready
    - 1  → blocked (operator intervention required)
    - 2  → remediable (re-invoke with `--remediate`)
    """
    from nightly_core.worktree_doctor import (  # noqa: PLC0415
        probe_worktree_readiness,
    )
    from nightly_core.worktree_doctor import (  # noqa: PLC0415
        remediate as run_remediation,
    )

    root = repo_root()
    readiness = probe_worktree_readiness(root)
    if readiness.ok:
        typer.echo("✓ worktree ready")
        return

    typer.echo(f"✗ {readiness.state}: {readiness.kind} — {readiness.detail}")
    if not remediate or not readiness.remediable:
        raise typer.Exit(code=1 if readiness.blocked else 2)

    typer.echo(f"… remediating {readiness.kind}")
    if run_remediation(readiness, root):
        # Re-probe once to confirm
        after = probe_worktree_readiness(root)
        if after.ok:
            typer.echo("✓ remediation succeeded; worktree ready")
            return
        typer.echo(f"✗ still {after.state}: {after.kind} — {after.detail}")
        raise typer.Exit(code=1 if after.blocked else 2)
    typer.echo("✗ remediation failed")
    raise typer.Exit(code=1)


# ── dispatch — background specialist sub-processes ────────────────────────


@dispatch_app.command(name="start")
def dispatch_start_cmd(
    slug: Annotated[
        str,
        typer.Argument(help="Task slug. Must exist under the current run."),
    ],
    role: Annotated[
        SpecialistRole,
        typer.Option(
            "--role",
            "-r",
            help="Specialist role to dispatch (implementer | tester | reviewer | researcher).",
        ),
    ] = "implementer",
    host: Annotated[
        HostId,
        typer.Option(
            "--host",
            help="Host whose headless CLI to invoke. Defaults to claude.",
        ),
    ] = "claude",
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            "-p",
            help=(
                "Prompt body for the specialist. Defaults to the role's "
                "system prompt from `nightly specialist <role>` plus a "
                "pointer at the task's plan.md."
            ),
        ),
    ] = None,
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            help="Working directory for the spawned process. Defaults to the repo root.",
        ),
    ] = None,
) -> None:
    """Spawn the host's headless CLI as a detached background process.

    Default behavior for interactive Nightly sessions: every
    IMPLEMENT / TEST / REVIEW step calls this verb instead of the
    host's blocking sub-agent primitive (Task tool, MCP dispatch,
    session fork). The operator's chat stays open; the spawned
    process writes to the dispatch.log next to the task plan.

    Returns `pid=<n>\\nlog=<path>\\nstatus=running` on stdout. Use
    `nightly dispatch status` to poll, `nightly dispatch tail` to
    follow output, `nightly dispatch wait` to block.
    """
    from nightly_core.dispatch import start_background  # noqa: PLC0415 - lazy
    from nightly_core.specialists import specialist_prompt  # noqa: PLC0415 - lazy

    root = repo_root()
    body = prompt or _default_dispatch_prompt(role=role, slug=slug, specialist=specialist_prompt)
    try:
        result = start_background(
            slug,
            role=role,
            host=host,
            prompt=body,
            root=root,
            cwd=cwd,
        )
    except RuntimeError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"pid={result.pid}")
    typer.echo(f"log={_format_path_for_display(result.log_path, root)}")
    typer.echo(f"status={result.status}")
    typer.echo(f"slug={result.slug}")
    typer.echo(f"role={result.role}")
    typer.echo(f"host={result.host}")


def _default_dispatch_prompt(
    *,
    role: SpecialistRole,
    slug: str,
    specialist: Callable[[SpecialistRole], str],
) -> str:
    """Build the default prompt for a backgrounded specialist.

    Concatenates the canonical role prompt (`nightly specialist <role>`)
    with a one-line "advance plan <slug>" addendum so the spawned host
    knows what to work on. Operators can override with `--prompt` to
    inject a more specific brief.
    """
    role_prompt = specialist(role)
    return (
        f"{role_prompt}\n\n"
        f"Advance the task `{slug}` in this repo. Read "
        f"`.nightly/runs/<current>/tasks/<N>-{slug}/plan.md` for scope, "
        "implement the plan, commit on the task's worktree branch, and "
        f"update plan status to `done` (or `parked` / `blocked: approval` "
        "if the refusal policy applies). Do not start new cascade work."
    )


@dispatch_app.command(name="status")
def dispatch_status_cmd(
    slug: Annotated[
        str | None,
        typer.Argument(
            help="Specific task slug to inspect. Omit to list every dispatch.",
        ),
    ] = None,
) -> None:
    """List active + finished background dispatches in the current run.

    Refreshes each result against the live PID before reporting, so a
    `running` row reflects the current state (not a stale snapshot).
    """
    from nightly_core.dispatch import (  # noqa: PLC0415 - lazy
        list_dispatches,
        read_dispatch_state,
        refresh,
    )

    root = repo_root()

    if slug is not None:
        state = read_dispatch_state(slug, root=root)
        if state is None:
            typer.echo(f"· no dispatch recorded for `{slug}`")
            raise typer.Exit(code=1)
        state = refresh(state, root=root)
        _print_dispatch_row(state, root=root, verbose=True)
        return

    states = list_dispatches(root=root)
    if not states:
        typer.echo("· no dispatches in the current run")
        return
    typer.echo(f"{'status':<10} {'pid':<8} {'host':<10} {'role':<13} {'slug':<32} log")
    typer.echo("-" * 78)
    for state in states:
        live = refresh(state, root=root)
        _print_dispatch_row(live, root=root, verbose=False)


def _print_dispatch_row(
    state: object,  # BackgroundDispatchResult — typed via duck on the read side
    *,
    root: Path,
    verbose: bool,
) -> None:
    """Render one dispatch as either a compact table row or a verbose block."""
    log = _format_path_for_display(state.log_path, root)  # type: ignore[attr-defined]
    if verbose:
        typer.echo(f"slug:      {state.slug}")  # type: ignore[attr-defined]
        typer.echo(f"role:      {state.role}")  # type: ignore[attr-defined]
        typer.echo(f"host:      {state.host}")  # type: ignore[attr-defined]
        typer.echo(f"pid:       {state.pid}")  # type: ignore[attr-defined]
        typer.echo(f"status:    {state.status}")  # type: ignore[attr-defined]
        if state.exit_code is not None:  # type: ignore[attr-defined]
            typer.echo(f"exit_code: {state.exit_code}")  # type: ignore[attr-defined]
        typer.echo(f"started:   {state.started_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")  # type: ignore[attr-defined]
        if state.finished_at is not None:  # type: ignore[attr-defined]
            typer.echo(f"finished:  {state.finished_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")  # type: ignore[attr-defined]
        typer.echo(f"log:       {log}")
        return
    typer.echo(
        f"{state.status:<10} {state.pid:<8} {state.host:<10} "  # type: ignore[attr-defined]
        f"{state.role:<13} {state.slug:<32} {log}"  # type: ignore[attr-defined]
    )


@dispatch_app.command(name="tail")
def dispatch_tail_cmd(
    slug: Annotated[str, typer.Argument(help="Task slug to tail.")],
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", help="Print the last N lines, then exit."),
    ] = 50,
) -> None:
    """Print the last N lines of a dispatch's log.

    Doesn't tail-follow — too session-disruptive in an interactive
    chat. Operators who want a live view should `tail -f <log>` in
    a separate terminal.
    """
    from nightly_core.dispatch import read_dispatch_state  # noqa: PLC0415 - lazy

    root = repo_root()
    state = read_dispatch_state(slug, root=root)
    if state is None:
        typer.echo(f"· no dispatch recorded for `{slug}`", err=True)
        raise typer.Exit(code=1)
    if not state.log_path.is_file():
        typer.echo(f"· log file missing: {state.log_path}", err=True)
        raise typer.Exit(code=1)
    text = state.log_path.read_text(encoding="utf-8", errors="replace")
    tail = text.splitlines()[-lines:]
    for line in tail:
        typer.echo(line)


@dispatch_app.command(name="wait")
def dispatch_wait_cmd(
    slug: Annotated[str, typer.Argument(help="Task slug to wait on.")],
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help="Wall-clock timeout in seconds. Default: no timeout (block forever).",
        ),
    ] = None,
    poll_interval: Annotated[
        float,
        typer.Option(
            "--poll-interval",
            help="Seconds between PID-liveness polls. Default: 1.",
        ),
    ] = 1.0,
) -> None:
    """Block until the dispatched process exits (or timeout elapses).

    Exit code mirrors the dispatch:
    - 0 if it finished cleanly within the timeout (status: completed).
    - 1 if the timeout elapsed while the process was still running.
    - 2 if no dispatch was found for the slug.
    """
    from nightly_core.dispatch import wait_for  # noqa: PLC0415 - lazy

    root = repo_root()
    state = wait_for(slug, root=root, timeout_s=timeout, poll_interval_s=poll_interval)
    if state is None:
        typer.echo(f"· no dispatch recorded for `{slug}`", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"status={state.status}")
    if state.exit_code is not None:
        typer.echo(f"exit_code={state.exit_code}")
    if state.finished_at is not None:
        typer.echo(f"finished_at={state.finished_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if state.status == "running":
        raise typer.Exit(code=1)


@session_app.command(name="start")
def session_start() -> None:
    """Arm the SESSION_ACTIVE marker so the Stop hook force-continues.

    The /nightly skill calls this at session start. Without it, the Stop
    hook treats the session as non-Nightly and lets it stop naturally —
    so this is the "opt in to keep-alive" switch. Idempotent.

    Surfaces RESPAWN_REQUESTED before clearing it. If the prior session
    ended involuntarily mid forced-continuation chain (host override
    without progress, crash, or kill — bug reports #13/#16), the marker
    tells the skill to skip the seed-vs-cascade prelude and go straight
    to `nightly next`. We print the notice from this verb (rather than
    relying on the skill to call `nightly status`) so the signal is
    unmissable in the same scrollback line as the arm acknowledgement.
    """
    from nightly_core.keepalive_hook import read_respawn_marker  # noqa: PLC0415 - lazy

    root = repo_root()
    respawn = read_respawn_marker(root)
    marker = arm_session(root)  # clears RESPAWN_REQUESTED as a side effect
    if marker is None:
        typer.echo(
            "no active run — `nightly start` first, then `nightly session start`.",
            err=True,
        )
        raise typer.Exit(code=1)
    if respawn is not None:
        typer.echo(
            f"⚠ RESPAWN_REQUESTED (involuntary mid-chain stop at {respawn or 'unknown time'})"
        )
        typer.echo("  prior session ended involuntarily with cascade work pending —")
        typer.echo("  skip the seed prelude and run `nightly next` immediately.")
    typer.echo(f"✓ armed keep-alive — {_format_path_for_display(marker, root)}")
    typer.echo("  The Stop hook will force-continue until CONCLUDE or STOP.")


@session_app.command(name="stop")
def session_stop() -> None:
    """Disarm the SESSION_ACTIVE marker — the Stop hook stops force-continuing.

    Less abrupt than `nightly stop` (which writes a STOP sentinel) and
    less graceful than `nightly conclude` (which waits for the current
    task to drain). Use when you want the session to end as soon as the
    model naturally stops, without writing any extra control file.
    """
    root = repo_root()
    marker = disarm_session(root)
    if marker is None:
        typer.echo("· no active run; nothing to disarm.", err=True)
        return
    typer.echo(f"✓ disarmed keep-alive ({_format_path_for_display(marker, root)})")


@app.command(name="stop")
def stop_cmd() -> None:
    """Request an immediate hard stop — the next turn boundary lets the model end.

    Writes a `STOP` sentinel under the current run dir. The Stop hook
    sees the sentinel on its next invocation and allows the model to
    finish its current response cleanly. Unlike `nightly conclude`, this
    does *not* wait for the current task to drain — use it when you
    walked over to the computer and want Nightly off **now**.
    """
    root = repo_root()
    marker = request_stop(root)
    if marker is None:
        typer.echo("no active run.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"✓ wrote STOP sentinel — {_format_path_for_display(marker, root)}")
    typer.echo("  The model will end its turn at the next Stop hook firing.")


@app.command(name="update")
def update_cmd(
    version: Annotated[
        str,
        typer.Option(
            "--version",
            help="Branch / tag / commit to upgrade to. Default: main.",
        ),
    ] = "main",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Fetch and show the commit delta without checking out or syncing.",
        ),
    ] = False,
    refresh_repo: Annotated[
        bool,
        typer.Option(
            "--refresh-repo/--no-refresh-repo",
            help=(
                "After upgrading the source, re-install every host that's "
                "already present in this repo so it picks up the new skill / "
                "hook / rules content. Default: on."
            ),
        ),
    ] = True,
) -> None:
    """Update Nightly's source and refresh installed hosts in this repo.

    Inspired by gsd-build's idempotent installer pattern
    (https://github.com/gsd-build/get-shit-done). For git installs this
    is `git fetch + checkout + uv sync`; for PyPI / pipx / uv-tool
    installs it prints the right upgrade command and exits cleanly.

    By default also walks the current repo and re-runs `nightly init`
    for every host already installed there, so new SKILL.md content,
    Stop-hook entries, conclude/update skill files, and AGENTS.md /
    CLAUDE.md rules block all propagate without manual intervention.
    """
    try:
        notes: list[str] = []
        method, before, after = update_install(version=version, dry_run=dry_run, notes=notes)
    except RuntimeError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from None
    except subprocess.CalledProcessError as exc:
        typer.echo(
            f"✗ upgrade command failed (exit {exc.returncode}): "
            f"{(exc.stderr or '').strip() or '(no stderr)'}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    refreshed: tuple[str, ...] = ()
    rules_action = "skipped"
    if refresh_repo and not dry_run:
        root = repo_root()
        try:
            refreshed, rules_action = refresh_repo_install(root)
        except Exception as exc:  # surface, don't crash on per-host quirks
            notes.append(f"refresh skipped: {exc!r}")

    report = UpdateReport(
        method=method,
        requested_version=version,
        before=before,
        after=after,
        refreshed_hosts=refreshed,
        rules_action=rules_action,
        dry_run=dry_run,
        notes=tuple(notes),
    )
    _print_update_report(report)


def _print_update_report(report: UpdateReport) -> None:
    if report.method.is_git:
        source = str(report.method.root) if report.method.root is not None else "(unknown)"
        typer.echo(f"source:   {source}")
    else:
        typer.echo(f"source:   {report.method.kind}")
    typer.echo(f"version:  {report.requested_version}{' (dry-run)' if report.dry_run else ''}")
    if report.before or report.after:
        if report.dry_run:
            typer.echo(f"commit:   {report.before or '?'} (no checkout in dry-run)")
        elif report.source_changed:
            typer.echo(f"commit:   {report.before or '?'} → {report.after or '?'}")
        else:
            typer.echo(f"commit:   {report.after or report.before or '?'} (already current)")
    if report.refreshed_hosts:
        typer.echo("hosts:    refreshed " + ", ".join(report.refreshed_hosts))
    elif not report.dry_run:
        typer.echo("hosts:    none installed in this repo")
    if report.rules_action in {"created", "updated"}:
        typer.echo(f"rules:    {report.rules_action} AGENTS.md / CLAUDE.md block")
    elif not report.dry_run:
        typer.echo("rules:    unchanged")
    for note in report.notes:
        typer.echo(f"  · {note}")


@app.command(name="check-update")
def check_update_cmd(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=("Bypass the 24h cache and refetch the latest release from GitHub now."),
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help=(
                "Print a status line even when up-to-date or when the "
                "check is suppressed (dev install). Default: silent on "
                "success so the agent can detect 'something to surface' "
                "by stdout emptiness."
            ),
        ),
    ] = False,
) -> None:
    """Check whether a newer Nightly release is available.

    Designed to be called at session start by every host's SKILL.md.
    Prints ONE line when an upgrade is available (the recommendation
    text), then exits 0. Stays silent when up-to-date or when the
    check is suppressed — that way the agent can detect "there's news"
    simply by checking whether stdout is empty.

    Network paths: `gh api repos/<repo>/releases/latest` first (uses
    operator's auth, no rate limit), then anonymous urllib fallback.
    Both failures yield a silent exit, never a crash. Cache lives at
    `~/.cache/nightly/update-check.json` with a 24h TTL.
    """
    from nightly_core.check_update import check_for_update  # noqa: PLC0415

    result = check_for_update(force=force)

    if result is None:
        if verbose:
            typer.echo("· check skipped (dev install — pull manually)")
        return

    rec = result.recommendation()
    if rec is None:
        if verbose:
            latest = result.latest or "(unknown)"
            typer.echo(
                f"· up to date — Nightly {result.current} "
                f"(latest: {latest}, channel: {result.channel})"
            )
        return

    typer.echo(rec)


@app.command(name="verify")
def verify_cmd(
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help=(
                "Only run checks with these names (e.g. --only ruff-check --only mypy). "
                "May be passed multiple times. Default: run every detected check."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List detected checks without running anything.",
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Per-check timeout in seconds. Default: 300.",
        ),
    ] = 300.0,
) -> None:
    """Detect & run the repo's linters / formatters / type checkers.

    Run before opening any Nightly PR — the auto-PR autonomy bar
    assumes lint and format pass. Detected tools come from
    `pyproject.toml` (ruff/black/mypy/pyrefly), `package.json`
    (eslint/prettier/tsc), `go.mod` (gofmt/go vet), `Cargo.toml`
    (cargo fmt/clippy), and `Makefile` (lint/check/verify targets).

    Exits non-zero on any failed check or missing configured tool so
    the agent can branch on `$?` from the prompt.
    """
    root = repo_root()
    report = run_verify(
        root,
        dry_run=dry_run,
        only=only,
        timeout_s=timeout,
    )
    _print_verify_report(report, root=root)
    if report.failed or report.not_found:
        raise typer.Exit(code=1)


def _print_verify_report(report: VerifyReport, *, root: Path) -> None:
    typer.echo(f"repo: {root}")
    if report.dry_run:
        typer.echo("mode: dry-run (no commands executed)")
    typer.echo("")
    if not report.checks:
        typer.echo("· no linters or formatters detected for this repo")
        return
    typer.echo(f"{'status':<10} {'check':<16} command")
    typer.echo("-" * 70)
    glyph = {
        "ok": "✓ ok",
        "failed": "✗ fail",
        "skipped": "·",
        "not_found": "✗ miss",
    }
    for c in report.checks:
        mark = glyph.get(c.status, c.status)
        cmd = " ".join(c.command)
        typer.echo(f"{mark:<10} {c.name:<16} {cmd}")
    typer.echo("")
    if report.dry_run:
        typer.echo(f"detected: {len(report.checks)} check(s)")
        return
    if report.ok:
        typer.echo(f"all clear — {len(report.passed)} check(s) passed")
        return
    if report.failed:
        typer.echo(f"failed: {len(report.failed)} check(s):")
        for c in report.failed:
            typer.echo(f"  ✗ {c.name} (exit {c.exit_code})")
            head = c.output.splitlines()[:6]
            for line in head:
                typer.echo(f"      {line}")
    if report.not_found:
        typer.echo(
            f"missing tooling: {len(report.not_found)} check(s) — "
            "install the binary or remove the config",
            err=True,
        )
        for c in report.not_found:
            typer.echo(f"  ✗ {c.name} ({' '.join(c.command)})", err=True)


@app.command(name="ci")
def ci_cmd(
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help=(
                "Inspect just this branch (default: every open Nightly PR). "
                "Useful when the agent wants to recheck after pushing a fix."
            ),
        ),
    ] = None,
) -> None:
    """Print CI status across open Nightly PRs.

    Glanced at between tasks — failed checks naturally route into the
    `pr_rescue` cascade step on the next `nightly next`, so the agent
    doesn't need to block waiting on CI. Exits non-zero when any PR
    has a failing check (the agent can branch on it).
    """
    root = repo_root()
    statuses = list_ci_status(root)
    if branch is not None:
        statuses = [s for s in statuses if s.branch == branch]
    _print_ci_status(statuses)
    if any(s.is_failing for s in statuses):
        raise typer.Exit(code=1)


def _print_ci_status(statuses: list[PRCIStatus]) -> None:
    if not statuses:
        typer.echo("· no open Nightly PRs (or `gh` CLI unavailable)")
        return
    typer.echo(f"{'overall':<10} {'pr':<6} {'branch':<40} failed")
    typer.echo("-" * 78)
    glyph = {
        "pass": "✓ pass",
        "fail": "✗ fail",
        "cancel": "✗ canc",
        "pending": "· pend",
        "skipping": "· skip",
        "unknown": "· ?",
    }
    for s in statuses:
        mark = glyph.get(s.overall, s.overall)
        failed_names = ", ".join(c.name for c in s.failed_checks) or "—"
        typer.echo(f"{mark:<10} #{s.pr_number:<5} {s.branch:<40} {failed_names}")
    typer.echo("")
    failing = [s for s in statuses if s.is_failing]
    pending = [s for s in statuses if s.is_pending]
    if failing:
        typer.echo(f"failing: {len(failing)} PR(s) — cascade will route to pr_rescue")
    elif pending:
        typer.echo(f"pending: {len(pending)} PR(s) — keep working, recheck later")
    else:
        typer.echo("all clear")


@app.command(name="bug")
def bug_cmd(
    title: Annotated[
        str | None,
        typer.Option(
            "--title",
            "-t",
            help="Issue title. Defaults to a stamped auto-title with the run id.",
        ),
    ] = None,
    describe: Annotated[
        str | None,
        typer.Option(
            "--describe",
            "-d",
            help=(
                "Short free-text 'what went wrong' summary — becomes the report's "
                "first section so reviewers see context before the disk dump."
            ),
        ),
    ] = None,
    repo: Annotated[
        str,
        typer.Option(
            "--repo",
            help=(
                "GitHub `owner/name` to file the issue against. Default is the "
                "upstream Nightly source repo."
            ),
        ),
    ] = DEFAULT_BUG_REPO,
    submit: Annotated[
        bool,
        typer.Option(
            "--submit/--no-submit",
            help=(
                "Run `gh issue create` after writing the report. Default: on. "
                "Disable to capture state without filing publicly."
            ),
        ),
    ] = True,
) -> None:
    """Bundle Nightly run state into a debug report; optionally open an issue.

    Use this when Nightly itself looks wrong — the agent self-concluded,
    the Stop hook stopped force-continuing while work remained, the
    cascade ignored a real plan, a worktree wedged. Captures
    `keepalive.log`, plan statuses, on-disk markers, last briefing,
    `nightly status`, `nightly next`, recent git log, and the
    AGENTS.md / CLAUDE.md rules block into a single markdown report
    under `.nightly/bugs/<timestamp>/report.md`. When `gh` is available
    and `--submit` is on, opens an issue against `--repo` (default:
    upstream Nightly).

    `nightly bug` is a **human-invoked** off-ramp — the agent must
    never run it; doing so would mask the very behavior the operator
    is trying to capture (see AGENTS.md rule 10).
    """
    root = repo_root()
    report = build_bug_report(root=root, title=title, summary=describe)
    written = write_bug_report(report)
    typer.echo(f"✓ wrote report → {_format_path_for_display(written, root)}")
    for extra in report.extra_attachments:
        typer.echo(f"  · attachment: {_format_path_for_display(extra, root)}")

    if not submit:
        typer.echo("· skipping `gh issue create` (--no-submit)")
        typer.echo("  to file later: " + " ".join(bug_gh_command(report, repo=repo)))
        return

    result = submit_bug_report(report, repo=repo)
    if result.ok:
        typer.echo(f"✓ filed issue on {repo}: {result.issue_url or '(url not captured)'}")
        return
    typer.echo(f"✗ {result.error or 'gh issue create failed'}", err=True)
    typer.echo("  report still on disk — file manually with:", err=True)
    typer.echo("    " + " ".join(result.command), err=True)
    raise typer.Exit(code=1)


@app.command(name="doctor")
def doctor_cmd(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Diagnose only — print drift without writing anything.",
        ),
    ] = False,
    scope: Annotated[
        InstallScope,
        typer.Option(help="Which scope to repair — repo-local 'project' or user-global 'user'."),
    ] = "project",
    install_host: Annotated[
        list[str] | None,
        typer.Option(
            "--host",
            help=(
                "Force-install these hosts even if they aren't already present in this repo. "
                "May be passed multiple times. Default: only repair hosts already installed."
            ),
        ),
    ] = None,
    install_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Force-install every supported host. Overrides --host.",
        ),
    ] = False,
) -> None:
    """Repair a drifted Nightly install — scaffold, config, rules, skills.

    Inspired by `gsd-build/get-shit-done`'s idempotent installer pattern
    (https://github.com/gsd-build/get-shit-done) and `brew doctor`'s
    diagnose-and-suggest UX. Walks the install surface and reconciles
    each piece: the `.nightly/` scaffold, `.nightly/config.yml`, the
    AGENTS.md / CLAUDE.md rules block, and every host's full skill set
    (main `/nightly`, `/nightly-conclude`, `/nightly-update`, Stop-hook
    entry). Idempotent — safe to re-run any time.

    By default only hosts already present in this repo get repaired.
    Pass `--host claude --host cursor` (or `--all`) to force-install
    additional hosts. `--dry-run` reports drift without writing.
    """
    root = repo_root()
    extra: tuple[str, ...] = ()
    if install_all:
        extra = tuple(_HOST_LOADERS.keys())
    elif install_host:
        extra = tuple(install_host)

    report = diagnose_and_repair(
        root,
        dry_run=dry_run,
        scope=scope,
        extra_hosts=extra,
    )
    _print_doctor_report(report, root=root)
    if not report.healthy:
        raise typer.Exit(code=1)


def _print_doctor_report(report: DoctorReport, *, root: Path) -> None:
    """Tabular doctor output with a one-line summary."""
    typer.echo(f"repo: {root}")
    if report.dry_run:
        typer.echo("mode: dry-run (no changes written)")
    typer.echo("")
    typer.echo(f"{'status':<10} {'check':<22} detail")
    typer.echo("-" * 70)
    glyph = {
        "ok": "✓ ok",
        "repaired": "✓ fixed",
        "missing": "✗ miss",
        "skipped": "·",
        "error": "✗ err",
        "warning": "⚠ warn",
    }
    for c in report.checks:
        mark = glyph.get(c.status, c.status)
        typer.echo(f"{mark:<10} {c.description:<22} {c.detail}")
    typer.echo("")
    if report.dry_run:
        if report.missing:
            typer.echo(f"would repair: {len(report.missing)} item(s)")
        else:
            typer.echo("all clear — nothing to repair")
    elif report.repaired:
        typer.echo(f"repaired: {len(report.repaired)} item(s)")
    else:
        typer.echo("all clear — install is healthy")
    if report.errors:
        typer.echo(f"errors: {len(report.errors)} item(s) — see detail above", err=True)


@hook_app.command(name="stop")
def hook_stop(
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help=(
                "Wire format. `claude_code` (default, also used by Codex), "
                "`cursor` (Cursor 1.7+ followup_message shape), "
                "`gemini_cli` (Antigravity / Gemini CLI AfterAgent deny shape)."
            ),
        ),
    ] = "claude_code",
) -> None:
    """Stop-hook handler — called by the host on every turn boundary.

    Reads the host's hook payload from stdin (JSON), decides whether to
    force-continue, and writes the decision JSON to stdout in the wire
    shape `fmt` expects. Always logs to `.nightly/runs/<id>/keepalive.log`.
    Never raises — if anything goes wrong, emit `{}` so the host lets
    the session stop rather than trapping the user.
    """
    if fmt not in HOOK_FORMATS:
        typer.echo(
            f"unknown --format '{fmt}'. Valid: {', '.join(HOOK_FORMATS)}",
            err=True,
        )
        raise typer.Exit(code=1)
    root = repo_root()
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    hook_input = parse_hook_input(raw)
    # Claude Code (and Codex, sharing the same shape) sets
    # `stop_hook_active=true` when the session is already continuing as a
    # result of a prior stop-hook block — i.e. this turn boundary is part
    # of a forced-continuation chain, not a host-override warning. The
    # decision logic uses it to track chain depth and pre-write the
    # respawn marker; it is NOT an off-ramp. Coerce defensively: the
    # field may be missing, the wrong type, or a stringified bool
    # depending on the host.
    stop_hook_active = bool(hook_input.get("stop_hook_active"))
    # The transcript path lets the decision logic estimate the live session's
    # context size (last assistant message's usage metadata) for budget
    # steering. Absent/None degrades gracefully to "couldn't measure".
    transcript_path = hook_input.get("transcript_path")
    try:
        decision = compute_stop_hook_decision(
            root,
            stop_hook_active=stop_hook_active,
            transcript_path=transcript_path,
        )
    except Exception as exc:  # hook must never crash the session
        typer.echo(f"nightly hook error: {exc!r}", err=True)
        typer.echo(json.dumps({}))
        return
    # Re-estimate for the heartbeat's ctx= field. The decision logic already
    # persisted this to keepalive.context; re-reading the transcript tail here
    # is cheap relative to the cascade walk and keeps log_heartbeat decoupled
    # from the decision's internals.
    context_tokens = estimate_context_tokens(transcript_path)
    log_heartbeat(decision, root, hook_input=hook_input, context_tokens=context_tokens)
    typer.echo(json.dumps(format_decision(decision, fmt=fmt)))


@hook_app.command(name="session-start")
def hook_session_start() -> None:
    """SessionStart hook handler — re-inject session state after compaction.

    Claude Code fires a `SessionStart` event with `source="compact"` right
    after any compaction (auto or manual), and injects this handler's
    `additionalContext` into the fresh context. That is the sanctioned way to
    make compaction lossless: we render the session digest and hand it back so
    the key state (run id, plans, open PRs, branch, autonomy one-liner)
    survives even when the transcript was summarized away.

    Emits the digest only for an armed Nightly run (`SESSION_ACTIVE` present)
    when `source` is `"compact"` (or missing — we are liberal). For any other
    source, an unarmed/absent run, or any error, emits `{}` so non-Nightly
    sessions stay completely untouched. Never raises.
    """
    import contextlib  # noqa: PLC0415 - lazy
    from datetime import UTC, datetime  # noqa: PLC0415 - lazy

    from nightly_core.digest import render_digest  # noqa: PLC0415 - lazy
    from nightly_core.keepalive_hook import SESSION_ACTIVE_FILENAME  # noqa: PLC0415 - lazy

    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        hook_input = parse_hook_input(raw)
        # An empty parse means no usable payload (empty or garbage stdin) —
        # not a real hook invocation, so inject nothing. A non-empty payload
        # with a missing `source` is tolerated as compact (some host versions
        # omit it); any other explicit source (startup / resume / clear) is
        # left alone.
        if not hook_input:
            typer.echo(json.dumps({}))
            return
        source = hook_input.get("source")
        if source not in (None, "compact"):
            typer.echo(json.dumps({}))
            return

        root = repo_root()
        run = current_run(root)
        if run is None or not (run.path / SESSION_ACTIVE_FILENAME).is_file():
            # No active run, or the session isn't a Nightly-armed one —
            # inject nothing so we never touch a plain interactive session.
            typer.echo(json.dumps({}))
            return

        body = render_digest(root)
        context = "Nightly session state re-injected after context compaction:\n\n" + body
        # Audit trail: record that we re-injected, mirroring the Stop hook's
        # keepalive.log discipline.
        with contextlib.suppress(OSError):
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            sid = hook_input.get("session_id") or "?"
            line = (
                f"{stamp}  decision={'digest_reinject':<16}  "
                f"session={sid}  ctx=?  msg=re-injected digest after "
                f"compaction (source={source or 'missing'}).\n"
            )
            with (run.path / "keepalive.log").open("a", encoding="utf-8") as fh:
                fh.write(line)
        typer.echo(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    }
                }
            )
        )
    except Exception:  # the hook must never crash a session
        typer.echo(json.dumps({}))


if __name__ == "__main__":
    app()
