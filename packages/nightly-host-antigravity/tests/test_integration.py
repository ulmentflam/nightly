"""Tests for AntigravityHostIntegration."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightly_core import AuthStatus, InstallScope, NightlyHostIntegration
from nightly_host_antigravity import SKILL_MD, AntigravityHostIntegration


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def integration(project: Path) -> AntigravityHostIntegration:
    return AntigravityHostIntegration(root=project)


def test_is_a_concrete_nightlyhostintegration() -> None:
    assert issubclass(AntigravityHostIntegration, NightlyHostIntegration)
    instance = AntigravityHostIntegration(root=Path("/tmp"))
    assert isinstance(instance, NightlyHostIntegration)


def test_host_id(integration: AntigravityHostIntegration) -> None:
    assert integration.host_id == "antigravity"


def test_skill_path_project_scope(integration: AntigravityHostIntegration, project: Path) -> None:
    subfolder = (
        "antigravity-cli"
        if "antigravity-cli" in str(integration.skill_path("project"))
        else "antigravity"
    )
    assert integration.skill_path("project") == (
        project / f".gemini/{subfolder}/agents/nightly/SKILL.md"
    )


def test_skill_path_user_scope_is_absolute(integration: AntigravityHostIntegration) -> None:
    user_path = integration.skill_path("user")
    assert user_path.is_absolute()
    assert user_path.parts[-3:] == ("agents", "nightly", "SKILL.md")
    assert any(x in user_path.parts for x in ("antigravity", "antigravity-cli"))


@pytest.mark.asyncio
async def test_install_writes_skill_md_at_project_scope(
    integration: AntigravityHostIntegration, project: Path
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
async def test_uninstall_removes_skill_and_empty_parents_up_to_agents(
    integration: AntigravityHostIntegration, project: Path
) -> None:
    await integration.install("project")
    target = integration.skill_path("project")
    assert target.is_file()

    await integration.uninstall("project")
    assert not target.exists()
    # nightly/ + agents/ cleaned up when empty
    assert not target.parent.exists()


@pytest.mark.asyncio
async def test_uninstall_preserves_sibling_agents(
    integration: AntigravityHostIntegration, project: Path
) -> None:
    """The agents directory may hold other agents — leave them."""
    target = integration.skill_path("project")
    sibling = target.parent.parent / "other" / "agent.md"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_text("# other agent", encoding="utf-8")

    await integration.install("project")
    await integration.uninstall("project")

    # nightly/ cleaned up; the sibling agent and its parent dir remain
    assert not target.parent.exists()
    assert sibling.exists()
    assert sibling.parent.is_dir()


@pytest.mark.asyncio
async def test_uninstall_is_idempotent(integration: AntigravityHostIntegration) -> None:
    await integration.uninstall("project")
    await integration.uninstall("project")


def test_session_id_reads_antigravity_env(
    integration: AntigravityHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGRAVITY_SESSION_ID", "ag-xyz")
    assert integration.session_id() == "ag-xyz"


def test_session_id_falls_back_to_gemini_env(
    integration: AntigravityHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTIGRAVITY_SESSION_ID", raising=False)
    monkeypatch.setenv("GEMINI_SESSION_ID", "gem-abc")
    assert integration.session_id() == "gem-abc"


def test_session_id_falls_back_to_detached_uuid(
    integration: AntigravityHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTIGRAVITY_SESSION_ID", raising=False)
    monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
    sid = integration.session_id()
    assert sid.startswith("detached-")
    assert len(sid) > len("detached-")


@pytest.mark.asyncio
async def test_dispatch_sub_agent_raises_phase_7(
    integration: AntigravityHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 7"):
        await integration.dispatch_sub_agent(role="implementer", prompt="x", cwd="/tmp")


@pytest.mark.asyncio
async def test_request_approval_raises_phase_7(
    integration: AntigravityHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 7"):
        await integration.request_approval("q?", ["a", "b"])


@pytest.mark.asyncio
async def test_auth_status_without_antigravity_home(
    integration: AntigravityHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `~/.gemini/antigravity/` doesn't exist, auth is unknown."""
    # Override the module-level constant by patching the integration's
    # detection target. We can't reach into the real home dir at test time
    # — use a sentinel path that definitely doesn't exist.
    monkeypatch.setattr(
        "nightly_host_antigravity.integration._ANTIGRAVITY_HOME",
        Path("/nonexistent/path/.gemini/antigravity"),
    )
    status = await integration.auth_status()
    assert isinstance(status, AuthStatus)
    assert status.ok is False


@pytest.mark.asyncio
async def test_auth_status_with_antigravity_home(
    integration: AntigravityHostIntegration,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the directory exists, treat as ok with `unknown` plan."""
    fake_home = tmp_path / ".gemini" / "antigravity"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(
        "nightly_host_antigravity.integration._ANTIGRAVITY_HOME",
        fake_home,
    )
    status = await integration.auth_status()
    assert status.ok is True
    assert status.plan == "unknown"


# ── Phase 9i: Stop-hook install + conclude skill ──────────────────────────


@pytest.mark.asyncio
async def test_install_writes_aftergent_hook_to_gemini_settings(tmp_path: Path) -> None:
    import json as _json

    from nightly_host_antigravity import AntigravityHostIntegration

    integration = AntigravityHostIntegration(root=tmp_path)
    await integration.install("project")
    settings_path = integration.settings_path()
    assert settings_path.is_file()
    settings = _json.loads(settings_path.read_text(encoding="utf-8"))
    cmds = [
        h
        for block in settings["hooks"]["AfterAgent"]
        for h in block.get("hooks", [])
        if h.get("command") == "nightly hook stop --format gemini_cli"
    ]
    assert len(cmds) == 1
    assert integration.is_keepalive_hook_installed("project")


@pytest.mark.asyncio
async def test_antigravity_install_writes_all_skills(tmp_path: Path) -> None:
    from nightly_host_antigravity import AntigravityHostIntegration

    integration = AntigravityHostIntegration(root=tmp_path)
    await integration.install("project")

    assert integration.skill_path("project").is_file()
    assert integration.conclude_skill_path("project").is_file()
    assert integration.update_skill_path("project").is_file()
    assert integration.bug_skill_path("project").is_file()
    assert integration.init_skill_path("project").is_file()

    # Verify parent directory names conform to expected skill names
    assert integration.conclude_skill_path("project").parent.name == "nightly-conclude"
    assert integration.update_skill_path("project").parent.name == "nightly-update"
    assert integration.bug_skill_path("project").parent.name == "nightly-bug"
    assert integration.init_skill_path("project").parent.name == "nightly-init"


@pytest.mark.asyncio
async def test_antigravity_uninstall_removes_all_skills(tmp_path: Path) -> None:
    from nightly_host_antigravity import AntigravityHostIntegration

    integration = AntigravityHostIntegration(root=tmp_path)
    await integration.install("project")
    await integration.uninstall("project")

    assert not integration.skill_path("project").exists()
    assert not integration.conclude_skill_path("project").exists()
    assert not integration.update_skill_path("project").exists()
    assert not integration.bug_skill_path("project").exists()
    assert not integration.init_skill_path("project").exists()
    assert not integration.is_keepalive_hook_installed("project")
