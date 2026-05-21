"""Tests for CursorHostIntegration."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nightly_core import AuthStatus, InstallScope, NightlyHostIntegration
from nightly_host_cursor import SKILL_MD, CursorHostIntegration


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def integration(project: Path) -> CursorHostIntegration:
    return CursorHostIntegration(root=project)


def test_is_a_concrete_nightlyhostintegration() -> None:
    assert issubclass(CursorHostIntegration, NightlyHostIntegration)
    instance = CursorHostIntegration(root=Path("/tmp"))
    assert isinstance(instance, NightlyHostIntegration)


def test_host_id(integration: CursorHostIntegration) -> None:
    assert integration.host_id == "cursor"


def test_skill_path_is_flat_file_not_folder(
    integration: CursorHostIntegration, project: Path
) -> None:
    """Cursor commands are single .md files, not a folder per command."""
    project_path = integration.skill_path("project")
    assert project_path == project / ".cursor/commands/nightly.md"
    # explicitly flat — no nested `nightly/` directory
    assert project_path.parent.name == "commands"


def test_skill_path_user_scope_is_absolute(integration: CursorHostIntegration) -> None:
    user_path = integration.skill_path("user")
    assert user_path.is_absolute()
    assert user_path.parts[-2:] == ("commands", "nightly.md")


@pytest.mark.asyncio
async def test_install_writes_command_file_at_project_scope(
    integration: CursorHostIntegration, project: Path
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
async def test_uninstall_removes_file_only_not_parent_dir(
    integration: CursorHostIntegration, project: Path
) -> None:
    """`.cursor/commands/` may contain other commands — must not be touched."""
    # Pretend the user has another command installed alongside ours.
    other = project / ".cursor/commands/explain.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("# explain", encoding="utf-8")

    await integration.install("project")
    target = integration.skill_path("project")
    assert target.is_file()

    await integration.uninstall("project")
    assert not target.exists()
    # The shared commands/ directory and the user's other command remain.
    assert other.exists()
    assert (project / ".cursor/commands").is_dir()


@pytest.mark.asyncio
async def test_uninstall_is_idempotent(integration: CursorHostIntegration) -> None:
    await integration.uninstall("project")
    await integration.uninstall("project")


def test_session_id_reads_cursor_env(
    integration: CursorHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURSOR_SESSION_ID", "cursor-abc-123")
    assert integration.session_id() == "cursor-abc-123"


def test_session_id_falls_back_to_detached_uuid(
    integration: CursorHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CURSOR_SESSION_ID", raising=False)
    sid = integration.session_id()
    assert sid.startswith("detached-")
    assert len(sid) > len("detached-")


@pytest.mark.asyncio
async def test_dispatch_sub_agent_raises_phase_7(
    integration: CursorHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 7"):
        await integration.dispatch_sub_agent(role="implementer", prompt="x", cwd="/tmp")


@pytest.mark.asyncio
async def test_request_approval_raises_phase_7(
    integration: CursorHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 7"):
        await integration.request_approval("q?", ["a", "b"])


@pytest.mark.asyncio
async def test_auth_status_without_cursor_agent_binary(
    integration: CursorHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    status = await integration.auth_status()
    assert isinstance(status, AuthStatus)
    assert status.ok is False
    assert status.plan is None
