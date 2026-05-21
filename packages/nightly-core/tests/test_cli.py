"""Tests for the nightly CLI (Phase 1 + Phase 2 commands)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core.cli import app

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean tmp dir acting as the repo for a CLI test."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── Phase 1 commands ──────────────────────────────────────────────────────


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "nightly 0.0.1" in result.stdout


def test_info_command_mentions_phase_and_design_doc() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "Phase" in result.stdout
    assert "brainstorm.html" in result.stdout


def test_init_bootstraps_nightly_and_installs_skill(repo: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    for sub in ("runs", "plans", "atlas", "memory", "prompts"):
        assert (repo / ".nightly" / sub).is_dir(), f"missing .nightly/{sub}"
    assert (repo / ".nightly" / "config.yml").is_file()
    assert (repo / ".claude" / "skills" / "nightly" / "SKILL.md").is_file()
    assert "✓ installed claude skill" in result.output


def test_init_is_idempotent(repo: Path) -> None:
    first = runner.invoke(app, ["init"])
    second = runner.invoke(app, ["init"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "already present" in second.output


def test_init_does_not_clobber_existing_config(repo: Path) -> None:
    nightly = repo / ".nightly"
    nightly.mkdir()
    custom = "# user-customized config\nhosts: [claude]\nfoo: bar\n"
    (nightly / "config.yml").write_text(custom, encoding="utf-8")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (nightly / "config.yml").read_text(encoding="utf-8") == custom


def test_init_rejects_invalid_host_via_typer_literal(repo: Path) -> None:
    """Typer's literal validation rejects unknown hosts before reaching our code."""
    result = runner.invoke(app, ["init", "--host", "bogus"])
    assert result.exit_code != 0


def test_init_codex_installs_codex_skill(repo: Path) -> None:
    result = runner.invoke(app, ["init", "--host", "codex"])
    assert result.exit_code == 0, result.output
    assert (repo / ".codex" / "skills" / "nightly" / "SKILL.md").is_file()
    assert "✓ installed codex skill" in result.output


def test_init_opencode_installs_opencode_skill(repo: Path) -> None:
    result = runner.invoke(app, ["init", "--host", "opencode"])
    assert result.exit_code == 0, result.output
    assert (repo / ".opencode" / "agents" / "nightly" / "SKILL.md").is_file()
    assert "✓ installed opencode skill" in result.output


def test_init_cursor_installs_flat_command_file(repo: Path) -> None:
    """Phase 6: Cursor commands are a single .md file, not a folder."""
    result = runner.invoke(app, ["init", "--host", "cursor"])
    assert result.exit_code == 0, result.output
    target = repo / ".cursor" / "commands" / "nightly.md"
    assert target.is_file()
    # No `nightly/` subdirectory — flat slash-command file
    assert not (repo / ".cursor" / "commands" / "nightly").exists()
    assert "✓ installed cursor skill" in result.output


def test_init_antigravity_installs_managed_agent_skill(repo: Path) -> None:
    result = runner.invoke(app, ["init", "--host", "antigravity"])
    assert result.exit_code == 0, result.output
    assert (repo / ".gemini" / "antigravity" / "agents" / "nightly" / "SKILL.md").is_file()
    assert "✓ installed antigravity skill" in result.output


def test_uninstall_codex_removes_codex_skill(repo: Path) -> None:
    runner.invoke(app, ["init", "--host", "codex"])
    skill = repo / ".codex" / "skills" / "nightly" / "SKILL.md"
    assert skill.is_file()

    result = runner.invoke(app, ["uninstall", "--host", "codex"])
    assert result.exit_code == 0
    assert not skill.exists()
    assert "✓ removed codex skill" in result.output


def test_uninstall_opencode_removes_opencode_skill(repo: Path) -> None:
    runner.invoke(app, ["init", "--host", "opencode"])
    skill = repo / ".opencode" / "agents" / "nightly" / "SKILL.md"
    assert skill.is_file()

    result = runner.invoke(app, ["uninstall", "--host", "opencode"])
    assert result.exit_code == 0
    assert not skill.exists()


def test_uninstall_cursor_preserves_sibling_commands(repo: Path) -> None:
    """`.cursor/commands/` is shared with other commands — leave it alone."""
    runner.invoke(app, ["init", "--host", "cursor"])
    other = repo / ".cursor" / "commands" / "explain.md"
    other.write_text("# explain command\n", encoding="utf-8")

    result = runner.invoke(app, ["uninstall", "--host", "cursor"])
    assert result.exit_code == 0
    assert not (repo / ".cursor" / "commands" / "nightly.md").exists()
    assert other.exists()  # sibling command untouched


