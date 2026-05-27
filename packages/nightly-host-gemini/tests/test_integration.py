"""Tests for GeminiHostIntegration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from nightly_core import AuthStatus, InstallScope, NightlyHostIntegration
from nightly_host_gemini import GeminiHostIntegration


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def integration(project: Path) -> GeminiHostIntegration:
    return GeminiHostIntegration(root=project)


def test_is_a_concrete_nightlyhostintegration() -> None:
    assert issubclass(GeminiHostIntegration, NightlyHostIntegration)
    instance = GeminiHostIntegration(root=Path("/tmp"))
    assert isinstance(instance, NightlyHostIntegration)


def test_host_id(integration: GeminiHostIntegration) -> None:
    assert integration.host_id == "gemini"


def test_keepalive_support_is_forced(integration: GeminiHostIntegration) -> None:
    assert integration.keepalive_support == "forced"


def test_skill_path_project_scope_lives_under_dot_gemini_commands(
    integration: GeminiHostIntegration, project: Path
) -> None:
    assert integration.skill_path("project") == (project / ".gemini/commands/nightly.toml")


def test_skill_path_user_scope_is_absolute(integration: GeminiHostIntegration) -> None:
    user_path = integration.skill_path("user")
    assert user_path.is_absolute()
    assert user_path.parts[-3:] == (".gemini", "commands", "nightly.toml")


@pytest.mark.asyncio
async def test_install_writes_toml_command_at_project_scope(
    integration: GeminiHostIntegration, project: Path
) -> None:
    scope: InstallScope = "project"
    assert not integration.is_installed(scope)

    await integration.install(scope)
    target = integration.skill_path(scope)
    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    # TOML shape — description string + triple-quoted prompt
    assert body.startswith('description = "')
    assert 'prompt = """' in body
    # Companion commands all written
    for companion in ("nightly-conclude", "nightly-update", "nightly-bug", "nightly-init"):
        assert (project / f".gemini/commands/{companion}.toml").is_file()


@pytest.mark.asyncio
async def test_install_is_idempotent(integration: GeminiHostIntegration, project: Path) -> None:
    await integration.install("project")
    target = integration.skill_path("project")
    first = target.read_text(encoding="utf-8")
    await integration.install("project")
    assert target.read_text(encoding="utf-8") == first


@pytest.mark.asyncio
async def test_uninstall_removes_main_and_companions(
    integration: GeminiHostIntegration, project: Path
) -> None:
    await integration.install("project")
    await integration.uninstall("project")
    assert not integration.skill_path("project").exists()
    for companion in ("nightly-conclude", "nightly-update", "nightly-bug", "nightly-init"):
        assert not (project / f".gemini/commands/{companion}.toml").exists()


@pytest.mark.asyncio
async def test_uninstall_preserves_unrelated_commands(
    integration: GeminiHostIntegration, project: Path
) -> None:
    """`.gemini/commands/` is shared — other user commands must survive."""
    other = project / ".gemini/commands/my-other.toml"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text('description = "x"\nprompt = "y"\n', encoding="utf-8")

    await integration.install("project")
    await integration.uninstall("project")
    assert other.is_file()
    # `.gemini/commands/` directory itself stays
    assert (project / ".gemini/commands").is_dir()


@pytest.mark.asyncio
async def test_uninstall_is_idempotent(integration: GeminiHostIntegration) -> None:
    await integration.uninstall("project")
    await integration.uninstall("project")


