"""Tests for OpencodeHostIntegration."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from nightly_core import AuthStatus, InstallScope, NightlyHostIntegration
from nightly_host_opencode import SKILL_MD, OpencodeHostIntegration


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def integration(project: Path) -> OpencodeHostIntegration:
    return OpencodeHostIntegration(root=project)


def test_is_a_concrete_nightlyhostintegration() -> None:
    assert issubclass(OpencodeHostIntegration, NightlyHostIntegration)
    instance = OpencodeHostIntegration(root=Path("/tmp"))
    assert isinstance(instance, NightlyHostIntegration)


def test_host_id(integration: OpencodeHostIntegration) -> None:
    assert integration.host_id == "opencode"


def test_skill_path_project_scope(integration: OpencodeHostIntegration, project: Path) -> None:
    assert integration.skill_path("project") == (project / ".opencode/agents/nightly/SKILL.md")


def test_skill_path_user_scope_is_absolute(integration: OpencodeHostIntegration) -> None:
    user_path = integration.skill_path("user")
    assert user_path.is_absolute()
    assert user_path.parts[-3:] == ("agents", "nightly", "SKILL.md")


@pytest.mark.asyncio
async def test_install_writes_skill_md_at_project_scope(
    integration: OpencodeHostIntegration, project: Path
) -> None:
    scope: InstallScope = "project"
    assert not integration.is_installed(scope)

    await integration.install(scope)
    target = integration.skill_path(scope)
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == SKILL_MD
    assert integration.is_installed(scope)

    await integration.install(scope)
    assert target.read_text(encoding="utf-8") == SKILL_MD


@pytest.mark.asyncio
async def test_uninstall_removes_skill_and_empty_parents(
    integration: OpencodeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    target = integration.skill_path("project")
    assert target.is_file()

    await integration.uninstall("project")
    assert not target.exists()
    assert not (project / ".opencode/agents/nightly").exists()


@pytest.mark.asyncio
async def test_uninstall_is_idempotent(integration: OpencodeHostIntegration) -> None:
    await integration.uninstall("project")
    await integration.uninstall("project")


def test_session_id_reads_opencode_env(
    integration: OpencodeHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_SESSION_ID", "opencode-xyz")
    assert integration.session_id() == "opencode-xyz"


def test_session_id_falls_back_to_detached_uuid(
    integration: OpencodeHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    sid = integration.session_id()
    assert sid.startswith("detached-")
    assert len(sid) > len("detached-")


@pytest.mark.asyncio
async def test_dispatch_sub_agent_raises_phase_5(
    integration: OpencodeHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 5"):
        await integration.dispatch_sub_agent(role="implementer", prompt="x", cwd="/tmp")


@pytest.mark.asyncio
async def test_request_approval_raises_phase_5(
    integration: OpencodeHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 5"):
        await integration.request_approval("q?", ["a", "b"])


@pytest.mark.asyncio
async def test_auth_status_without_opencode_binary(
    integration: OpencodeHostIntegration, monkeypatch: pytest.MonkeyPatch
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
async def test_run_headless_without_opencode_binary(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    integration = OpencodeHostIntegration(root=project)
    result = await integration.run_headless("hi")
    assert result.ok is False
    assert "opencode" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_run_headless_builds_opencode_argv(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/opencode")
    runner, captured = _make_runner(stdout=b'{"ok": true}', exit_code=0)
    integration = OpencodeHostIntegration(root=project, subprocess_runner=runner)

    result = await integration.run_headless("fix the bug", cwd=project)

    assert result.ok is True
    argv = captured["argv"]
    assert argv[0] == "/usr/local/bin/opencode"
    assert "run" in argv
    assert "fix the bug" in argv
    assert "--format" in argv
    assert "json" in argv
    assert captured["cwd"] == project


@pytest.mark.asyncio
async def test_run_headless_propagates_nonzero_exit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/opencode")
    runner, _ = _make_runner(stderr=b"provider error", exit_code=4)
    integration = OpencodeHostIntegration(root=project, subprocess_runner=runner)
    result = await integration.run_headless("hi")
    assert result.ok is False
    assert result.exit_code == 4


# ── Phase 9i: conclude skill (no hook — soft keep-alive) ─────────────────


@pytest.mark.asyncio
async def test_install_writes_conclude_agent_for_opencode(tmp_path: Path) -> None:
    from nightly_host_opencode import OpencodeHostIntegration

    integration = OpencodeHostIntegration(root=tmp_path)
    await integration.install("project")
    conclude = integration.conclude_skill_path("project")
    assert conclude.is_file()


def test_opencode_keepalive_support_is_soft() -> None:
    from nightly_host_opencode import OpencodeHostIntegration

    integration = OpencodeHostIntegration(root=Path("/tmp"))
    assert integration.keepalive_support == "soft"
    # No hook should be installed
    assert not integration.is_keepalive_hook_installed("project")


@pytest.mark.asyncio
async def test_opencode_install_writes_update_skill(tmp_path: Path) -> None:
    from nightly_host_opencode import OpencodeHostIntegration

    integration = OpencodeHostIntegration(root=tmp_path)
    await integration.install("project")
    assert integration.update_skill_path("project").is_file()


@pytest.mark.asyncio
async def test_opencode_uninstall_removes_update_skill(tmp_path: Path) -> None:
    from nightly_host_opencode import OpencodeHostIntegration

    integration = OpencodeHostIntegration(root=tmp_path)
    await integration.install("project")
    await integration.uninstall("project")
    assert not integration.update_skill_path("project").exists()
