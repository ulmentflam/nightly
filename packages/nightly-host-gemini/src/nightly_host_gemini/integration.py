"""GeminiHostIntegration — Nightly's vanilla Gemini CLI host.

Distinct from `nightly-host-antigravity`: both write under `.gemini/`,
but Antigravity targets the desktop IDE's managed-agent surface
(`.gemini/antigravity/agents/`), while this host targets the upstream
Gemini CLI's custom-command surface (`.gemini/commands/`). The two
share the same `.gemini/settings.json` hook surface (`AfterAgent`).

Gemini CLI custom commands are TOML, not markdown — see
`nightly_host_gemini.skill.md_to_gemini_toml` for the conversion.

Sub-agent dispatch via headless `gemini --prompt ...` and `request_approval`
land later (Phase 7+).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from nightly_core import (
    BUG_SKILL_MD,
    CONCLUDE_SKILL_MD,
    INIT_SKILL_MD,
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
from nightly_host_gemini.skill import SKILL_MD, md_to_gemini_toml

__all__ = ["GeminiHostIntegration"]

# Gemini CLI session id env var. Best-effort — the upstream CLI doesn't
# expose this consistently yet; we accept either name.
_SESSION_ID_ENV_VARS = ("GEMINI_SESSION_ID", "GEMINI_CLI_SESSION_ID")

# Shared with Antigravity — both surfaces register `AfterAgent` against
# the same settings file. `merge_nested_hook` is idempotent so co-install
# is safe.
_STOP_HOOK_COMMAND = "nightly hook stop --format gemini_cli"
_GEMINI_SETTINGS_RELATIVE = Path(".gemini/settings.json")

_GEMINI_HOME = Path.home() / ".gemini"


class GeminiHostIntegration(NightlyHostIntegration):
    """Nightly host integration for vanilla Google Gemini CLI."""

    host_id: HostId = "gemini"
    keepalive_support: KeepaliveSupport = "forced"

    # Gemini CLI custom commands are single TOML files per command, not
    # folders. Same shape as Cursor.
    PROJECT_SKILL_RELATIVE = Path(".gemini/commands/nightly.toml")
    USER_SKILL_ABSOLUTE = _GEMINI_HOME / "commands" / "nightly.toml"
    PROJECT_CONCLUDE_RELATIVE = Path(".gemini/commands/nightly-conclude.toml")
    USER_CONCLUDE_ABSOLUTE = _GEMINI_HOME / "commands" / "nightly-conclude.toml"
    PROJECT_UPDATE_RELATIVE = Path(".gemini/commands/nightly-update.toml")
    USER_UPDATE_ABSOLUTE = _GEMINI_HOME / "commands" / "nightly-update.toml"
    PROJECT_BUG_RELATIVE = Path(".gemini/commands/nightly-bug.toml")
    USER_BUG_ABSOLUTE = _GEMINI_HOME / "commands" / "nightly-bug.toml"
    PROJECT_INIT_RELATIVE = Path(".gemini/commands/nightly-init.toml")
    USER_INIT_ABSOLUTE = _GEMINI_HOME / "commands" / "nightly-init.toml"

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

    def bug_skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_BUG_RELATIVE
        return self.USER_BUG_ABSOLUTE

    def init_skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_INIT_RELATIVE
        return self.USER_INIT_ABSOLUTE

    async def install(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md_to_gemini_toml(SKILL_MD), encoding="utf-8")
        for sibling_path, sibling_md in (
            (self.conclude_skill_path(scope), CONCLUDE_SKILL_MD),
            (self.update_skill_path(scope), UPDATE_SKILL_MD),
            (self.bug_skill_path(scope), BUG_SKILL_MD),
            (self.init_skill_path(scope), INIT_SKILL_MD),
        ):
            sibling_path.parent.mkdir(parents=True, exist_ok=True)
            sibling_path.write_text(md_to_gemini_toml(sibling_md), encoding="utf-8")
        self.install_keepalive_hook(scope)

    async def uninstall(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        self.uninstall_keepalive_hook(scope)
        for sibling in (
            self.conclude_skill_path(scope),
            self.update_skill_path(scope),
            self.bug_skill_path(scope),
            self.init_skill_path(scope),
        ):
            if sibling.exists():
                sibling.unlink()
        if target.exists():
            target.unlink()
        # No parent cleanup: `.gemini/commands/` is shared with other
        # custom commands the user may have installed (same convention
        # as Cursor's `.cursor/commands/`).

    def is_installed(self, scope: InstallScope) -> bool:
        return self.skill_path(scope).is_file()

    # ── Stop hook wiring (Gemini CLI AfterAgent) ─────────────────────────
    def settings_path(self) -> Path:
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

    # ── auth_status (heuristic) ──────────────────────────────────────────
    async def auth_status(self) -> AuthStatus:
        """Heuristic: `gemini` binary present and `gemini --version` exits 0.

        Gemini CLI manages OAuth state under `~/.gemini/`; a richer probe
        can land later. Synchronous subprocess inside an async method is
        intentional — this is a one-shot init probe, never on a hot path.
        """
        binary = shutil.which("gemini")
        if binary is None:
            return AuthStatus(ok=False)
        try:
            subprocess.run(  # noqa: ASYNC221 - one-shot init probe
                [binary, "--version"],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
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
            "Sub-agent dispatch via headless `gemini --prompt` is Phase 7+. "
            "Phase 6 ships only the custom-command launcher; the skill "
            "orchestrates dispatch in-session for now."
        )

    async def request_approval(self, q: str, choices: list[str]) -> str:
        raise NotImplementedError(
            "Native Gemini CLI UI approval is Phase 7+. Phase 6 records "
            "refusals to .nightly/runs/<run-id>/proposed/approvals/ for "
            "retro review."
        )
