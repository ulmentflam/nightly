"""Tests for CodexHostIntegration."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from nightly_core import AuthStatus, InstallScope, NightlyHostIntegration
from nightly_host_codex import SKILL_MD, CodexHostIntegration


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def integration(project: Path) -> CodexHostIntegration:
    return CodexHostIntegration(root=project)


def test_is_a_concrete_nightlyhostintegration() -> None:
    assert issubclass(CodexHostIntegration, NightlyHostIntegration)
    instance = CodexHostIntegration(root=Path("/tmp"))
    assert isinstance(instance, NightlyHostIntegration)


def test_host_id(integration: CodexHostIntegration) -> None:
    assert integration.host_id == "codex"


def test_skill_path_project_scope(integration: CodexHostIntegration, project: Path) -> None:
    assert integration.skill_path("project") == (project / ".codex/skills/nightly/SKILL.md")


def test_skill_path_user_scope_is_absolute(integration: CodexHostIntegration) -> None:
    user_path = integration.skill_path("user")
    assert user_path.is_absolute()
    assert user_path.parts[-3:] == ("skills", "nightly", "SKILL.md")


@pytest.mark.asyncio
async def test_install_writes_skill_md_at_project_scope(
    integration: CodexHostIntegration, project: Path
) -> None:
    scope: InstallScope = "project"
    assert not integration.is_installed(scope)

    await integration.install(scope)
    target = integration.skill_path(scope)
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == SKILL_MD
    assert integration.is_installed(scope)

    # Idempotent
    await integration.install(scope)
    assert target.read_text(encoding="utf-8") == SKILL_MD


@pytest.mark.asyncio
async def test_uninstall_removes_skill_and_empty_parents(
    integration: CodexHostIntegration, project: Path
) -> None:
    await integration.install("project")
    target = integration.skill_path("project")
    assert target.is_file()

    await integration.uninstall("project")
    assert not target.exists()
    assert not (project / ".codex/skills/nightly").exists()


@pytest.mark.asyncio
async def test_uninstall_is_idempotent(integration: CodexHostIntegration) -> None:
    await integration.uninstall("project")
    await integration.uninstall("project")


def test_session_id_reads_codex_env(
    integration: CodexHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_SESSION_ID", "codex-abc-123")
    assert integration.session_id() == "codex-abc-123"


def test_session_id_falls_back_to_detached_uuid(
    integration: CodexHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    sid = integration.session_id()
    assert sid.startswith("detached-")
    assert len(sid) > len("detached-")


@pytest.mark.asyncio
async def test_dispatch_sub_agent_raises_phase_5(
    integration: CodexHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 5"):
        await integration.dispatch_sub_agent(role="implementer", prompt="x", cwd="/tmp")


@pytest.mark.asyncio
async def test_request_approval_raises_phase_5(
    integration: CodexHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 5"):
        await integration.request_approval("q?", ["a", "b"])


@pytest.mark.asyncio
async def test_auth_status_without_codex_binary(
    integration: CodexHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    status = await integration.auth_status()
    assert isinstance(status, AuthStatus)
    assert status.ok is False
    assert status.plan is None


# ── Phase 7: run_headless ────────────────────────────────────────────────


def _make_runner(stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0):
    captured: dict[str, Any] = {}

    async def runner(argv, cwd, stdin, timeout_s):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        captured["timeout_s"] = timeout_s
        return stdout, stderr, exit_code

    return runner, captured


@pytest.mark.asyncio
async def test_run_headless_without_codex_binary(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    integration = CodexHostIntegration(root=project)
    result = await integration.run_headless("hi")
    assert result.ok is False
    assert "codex" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_run_headless_uses_safe_sandbox_defaults(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per brainstorm: `--sandbox workspace-write --ask-for-approval never`."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/codex")
    runner, captured = _make_runner(stdout=b'{"role":"assistant"}', exit_code=0)
    integration = CodexHostIntegration(root=project, subprocess_runner=runner)

    result = await integration.run_headless("fix the bug")

    assert result.ok is True
    argv = captured["argv"]
    assert argv[0] == "/usr/local/bin/codex"
    assert "exec" in argv
    assert "--json" in argv
    assert "--sandbox" in argv
    assert "workspace-write" in argv
    assert "--ask-for-approval" in argv
    assert "never" in argv
    assert "fix the bug" in argv  # prompt at the end


@pytest.mark.asyncio
async def test_run_headless_propagates_nonzero_exit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/codex")
    runner, _ = _make_runner(stderr=b"sandbox violation", exit_code=3)
    integration = CodexHostIntegration(root=project, subprocess_runner=runner)
    result = await integration.run_headless("hi")
    assert result.ok is False
    assert result.exit_code == 3
    assert "sandbox violation" in result.stderr


# ── Phase 9i: Stop-hook install + conclude skill ──────────────────────────


@pytest.mark.asyncio
async def test_install_writes_stop_hook_to_codex_hooks_json(tmp_path: Path) -> None:
    import json as _json

    from nightly_host_codex import CodexHostIntegration

    integration = CodexHostIntegration(root=tmp_path)
    await integration.install("project")
    hooks_path = integration.hooks_path()
    assert hooks_path.is_file()
    settings = _json.loads(hooks_path.read_text(encoding="utf-8"))
    cmds = [
        h
        for block in settings["hooks"]["Stop"]
        for h in block.get("hooks", [])
        if h.get("command") == "nightly hook stop"
    ]
    assert len(cmds) == 1
    assert integration.is_keepalive_hook_installed("project")


@pytest.mark.asyncio
async def test_install_writes_conclude_skill(tmp_path: Path) -> None:
    from nightly_host_codex import CodexHostIntegration

    integration = CodexHostIntegration(root=tmp_path)
    await integration.install("project")
    conclude = integration.conclude_skill_path("project")
    assert conclude.is_file()
    content = conclude.read_text(encoding="utf-8")
    assert "name: nightly-conclude" in content
    assert "nightly conclude" in content


@pytest.mark.asyncio
async def test_uninstall_removes_conclude_skill_and_hook(tmp_path: Path) -> None:
    from nightly_host_codex import CodexHostIntegration

    integration = CodexHostIntegration(root=tmp_path)
    await integration.install("project")
    await integration.uninstall("project")
    assert not integration.skill_path("project").exists()
    assert not integration.conclude_skill_path("project").exists()
    assert not integration.is_keepalive_hook_installed("project")
