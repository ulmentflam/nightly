"""AntigravityHostIntegration — Nightly's second secondary host.

Phase 6 implements the launcher lifecycle. The Skill installs to
`.gemini/antigravity/agents/nightly/SKILL.md` per Antigravity's per-host
convention (Google's Gemini family puts agent and skill config under
`~/.gemini/antigravity/`).

The auth heuristic is unusual: Antigravity doesn't have a single CLI
binary to probe (the desktop IDE owns most of the auth surface). We use
*directory presence* of `~/.gemini/antigravity/` as the signal — if the
user has ever opened Antigravity and gone through OAuth, that directory
exists. Empty / absent = "unknown / not authenticated."

`dispatch_sub_agent` (Agent Manager registration over Antigravity's API)
and `request_approval` (native UI prompts) arrive in Phase 7+, alongside
`brain/<GUID>/` mirroring.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from nightly_core import (
    CONCLUDE_SKILL_MD,
    UPDATE_SKILL_MD,
    AuthStatus,
    HostId,
    InstallScope,
    KeepaliveSupport,
    NightlyHostIntegration,
    SpecialistRole,
    SubAgentResult,
    repo_root,
)
from nightly_core.hook_install import (
    HookFile,
    find_nested_hook_index,
    merge_nested_hook,
    read_settings,
    remove_nested_hook,
)
from nightly_host_antigravity.skill import SKILL_MD

__all__ = ["AntigravityHostIntegration"]

# Antigravity exposes session ids inconsistently — try both common names.
_SESSION_ID_ENV_VARS = ("ANTIGRAVITY_SESSION_ID", "GEMINI_SESSION_ID")

# User-scope home for Antigravity. Used both as the user-scope install
# parent AND as the auth-status presence probe.
_ANTIGRAVITY_HOME = Path.home() / ".gemini" / "antigravity"

# Antigravity is built on Gemini CLI, which exposes an `AfterAgent`
# lifecycle hook (Stop-hook equivalent) configured in `.gemini/settings.json`.
# `decision: "deny"` with a `reason` triggers a retry with the reason text
# fed back as the next user prompt — the same semantics as Claude Code's
# `{"decision":"block","reason":"..."}` shape, just renamed.
# https://geminicli.com/docs/hooks/ and the official docs at
# github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md
_STOP_HOOK_COMMAND = "nightly hook stop --format gemini_cli"
_GEMINI_SETTINGS_RELATIVE = Path(".gemini/settings.json")


class AntigravityHostIntegration(NightlyHostIntegration):
    """Nightly host integration for Google Antigravity (secondary host)."""

    host_id: HostId = "antigravity"
    keepalive_support: KeepaliveSupport = "forced"

    PROJECT_SKILL_RELATIVE = Path(".gemini/antigravity/agents/nightly/SKILL.md")
    USER_SKILL_ABSOLUTE = _ANTIGRAVITY_HOME / "agents" / "nightly" / "SKILL.md"
    PROJECT_CONCLUDE_RELATIVE = Path(".gemini/antigravity/agents/nightly-conclude/SKILL.md")
    USER_CONCLUDE_ABSOLUTE = _ANTIGRAVITY_HOME / "agents" / "nightly-conclude" / "SKILL.md"
    PROJECT_UPDATE_RELATIVE = Path(".gemini/antigravity/agents/nightly-update/SKILL.md")
    USER_UPDATE_ABSOLUTE = _ANTIGRAVITY_HOME / "agents" / "nightly-update" / "SKILL.md"

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or repo_root()).resolve()

    @property
    def root(self) -> Path:
        return self._root

    # ── launcher lifecycle ────────────────────────────────────────────────
    def skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_SKILL_RELATIVE
        return self.USER_SKILL_ABSOLUTE

    def conclude_skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_CONCLUDE_RELATIVE
        return self.USER_CONCLUDE_ABSOLUTE

    def update_skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_UPDATE_RELATIVE
        return self.USER_UPDATE_ABSOLUTE

    async def install(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SKILL_MD, encoding="utf-8")
        for sibling_path, sibling_md in (
            (self.conclude_skill_path(scope), CONCLUDE_SKILL_MD),
            (self.update_skill_path(scope), UPDATE_SKILL_MD),
        ):
            sibling_path.parent.mkdir(parents=True, exist_ok=True)
            sibling_path.write_text(sibling_md, encoding="utf-8")
        self.install_keepalive_hook(scope)

    async def uninstall(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        self.uninstall_keepalive_hook(scope)
        for sibling in (self.conclude_skill_path(scope), self.update_skill_path(scope)):
            if sibling.exists():
                sibling.unlink()
                self._trim_agents_parents(sibling)
        if not target.exists():
            return
        target.unlink()
        self._trim_agents_parents(target)

    @staticmethod
    def _trim_agents_parents(skill_file: Path) -> None:
        """Trim empty parents up to `agents/`. Never touch antigravity/ —
        that's user home and may contain unrelated state (brain/, settings)."""
        parent = skill_file.parent
        stop_at = "agents"
        while parent.name and not any(parent.iterdir()):
            removed = parent
            parent = parent.parent
            removed.rmdir()
            if removed.name == stop_at:
                break

    def is_installed(self, scope: InstallScope) -> bool:
        return self.skill_path(scope).is_file()

    # ── Stop hook wiring (Phase 9i — Gemini CLI AfterAgent) ──────────────
    def settings_path(self) -> Path:
        """Where Nightly merges its AfterAgent hook entry."""
        return self._root / _GEMINI_SETTINGS_RELATIVE

    def _hook_file(self) -> HookFile:
        return HookFile(
            path=self.settings_path(),
            event_name="AfterAgent",
            command=_STOP_HOOK_COMMAND,
        )

    def install_keepalive_hook(self, scope: InstallScope) -> None:
        if scope != "project":
            return
        merge_nested_hook(self._hook_file())

    def uninstall_keepalive_hook(self, scope: InstallScope) -> None:
        if scope != "project":
            return
        remove_nested_hook(self._hook_file())

    def is_keepalive_hook_installed(self, scope: InstallScope = "project") -> bool:
        if scope != "project":
            return False
        import json as _json  # noqa: PLC0415 — narrow scope for the JSON error catch

        try:
            settings = read_settings(self.settings_path())
        except _json.JSONDecodeError:
            return False
        return (
            find_nested_hook_index(settings, event_name="AfterAgent", command=_STOP_HOOK_COMMAND)
            is not None
        )

    # ── session identity ──────────────────────────────────────────────────
    def session_id(self) -> str:
        for var in _SESSION_ID_ENV_VARS:
            value = os.environ.get(var)
            if value:
                return value
        return f"detached-{uuid.uuid4()}"

    # ── auth_status (heuristic for Phase 6) ──────────────────────────────
    async def auth_status(self) -> AuthStatus:
        """Heuristic: `~/.gemini/antigravity/` exists.

        Antigravity has no canonical CLI binary to probe — auth is owned
        by the desktop IDE going through Google OAuth. The compromise:
        treat directory presence as evidence that the user has launched
        Antigravity at least once. False positives are possible (a stale
        empty directory) but real-world false negatives only happen
        before the user has ever opened the app, which is a useful signal.
        """
        if not _ANTIGRAVITY_HOME.is_dir():
            return AuthStatus(ok=False)
        return AuthStatus(ok=True, plan="unknown")

    # ── runtime primitives — Phase 7+ ────────────────────────────────────
    async def dispatch_sub_agent(
        self,
        *,
        role: SpecialistRole,
        prompt: str,
        cwd: str,
        allowed_tools: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> SubAgentResult:
        raise NotImplementedError(
            "Sub-agent dispatch via Antigravity's Agent Manager API is "
            "Phase 7+. Phase 6 ships only the managed-agent launcher; "
            "the Skill orchestrates dispatch in-session for now. The "
            "Phase 7 implementation will also mirror task artifacts into "
            "~/.gemini/antigravity/brain/<GUID>/ for the Agent Manager UI."
        )

    async def request_approval(self, q: str, choices: list[str]) -> str:
        raise NotImplementedError(
            "Native Antigravity UI approval is Phase 7+. Phase 6 records "
            "refusals to .nightly/runs/<run-id>/proposed/approvals/ for "
            "retro review."
        )
