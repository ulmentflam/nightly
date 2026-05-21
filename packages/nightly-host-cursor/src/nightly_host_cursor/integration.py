"""CursorHostIntegration — Nightly's first secondary host.

Phase 6 implements the launcher lifecycle. Cursor installs differ from
the primary hosts in two ways:

1. **Flat file, not a folder.** Cursor's slash commands live at
   `.cursor/commands/<name>.md` (one markdown file per command), not a
   `<name>/SKILL.md` folder. Uninstall is therefore a plain `unlink()` —
   `.cursor/commands/` is shared with other commands and must not be
   touched.
2. **Auth heuristic uses `cursor-agent`.** The Cursor CLI binary is
   `cursor-agent`; `cursor` itself is the desktop app.

`dispatch_sub_agent` (Background Agent dispatch over Cursor's REST API)
and `request_approval` (native UI prompts) arrive in Phase 7+.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from nightly_core import (
    AuthStatus,
    HostId,
    InstallScope,
    NightlyHostIntegration,
    SpecialistRole,
    SubAgentResult,
    repo_root,
)
from nightly_host_cursor.skill import SKILL_MD

__all__ = ["CursorHostIntegration"]

_SESSION_ID_ENV_VARS = ("CURSOR_SESSION_ID",)
"""Env vars Cursor exposes for the active session id. Best-effort."""


class CursorHostIntegration(NightlyHostIntegration):
    """Nightly host integration for Cursor (secondary host)."""

    host_id: HostId = "cursor"

    # Cursor commands are a single markdown file per command — no folder
    # named after the command — so the path ends in `.md`, not `/SKILL.md`.
    PROJECT_SKILL_RELATIVE = Path(".cursor/commands/nightly.md")
    USER_SKILL_ABSOLUTE = Path.home() / ".cursor/commands/nightly.md"

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or repo_root()).resolve()

    @property
    def root(self) -> Path:
        return self._root

    # ── launcher lifecycle ────────────────────────────────────────────────
    def skill_path(self, scope: InstallScope) -> Path:
        """Return the absolute path the command file lives at for `scope`."""
        if scope == "project":
            return self._root / self.PROJECT_SKILL_RELATIVE
        return self.USER_SKILL_ABSOLUTE

    async def install(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SKILL_MD, encoding="utf-8")

    async def uninstall(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        if not target.exists():
            return
        target.unlink()
        # Intentionally no parent cleanup — `.cursor/commands/` is shared
        # with other commands the user may have installed; leave it alone.

    def is_installed(self, scope: InstallScope) -> bool:
        return self.skill_path(scope).is_file()

    # ── session identity ──────────────────────────────────────────────────
    def session_id(self) -> str:
        for var in _SESSION_ID_ENV_VARS:
            value = os.environ.get(var)
            if value:
                return value
        return f"detached-{uuid.uuid4()}"

    # ── auth_status (heuristic for Phase 6) ──────────────────────────────
    async def auth_status(self) -> AuthStatus:
        """Heuristic: `cursor-agent --version` exits 0.

        The desktop app's CLI binary is `cursor-agent`; `cursor` alone is
        an alias on some installs and a different tool on others. We probe
        `cursor-agent` first because it's the canonical name for the agent
        CLI. Synchronous subprocess inside an async method is intentional
        — this is a one-shot init probe, not a hot path.
        """
        binary = shutil.which("cursor-agent")
        if binary is None:
            return AuthStatus(ok=False)
        try:
            subprocess.run(  # noqa: ASYNC221  - one-shot init probe
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
            "Sub-agent dispatch via Cursor Background Agents (REST API) "
            "is Phase 7+. Phase 6 ships only the slash command launcher; "
            "the Skill orchestrates dispatch in-session for now."
        )

    async def request_approval(self, q: str, choices: list[str]) -> str:
        raise NotImplementedError(
            "Native Cursor UI approval is Phase 7+. Phase 6 records refusals "
            "to .nightly/runs/<run-id>/proposed/approvals/ for retro review."
        )