def test_uninstall_antigravity_removes_managed_agent(repo: Path) -> None:
    runner.invoke(app, ["init", "--host", "antigravity"])
    skill = repo / ".gemini" / "antigravity" / "agents" / "nightly" / "SKILL.md"
    assert skill.is_file()

    result = runner.invoke(app, ["uninstall", "--host", "antigravity"])
    assert result.exit_code == 0
    assert not skill.exists()


def test_status_shows_all_five_hosts(repo: Path) -> None:
    """Phase 6: status lists every supported host."""
    runner.invoke(app, ["init"])  # default = claude
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    for host in ("claude", "codex", "opencode", "cursor", "antigravity"):
        assert host in result.output, f"status should list host {host}"


def test_status_independent_install_per_host(repo: Path) -> None:
    runner.invoke(app, ["init", "--host", "claude"])
    runner.invoke(app, ["init", "--host", "cursor"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    claude_line = next(line for line in lines if "claude" in line and "project" in line)
    cursor_line = next(line for line in lines if "cursor" in line and "project" in line)
    codex_line = next(line for line in lines if "codex" in line and "project" in line)
    assert "✓" in claude_line
    assert "✓" in cursor_line
    # Hosts we didn't install → ✗
    assert "✗" in codex_line


# ── Phase 5 commands ──────────────────────────────────────────────────────


def _eligible_proposal(score: float = 3.0, title: str = "apply F401"):
    from nightly_core.proposers.base import Proposal

    return Proposal(
        proposer="lint_debt",
        category="lint_debt",
        title=title,
        body=f"# {title}\n\nbody",
        score=score,
        file_scope=("src/a.py",),
        estimated_loc=4,
    )


def _ineligible_proposal(score: float = 5.0, title: str = "audit TODOs"):
    from nightly_core.proposers.base import Proposal

    return Proposal(
        proposer="todo_fixme",
        category="todo_audit",
        title=title,
        body=f"# {title}\n\nbody",
        score=score,
        file_scope=("src/a.py", "src/b.py"),
        estimated_loc=12,
    )


def test_propose_empty_when_no_proposals(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["propose"])
    assert result.exit_code == 0
    assert "no proposals" in result.output.lower()


def test_propose_lists_ranked_proposals(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner.invoke(app, ["init"])
    monkeypatch.setattr(
        "nightly_core.cli.run_proposers",
        lambda _root, **_: [
            _eligible_proposal(score=4.5, title="high lint"),
            _ineligible_proposal(score=3.0, title="audit todos"),
            _eligible_proposal(score=1.5, title="low lint"),
        ],
    )
    result = runner.invoke(app, ["propose"])
    assert result.exit_code == 0
    # Header
    assert "score" in result.output
    assert "auto" in result.output
    # Eligible rows marked ok; ineligible marked skip
    assert " ok" in result.output
    assert "skip" in result.output
    # Highest score appears first
    lines = [
        line for line in result.output.splitlines() if "lint_debt" in line or "todo_fixme" in line
    ]
    assert "high lint" in lines[0]


def test_ideate_requires_active_run(repo: Path) -> None:
    runner.invoke(app, ["init"])  # no `start`
    result = runner.invoke(app, ["ideate"])
    assert result.exit_code != 0
    assert "no active run" in result.output


def test_ideate_writes_drafts(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    monkeypatch.setattr(
        "nightly_core.cli.run_proposers",
        lambda _root, **_: [
            _eligible_proposal(score=4.5, title="apply F401"),
            _ineligible_proposal(score=3.0, title="audit TODOs across 4 files"),
        ],
    )
    result = runner.invoke(app, ["ideate"])
    assert result.exit_code == 0
    assert "wrote 2 proposal" in result.output
    assert "1 auto-PR-eligible" in result.output

    run_id = (repo / ".nightly/runs/CURRENT").read_text(encoding="utf-8").strip()
    issues_dir = repo / ".nightly" / "runs" / run_id / "proposed" / "issues"
    drafts = sorted(issues_dir.glob("[0-9][0-9][0-9]-*.md"))
    assert len(drafts) == 2
    assert drafts[0].name.startswith("001-")
    assert drafts[1].name.startswith("002-")


def test_ideate_empty_when_no_proposals(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    monkeypatch.setattr("nightly_core.cli.run_proposers", lambda _root, **_: [])
    result = runner.invoke(app, ["ideate"])
    assert result.exit_code == 0
    assert "no proposals" in result.output.lower()


def test_uninstall_removes_skill(repo: Path) -> None:
    runner.invoke(app, ["init"])
    skill = repo / ".claude" / "skills" / "nightly" / "SKILL.md"
    assert skill.is_file()

    result = runner.invoke(app, ["uninstall"])
    assert result.exit_code == 0
    assert not skill.exists()
    assert "✓ removed claude skill" in result.output


def test_uninstall_when_not_installed_is_a_noop(repo: Path) -> None:
    result = runner.invoke(app, ["uninstall"])
    assert result.exit_code == 0
    assert "not installed" in result.output


def test_status_before_init_reports_missing(repo: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "✗" in result.output
    assert "nightly init" in result.output


def test_status_after_init_reports_ok(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert ".nightly/:  ✓" in result.output
    assert "claude" in result.output


# ── Phase 2 commands ──────────────────────────────────────────────────────


def test_start_requires_init(repo: Path) -> None:
    result = runner.invoke(app, ["start"])
    assert result.exit_code != 0
    assert "nightly init" in result.output


def test_start_creates_run(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 0
    assert "✓ started run" in result.output
    pointer = repo / ".nightly" / "runs" / "CURRENT"
    assert pointer.is_file()
    run_id = pointer.read_text(encoding="utf-8").strip()
    assert (repo / ".nightly" / "runs" / run_id / "tasks").is_dir()


def test_start_with_task_seeds_first_task(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["start", "Fix login bug"])
    assert result.exit_code == 0
    assert "✓ seeded task" in result.output
    run_id = (repo / ".nightly/runs/CURRENT").read_text(encoding="utf-8").strip()
    task_dir = repo / ".nightly" / "runs" / run_id / "tasks" / "0001-fix-login-bug"
    assert (task_dir / "plan.md").is_file()


def test_conclude_without_run(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["conclude"])
    assert result.exit_code != 0
    assert "no active run" in result.output


def test_conclude_marks_run(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    result = runner.invoke(app, ["conclude"])
    assert result.exit_code == 0
    assert "marked concluding" in result.output
    run_id = (repo / ".nightly/runs/CURRENT").read_text(encoding="utf-8").strip()
    assert (repo / ".nightly" / "runs" / run_id / "CONCLUDE").is_file()


def test_conclude_idempotent(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    runner.invoke(app, ["conclude"])
    second = runner.invoke(app, ["conclude"])
    assert second.exit_code == 0
    assert "already concluded" in second.output


def test_task_requires_active_run(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["task", "alpha"])
    assert result.exit_code != 0
    assert "no active run" in result.output


def test_task_creates_directory(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    result = runner.invoke(app, ["task", "add-retry", "-d", "Add retry budget"])
    assert result.exit_code == 0
    run_id = (repo / ".nightly/runs/CURRENT").read_text(encoding="utf-8").strip()
    task_dir = repo / ".nightly" / "runs" / run_id / "tasks" / "0001-add-retry"
    assert task_dir.is_dir()
    assert "Add retry budget" in (task_dir / "plan.md").read_text(encoding="utf-8")


def test_specialist_prints_prompt() -> None:
    for role in ("implementer", "tester", "reviewer", "researcher"):
        result = runner.invoke(app, ["specialist", role])
        assert result.exit_code == 0
        assert len(result.stdout) > 100
        assert role in result.stdout.lower() or "specialist" in result.stdout.lower()


def test_specialist_rejects_invalid_role(repo: Path) -> None:
    result = runner.invoke(app, ["specialist", "bogus"])
    assert result.exit_code != 0


def test_brief_requires_run(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["brief"])
    assert result.exit_code != 0
    assert "no active run" in result.output


def test_brief_renders_briefing(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start", "Fix login bug"])
    result = runner.invoke(app, ["brief"])
    assert result.exit_code == 0
    run_id = (repo / ".nightly/runs/CURRENT").read_text(encoding="utf-8").strip()
    briefing = repo / ".nightly" / "runs" / run_id / "briefing.html"
    assert briefing.is_file()
    html = briefing.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert run_id in html


def test_brief_unknown_run(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["brief", "--run", "2026-01-01T00-00-00Z"])
    assert result.exit_code != 0
    assert "no such run" in result.output


def test_status_shows_active_run(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "active" in result.output


def test_status_shows_concluded_run(repo: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    runner.invoke(app, ["conclude"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "concluded" in result.output


# ── Phase 3 commands ──────────────────────────────────────────────────────


def test_next_in_empty_repo_reports_nothing(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["next"])
    assert result.exit_code == 0
    assert "source:   nothing" in result.output
    assert "no work" in result.output.lower() or "backlog" in result.output.lower()


def test_next_picks_in_flight_plan(repo: Path) -> None:
    """Cascade returns resume_in_flight when an `in_progress` plan exists."""
    from nightly_core.plans import update_plan_status
    from nightly_core.runs import current_run, new_task

    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    run = current_run(repo)
    assert run is not None
    task = new_task(run, slug="alpha")
    update_plan_status(task.path / "plan.md", "in_progress")

    result = runner.invoke(app, ["next"])
    assert result.exit_code == 0
    assert "source:   resume_in_flight" in result.output
    assert "alpha" in result.output


def test_next_picks_rfc_when_only_planning_work(repo: Path) -> None:
    runner.invoke(app, ["init"])
    rfcs = repo / ".planning" / "rfcs"
    rfcs.mkdir(parents=True)
    (rfcs / "001-retry.md").write_text(
        "---\nstatus: accepted\n---\n# RFC\n\n- [ ] add a knob\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["next"])
    assert result.exit_code == 0
    assert "source:   accepted_rfc" in result.output
    assert "add a knob" in result.output


def test_next_picks_github_issue_when_no_local_work(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from nightly_core.triage import IssueRecord

    runner.invoke(app, ["init"])
    issues = [
        IssueRecord(
            number=42,
            title="Fix the login bug",
            body="A reasonable acceptance criterion explaining what's broken.",
            labels=("nightly-ready",),
            created_at=datetime(2026, 4, 20, tzinfo=UTC),
            updated_at=datetime(2026, 4, 20, tzinfo=UTC),
            url="https://github.com/x/y/issues/42",
            author="alice",
        )
    ]
    monkeypatch.setattr("nightly_core.triage.fetch_via_gh", lambda _root, **_: issues)
    result = runner.invoke(app, ["next"])
    assert result.exit_code == 0
    assert "source:   github_issue" in result.output
    assert "42" in result.output


def test_triage_empty_when_no_issues(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["triage"])
    assert result.exit_code == 0
    assert "no open issues" in result.output.lower()


def test_triage_lists_ranked_issues(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from nightly_core.triage import IssueRecord

    runner.invoke(app, ["init"])
    issues = [
        IssueRecord(
            number=1,
            title="Plain issue",
            body="Body that is long enough to count as having criteria.",
            labels=(),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
            url="x",
            author="alice",
        ),
        IssueRecord(
            number=2,
            title="High-priority nightly task",
            body="Body that is long enough to count as having criteria.",
            labels=("nightly-ready",),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
            url="x",
            author="alice",
        ),
        IssueRecord(
            number=3,
            title="Do not touch",
            body="Body that is long enough to count as having criteria.",
            labels=("do-not-automate",),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
            url="x",
            author="alice",
        ),
    ]
    monkeypatch.setattr("nightly_core.triage.fetch_via_gh", lambda _root, **_: issues)
    result = runner.invoke(app, ["triage"])
    assert result.exit_code == 0
    # #2 (nightly-ready) ranks above #1 (default); #3 is skipped at the bottom
    lines = [line for line in result.output.splitlines() if line.startswith(" ")]
    # rough check: first listed eligible row is #2
    assert "#2" in lines[0]
    assert "skip" in result.output.lower()
    assert "do-not-automate" in result.output


def test_plans_when_empty(repo: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["plans"])
    assert result.exit_code == 0
    assert "no plans" in result.output.lower()


def test_plans_lists_status_across_runs(repo: Path) -> None:
    from nightly_core.plans import update_plan_status
    from nightly_core.runs import current_run, new_task

    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    run = current_run(repo)
    assert run is not None
    alpha = new_task(run, slug="alpha")
    beta = new_task(run, slug="beta")
    update_plan_status(alpha.path / "plan.md", "in_progress")
    update_plan_status(beta.path / "plan.md", "done")

    result = runner.invoke(app, ["plans"])
    assert result.exit_code == 0
    assert "0001-alpha" in result.output
    assert "0002-beta" in result.output
    assert "in_progress" in result.output
    assert "done" in result.output


# ── Phase 7: headless ────────────────────────────────────────────────────


def test_headless_default_host_missing_binary(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No claude binary on PATH → exit code 1 with a clear error."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = runner.invoke(app, ["headless", "hello world"])
    assert result.exit_code == 1
    assert "claude" in result.output.lower()


def test_headless_invokes_chosen_host(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The --host flag should route to the named host's integration."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = runner.invoke(app, ["headless", "hi", "--host", "codex"])
    assert result.exit_code == 1
    assert "codex" in result.output.lower()


def test_headless_unimplemented_for_secondary_host(repo: Path) -> None:
    """Cursor + Antigravity inherit the ABC default — non-zero exit."""
    result = runner.invoke(app, ["headless", "hi", "--host", "cursor"])
    assert result.exit_code != 0


def test_headless_typer_rejects_invalid_host(repo: Path) -> None:
    """Typer's literal validation catches misspellings before we run."""
    result = runner.invoke(app, ["headless", "hi", "--host", "bogus"])
    assert result.exit_code != 0


# ── Phase 8: nightly run ─────────────────────────────────────────────────


def test_run_command_empty_when_cascade_returns_nothing(repo: Path) -> None:
    """No in-flight plans, no proposers fire → loop returns []. Exit clean."""
    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])  # creates a run with no tasks

    result = runner.invoke(app, ["run", "--max-tasks", "1"])
    assert result.exit_code == 0
    assert "no work dispatched" in result.output


def test_run_command_dispatches_tasks(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed two in-flight plans, mock the driver to confirm CLI plumbing."""
    from nightly_core.driver import TaskOutcome
    from nightly_core.headless import HeadlessResult

    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])

    async def fake_run_loop(*, root, host, config, git_runner=None):
        return [
            TaskOutcome(
                plan_path=root / ".nightly" / "runs" / "x" / "tasks" / "0001-alpha" / "plan.md",
                worktree=None,
                headless=HeadlessResult(host_id="claude", output="ok", exit_code=0, elapsed_ms=42),
                cascade_source="resume_in_flight",
                final_status="done",
                error=None,
            )
        ]

    monkeypatch.setattr("nightly_core.cli.run_loop", fake_run_loop)
    result = runner.invoke(app, ["run", "--max-tasks", "1"])
    assert result.exit_code == 0
    assert "dispatched 1 task(s)" in result.output
    assert "0001-alpha" in result.output
    assert "resume_in_flight" in result.output


def test_run_command_surfaces_parked_outcomes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nightly_core.driver import TaskOutcome
    from nightly_core.headless import HeadlessResult

    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])

    async def fake_run_loop(*, root, host, config, git_runner=None):
        return [
            TaskOutcome(
                plan_path=root / ".nightly" / "runs" / "x" / "tasks" / "0001-alpha" / "plan.md",
                worktree=None,
                headless=HeadlessResult(host_id="claude", output="", exit_code=1, elapsed_ms=12),
                cascade_source="resume_in_flight",
                final_status="parked",
                error=None,
            )
        ]

    monkeypatch.setattr("nightly_core.cli.run_loop", fake_run_loop)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "parked" in result.output
    # Parked tasks get a · marker rather than ✓
    assert "·" in result.output


def test_run_command_concurrency_passes_through(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --concurrency flag should land on DriverConfig."""
    captured: dict[str, object] = {}

    async def fake_run_loop(*, root, host, config, git_runner=None):
        captured["concurrency"] = config.concurrency
        captured["max_tasks"] = config.max_tasks
        return []

    runner.invoke(app, ["init"])
    runner.invoke(app, ["start"])
    monkeypatch.setattr("nightly_core.cli.run_loop", fake_run_loop)
    result = runner.invoke(app, ["run", "--concurrency", "3", "--max-tasks", "7"])
    assert result.exit_code == 0
    assert captured["concurrency"] == 3
    assert captured["max_tasks"] == 7


def test_run_command_clamps_concurrency_to_minimum_one(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--concurrency 0 (or negative) should clamp to 1, not deadlock the semaphore."""
    captured: dict[str, object] = {}

    async def fake_run_loop(*, root, host, config, git_runner=None):
        captured["concurrency"] = config.concurrency
        return []

    runner.invoke(app, ["init"])
    monkeypatch.setattr("nightly_core.cli.run_loop", fake_run_loop)
    runner.invoke(app, ["run", "--concurrency", "0"])
    assert captured["concurrency"] == 1


def test_run_command_typer_rejects_invalid_host(repo: Path) -> None:
    result = runner.invoke(app, ["run", "--host", "bogus"])
    assert result.exit_code != 0
