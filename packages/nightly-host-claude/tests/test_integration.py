"""Tests for ClaudeHostIntegration."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from nightly_core import AuthStatus, InstallScope, NightlyHostIntegration
from nightly_host_claude import SKILL_MD, ClaudeHostIntegration


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def integration(project: Path) -> ClaudeHostIntegration:
    return ClaudeHostIntegration(root=project)


def test_is_a_concrete_NightlyHostIntegration() -> None:
    assert issubclass(ClaudeHostIntegration, NightlyHostIntegration)
    instance = ClaudeHostIntegration(root=Path("/tmp"))
    assert isinstance(instance, NightlyHostIntegration)


def test_host_id(integration: ClaudeHostIntegration) -> None:
    assert integration.host_id == "claude"


def test_skill_path_project_scope(integration: ClaudeHostIntegration, project: Path) -> None:
    assert integration.skill_path("project") == (project / ".claude/skills/nightly/SKILL.md")


def test_skill_path_user_scope_is_absolute(integration: ClaudeHostIntegration) -> None:
    user_path = integration.skill_path("user")
    assert user_path.is_absolute()
    assert user_path.parts[-3:] == ("skills", "nightly", "SKILL.md")


@pytest.mark.asyncio
async def test_install_writes_skill_md_at_project_scope(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    scope: InstallScope = "project"
    assert not integration.is_installed(scope)

    await integration.install(scope)
    target = integration.skill_path(scope)
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == SKILL_MD
    assert integration.is_installed(scope)

    # Idempotent: a second install is a no-op rewrite.
    await integration.install(scope)
    assert target.read_text(encoding="utf-8") == SKILL_MD


@pytest.mark.asyncio
async def test_uninstall_removes_skill_and_empty_parents(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    target = integration.skill_path("project")
    assert target.is_file()

    await integration.uninstall("project")
    assert not target.exists()
    # nightly/ directory cleaned up; skills/ either gone (if empty) or kept
    assert not (project / ".claude/skills/nightly").exists()


@pytest.mark.asyncio
async def test_uninstall_is_idempotent(integration: ClaudeHostIntegration) -> None:
    await integration.uninstall("project")  # noop on a fresh fixture
    await integration.uninstall("project")


def test_session_id_reads_from_claude_code_env(
    integration: ClaudeHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    assert integration.session_id() == "abc-123"


def test_session_id_falls_back_to_detached_uuid(
    integration: ClaudeHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    sid = integration.session_id()
    assert sid.startswith("detached-")
    assert len(sid) > len("detached-")


@pytest.mark.asyncio
async def test_dispatch_sub_agent_raises_phase_2(
    integration: ClaudeHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 2"):
        await integration.dispatch_sub_agent(role="implementer", prompt="x", cwd="/tmp")


@pytest.mark.asyncio
async def test_request_approval_raises_phase_2(
    integration: ClaudeHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 2"):
        await integration.request_approval("q?", ["a", "b"])


@pytest.mark.asyncio
async def test_auth_status_without_claude_binary(
    integration: ClaudeHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    status = await integration.auth_status()
    assert isinstance(status, AuthStatus)
    assert status.ok is False
    assert status.plan is None


# ── Phase 7: run_headless ────────────────────────────────────────────────


def _make_runner(stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0):
    """Build a runner that returns the captured argv for inspection."""
    captured: dict[str, Any] = {}

    async def runner(argv, cwd, stdin, timeout_s):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        captured["stdin"] = stdin
        captured["timeout_s"] = timeout_s
        return stdout, stderr, exit_code

    return runner, captured


@pytest.mark.asyncio
async def test_run_headless_without_claude_binary_surfaces_error(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    integration = ClaudeHostIntegration(root=project)
    result = await integration.run_headless("hi")
    assert result.ok is False
    assert result.error is not None
    assert "claude" in result.error.lower()


@pytest.mark.asyncio
async def test_run_headless_builds_claude_argv(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    runner, captured = _make_runner(stdout=b'{"ok": true}', exit_code=0)
    integration = ClaudeHostIntegration(root=project, subprocess_runner=runner)

    result = await integration.run_headless("fix the bug", cwd=project, timeout_s=30)

    assert result.ok is True
    assert result.output == '{"ok": true}'
    argv = captured["argv"]
    assert argv[0] == "/usr/local/bin/claude"
    assert "-p" in argv
    assert "fix the bug" in argv  # prompt passed as CLI arg
    assert "--output-format" in argv
    assert "json" in argv
    assert "--session-id" in argv
    assert "abc-123" in argv  # session id propagated
    assert "--permission-mode" in argv  # nightly autonomy contract
    assert "acceptEdits" in argv
    assert captured["cwd"] == project
    assert captured["timeout_s"] == 30


@pytest.mark.asyncio
async def test_run_headless_propagates_nonzero_exit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
    runner, _ = _make_runner(stdout=b"", stderr=b"boom", exit_code=2)
    integration = ClaudeHostIntegration(root=project, subprocess_runner=runner)

    result = await integration.run_headless("hi")
    assert result.ok is False
    assert result.exit_code == 2
    assert result.stderr == "boom"
    assert result.error is None  # subprocess ran; just exited non-zero


@pytest.mark.asyncio
async def test_run_headless_handles_timeout(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")

    async def slow_runner(argv, cwd, stdin, timeout_s):
        raise TimeoutError("too slow")

    integration = ClaudeHostIntegration(root=project, subprocess_runner=slow_runner)
    result = await integration.run_headless("hi", timeout_s=0.01)
    assert result.ok is False
    assert "timeout" in (result.error or "").lower()
