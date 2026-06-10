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


# ── Phase 9h: Stop-hook merge into settings.local.json ────────────────────


@pytest.mark.asyncio
async def test_install_project_writes_stop_hook(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    import json as _json

    await integration.install("project")
    settings = _json.loads(integration.settings_local_path().read_text(encoding="utf-8"))
    assert "hooks" in settings
    assert "Stop" in settings["hooks"]
    block = settings["hooks"]["Stop"][0]
    cmd_entries = [h for h in block["hooks"] if h.get("command") == "nightly hook stop"]
    assert len(cmd_entries) == 1
    assert integration.is_stop_hook_installed()


@pytest.mark.asyncio
async def test_install_user_scope_skips_stop_hook(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    # User-scope install shouldn't drop a per-repo hook in the project tree.
    await integration.install("user")
    assert not integration.settings_local_path().exists()


@pytest.mark.asyncio
async def test_install_stop_hook_is_idempotent(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    import json as _json

    await integration.install("project")
    await integration.install("project")
    settings = _json.loads(integration.settings_local_path().read_text(encoding="utf-8"))
    cmds = [
        h
        for block in settings["hooks"]["Stop"]
        for h in block.get("hooks", [])
        if h.get("command") == "nightly hook stop"
    ]
    assert len(cmds) == 1  # not duplicated on re-install


@pytest.mark.asyncio
async def test_install_stop_hook_preserves_existing_settings(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    import json as _json

    # Pre-existing settings.local.json with unrelated entries
    settings_path = integration.settings_local_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        _json.dumps(
            {
                "permissions": {"allow": ["Bash(git status:*)"]},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "other-hook"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    await integration.install("project")
    settings = _json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["permissions"] == {"allow": ["Bash(git status:*)"]}
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other-hook"
    assert integration.is_stop_hook_installed()


@pytest.mark.asyncio
async def test_uninstall_removes_stop_hook_only(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    import json as _json

    settings_path = integration.settings_local_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        _json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}),
        encoding="utf-8",
    )
    await integration.install("project")
    assert integration.is_stop_hook_installed()
    await integration.uninstall("project")
    # settings.local.json should still exist with permissions intact
    settings = _json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings == {"permissions": {"allow": ["Bash(ls:*)"]}}


@pytest.mark.asyncio
async def test_uninstall_removes_settings_file_when_empty(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    assert integration.settings_local_path().is_file()
    await integration.uninstall("project")
    # Nothing else in the file (hook + env both removed) → it's cleaned up.
    assert not integration.settings_local_path().exists()


@pytest.mark.asyncio
async def test_install_pins_stop_hook_block_cap_env(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    """Install raises the host's without-progress block cap via the
    session `env` object so an overnight forced-continuation chain never
    trips it (the default of 8 is far too low)."""
    import json as _json

    await integration.install("project")
    settings = _json.loads(integration.settings_local_path().read_text(encoding="utf-8"))
    assert settings["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] == "5000"


@pytest.mark.asyncio
async def test_uninstall_removes_block_cap_env(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    """Uninstall removes the env pin (matching value) — file cleaned up."""
    import json as _json

    settings_path = integration.settings_local_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        _json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}),
        encoding="utf-8",
    )
    await integration.install("project")
    await integration.uninstall("project")
    settings = _json.loads(settings_path.read_text(encoding="utf-8"))
    assert "env" not in settings
    assert settings == {"permissions": {"allow": ["Bash(ls:*)"]}}


@pytest.mark.asyncio
async def test_uninstall_preserves_operator_custom_block_cap(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    """If the operator has set their own value for the block-cap env var
    that differs from Nightly's, uninstall must not clobber it — removal
    is matching-value-only.

    This simulates the operator re-pinning a custom value *after* a
    Nightly install: uninstall should see the mismatch and leave it.
    """
    import json as _json

    from nightly_core.hook_install import merge_settings_env

    settings_path = integration.settings_local_path()
    await integration.install("project")
    # Operator overrides Nightly's pin with their own value.
    merge_settings_env(settings_path, "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "42")
    await integration.uninstall("project")
    settings = _json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] == "42"


@pytest.mark.asyncio
async def test_install_does_not_clobber_malformed_settings(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    settings_path = integration.settings_local_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    malformed = "{this is not valid JSON"
    settings_path.write_text(malformed, encoding="utf-8")
    await integration.install("project")
    # SKILL.md installed, but malformed settings file left alone.
    assert integration.is_installed("project")
    assert settings_path.read_text(encoding="utf-8") == malformed
    assert not integration.is_stop_hook_installed()


# ── Phase 9i: conclude skill ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_claude_writes_conclude_skill(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    conclude = integration.conclude_skill_path("project")
    assert conclude is not None
    assert conclude.is_file()
    assert "name: nightly-conclude" in conclude.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_uninstall_claude_removes_conclude_skill(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    await integration.uninstall("project")
    conclude = integration.conclude_skill_path("project")
    assert conclude is not None
    assert not conclude.exists()


def test_claude_keepalive_support_is_forced(integration: ClaudeHostIntegration) -> None:
    assert integration.keepalive_support == "forced"


# ── Phase 9j: /nightly-update skill ───────────────────────────────────────


@pytest.mark.asyncio
async def test_install_claude_writes_update_skill(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    upd = integration.update_skill_path("project")
    assert upd is not None
    assert upd.is_file()
    assert "name: nightly-update" in upd.read_text(encoding="utf-8")
    assert integration.is_update_installed("project")


@pytest.mark.asyncio
async def test_uninstall_claude_removes_update_skill(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    await integration.uninstall("project")
    upd = integration.update_skill_path("project")
    assert upd is not None
    assert not upd.exists()


# ── Phase 9n: /nightly-bug skill + repulsion language ─────────────────────


@pytest.mark.asyncio
async def test_install_claude_writes_bug_skill(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    bug = integration.bug_skill_path("project")
    assert bug is not None
    assert bug.is_file()
    body = bug.read_text(encoding="utf-8")
    assert "name: nightly-bug" in body
    assert integration.is_bug_installed("project")


@pytest.mark.asyncio
async def test_uninstall_claude_removes_bug_skill(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    await integration.uninstall("project")
    bug = integration.bug_skill_path("project")
    assert bug is not None
    assert not bug.exists()


# ── /nightly-init skill — global-install bootstrap ────────────────────────


@pytest.mark.asyncio
async def test_install_claude_writes_init_skill(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    init = integration.init_skill_path("project")
    assert init is not None
    assert init.is_file()
    body = init.read_text(encoding="utf-8")
    assert "name: nightly-init" in body
    assert integration.is_init_installed("project")


@pytest.mark.asyncio
async def test_install_claude_writes_init_skill_at_user_scope(
    integration: ClaudeHostIntegration,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-scope install is the primary use case for /nightly-init —
    install once globally, drop into any repo, type the command to
    bootstrap. Verify the user-scope path lands the file."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setattr(
        ClaudeHostIntegration,
        "USER_INIT_ABSOLUTE",
        tmp_path / "fake-home" / ".claude/skills/nightly-init/SKILL.md",
    )
    monkeypatch.setattr(
        ClaudeHostIntegration,
        "USER_SKILL_ABSOLUTE",
        tmp_path / "fake-home" / ".claude/skills/nightly/SKILL.md",
    )
    monkeypatch.setattr(
        ClaudeHostIntegration,
        "USER_CONCLUDE_ABSOLUTE",
        tmp_path / "fake-home" / ".claude/skills/nightly-conclude/SKILL.md",
    )
    monkeypatch.setattr(
        ClaudeHostIntegration,
        "USER_UPDATE_ABSOLUTE",
        tmp_path / "fake-home" / ".claude/skills/nightly-update/SKILL.md",
    )
    monkeypatch.setattr(
        ClaudeHostIntegration,
        "USER_BUG_ABSOLUTE",
        tmp_path / "fake-home" / ".claude/skills/nightly-bug/SKILL.md",
    )
    inst = ClaudeHostIntegration(root=project)
    await inst.install("user")
    assert inst.is_init_installed("user")
    assert inst.is_installed("user")
    # No stop hook merged at user scope.
    assert not inst.settings_local_path().exists()


@pytest.mark.asyncio
async def test_uninstall_claude_removes_init_skill(
    integration: ClaudeHostIntegration, project: Path
) -> None:
    await integration.install("project")
    await integration.uninstall("project")
    init = integration.init_skill_path("project")
    assert init is not None
    assert not init.exists()


def test_conclude_skill_repels_the_agent() -> None:
    """The CONCLUDE_SKILL_MD description and body must explicitly say
    this is HUMAN-only — the failure mode it guards against is the agent
    pattern-matching a generic 'wind down the session' description."""
    from nightly_core import CONCLUDE_SKILL_MD

    text = CONCLUDE_SKILL_MD
    # Description / frontmatter must read as human-only at a glance.
    assert "HUMAN-ONLY" in text or "human-only" in text.lower()
    assert "NEVER call this skill" in text or "never invoke" in text.lower()
    # The body must point the agent at the correct end-of-cascade flow
    # so a re-read recovers from the antipattern.
    assert "nightly ideate" in text
    assert "nightly brief" in text


def test_init_skill_content() -> None:
    """The /nightly-init skill should walk the operator through running
    `nightly init` on the current repo — the global-install bootstrap."""
    from nightly_core import INIT_SKILL_MD

    text = INIT_SKILL_MD
    assert "name: nightly-init" in text
    # Body must mention the command it shells out to.
    assert "nightly init" in text
    # Should reference the host options so the operator knows the choices.
    assert "--host" in text
    # The install.sh one-liner is the documented escape hatch when the
    # binary isn't on PATH — make sure it survives future edits.
    assert "install.sh" in text


def test_bug_skill_repels_the_agent() -> None:
    """Same guard as conclude — the bug skill description is even more
    sensitive because self-filing would mask the very bug the operator
    needs to triage."""
    from nightly_core import BUG_SKILL_MD

    text = BUG_SKILL_MD
    assert "HUMAN-ONLY" in text or "human-only" in text.lower()
    assert "NEVER call this skill" in text or "never invoke" in text.lower()
    assert "nightly bug" in text
    # Reasoning citation — keeps future edits from softening the
    # repulsion without realising why it exists.
    assert "self-filing" in text.lower() or "mask" in text.lower()