def test_session_id_reads_gemini_session_env(
    integration: GeminiHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_CLI_SESSION_ID", raising=False)
    monkeypatch.setenv("GEMINI_SESSION_ID", "gem-xyz")
    assert integration.session_id() == "gem-xyz"


def test_session_id_falls_back_to_detached_uuid(
    integration: GeminiHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
    monkeypatch.delenv("GEMINI_CLI_SESSION_ID", raising=False)
    sid = integration.session_id()
    assert sid.startswith("detached-")
    assert len(sid) > len("detached-")


@pytest.mark.asyncio
async def test_dispatch_sub_agent_raises_phase_7(
    integration: GeminiHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 7"):
        await integration.dispatch_sub_agent(role="implementer", prompt="x", cwd="/tmp")


@pytest.mark.asyncio
async def test_request_approval_raises_phase_7(
    integration: GeminiHostIntegration,
) -> None:
    with pytest.raises(NotImplementedError, match="Phase 7"):
        await integration.request_approval("q?", ["a", "b"])


@pytest.mark.asyncio
async def test_auth_status_without_gemini_binary(
    integration: GeminiHostIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    status = await integration.auth_status()
    assert isinstance(status, AuthStatus)
    assert status.ok is False
    assert status.plan is None


# ── Stop-hook (AfterAgent) wiring ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_writes_aftergent_hook_to_gemini_settings(
    integration: GeminiHostIntegration, project: Path
) -> None:
    await integration.install("project")
    settings_path = integration.settings_path()
    assert settings_path.is_file()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    cmds = [
        h
        for block in settings["hooks"]["AfterAgent"]
        for h in block.get("hooks", [])
        if h.get("command") == "nightly hook stop --format gemini_cli"
    ]
    assert len(cmds) == 1
    assert integration.is_keepalive_hook_installed("project")


@pytest.mark.asyncio
async def test_install_user_scope_skips_stop_hook(
    integration: GeminiHostIntegration, project: Path
) -> None:
    await integration.install("user")
    # Project-scope settings file should not have been touched
    assert not integration.settings_path().exists()


@pytest.mark.asyncio
async def test_install_stop_hook_is_idempotent(
    integration: GeminiHostIntegration, project: Path
) -> None:
    await integration.install("project")
    await integration.install("project")
    settings = json.loads(integration.settings_path().read_text(encoding="utf-8"))
    cmds = [
        h
        for block in settings["hooks"]["AfterAgent"]
        for h in block.get("hooks", [])
        if h.get("command") == "nightly hook stop --format gemini_cli"
    ]
    assert len(cmds) == 1


@pytest.mark.asyncio
async def test_uninstall_removes_stop_hook(
    integration: GeminiHostIntegration, project: Path
) -> None:
    await integration.install("project")
    assert integration.is_keepalive_hook_installed("project")
    await integration.uninstall("project")
    assert not integration.is_keepalive_hook_installed("project")


@pytest.mark.asyncio
async def test_install_coexists_with_antigravity_hook(
    integration: GeminiHostIntegration, project: Path
) -> None:
    """`gemini` and `antigravity` share `.gemini/settings.json` — co-install must dedupe."""
    from nightly_host_antigravity import AntigravityHostIntegration

    antigravity = AntigravityHostIntegration(root=project)
    await antigravity.install("project")
    await integration.install("project")

    settings = json.loads(integration.settings_path().read_text(encoding="utf-8"))
    cmds = [
        h
        for block in settings["hooks"]["AfterAgent"]
        for h in block.get("hooks", [])
        if h.get("command") == "nightly hook stop --format gemini_cli"
    ]
    # Both hosts merge the same command — the merger must dedupe.
    assert len(cmds) == 1


# ── companion skills (conclude, update, bug, init) ────────────────────────


@pytest.mark.asyncio
async def test_install_writes_companion_skills(
    integration: GeminiHostIntegration, project: Path
) -> None:
    await integration.install("project")
    for path_fn, marker in (
        (integration.conclude_skill_path, "nightly-conclude"),
        (integration.update_skill_path, "nightly-update"),
        (integration.bug_skill_path, "nightly-bug"),
        (integration.init_skill_path, "nightly-init"),
    ):
        path = path_fn("project")
        assert path.is_file()
        body = path.read_text(encoding="utf-8")
        assert body.startswith('description = "')
        # Companion content references its own slash command in the prompt body
        assert marker in body


@pytest.mark.asyncio
async def test_init_skill_installed_flag(integration: GeminiHostIntegration, project: Path) -> None:
    assert not integration.is_init_installed("project")
    await integration.install("project")
    assert integration.is_init_installed("project")
