"""OpencodeHostIntegration — Nightly's third primary host.

Phase 4 implements the launcher lifecycle. The Skill installs to
`.opencode/agents/nightly/SKILL.md` per the brainstorm.

opencode has no equivalent of Codex's Seatbelt/Landlock today, so
`auth_status` only confirms the CLI is reachable; the broader sandbox
story for opencode arrives with the outer-container support in Phase 7.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from nightly_core import (
    AuthStatus,
    HeadlessResult,
    HostId,
    InstallScope,
    NightlyHostIntegration,
    SpecialistRole,
    SubAgentResult,
    SubprocessRunner,
    repo_root,
    run_subprocess,
)
from nightly_host_opencode.skill import SKILL_MD

__all__ = ["OpencodeHostIntegration"]

_SESSION_ID_ENV_VARS = ("OPENCODE_SESSION_ID",)


class OpencodeHostIntegration(NightlyHostIntegration):
    """Nightly host integration for opencode (primary host)."""

    host_id: HostId = "opencode"

    PROJECT_SKILL_RELATIVE = Path(".opencode/agents/nightly/SKILL.md")
    USER_SKILL_ABSOLUTE = Path.home() / ".opencode/agents/nightly/SKILL.md"

    def __init__(
        self,
        root: Path | None = None,
        *,
        subprocess_runner: SubprocessRunner | None = None,
    ) -> None:
        self._root = (root or repo_root()).resolve()
        self._subprocess_runner = subprocess_runner

    @property
    def root(self) -> Path:
        return self._root

    # ── launcher lifecycle ────────────────────────────────────────────────
    def skill_path(self, scope: InstallScope) -> Path:
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
        # Trim empty parents up to .opencode/agents/, but never go above.
        parent = target.parent
        stop_at = "agents"
        while parent.name in {"nightly", stop_at} and not any(parent.iterdir()):
            removed = parent
            parent = parent.parent
            removed.rmdir()
            if removed.name == stop_at:
                break

    def is_installed(self, scope: InstallScope) -> bool:
        return self.skill_path(scope).is_file()

    # ── session identity ──────────────────────────────────────────────────
    def session_id(self) -> str:
        for var in _SESSION_ID_ENV_VARS:
            value = os.environ.get(var)
            if value:
                return value
        return f"detached-{uuid.uuid4()}"

    # ── auth_status (heuristic for Phase 4) ──────────────────────────────
    async def auth_status(self) -> AuthStatus:
        """Heuristic: `opencode` binary present and `opencode --version` exits 0.

        opencode manages provider credentials in `~/.local/share/opencode/auth.json`
        — a richer check arrives in Phase 5+.
        """
        binary = shutil.which("opencode")
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

    # ── headless mode — Phase 7 ──────────────────────────────────────────
    async def run_headless(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        timeout_s: float | None = None,
    ) -> HeadlessResult:
        """Spawn `opencode run --format json` and normalize the result.

        Subscription credentials inherit from opencode's per-provider
        `auth.json` (typically `~/.local/share/opencode/auth.json`). Set
        the provider's API key env var (e.g. `OPENAI_API_KEY`) before
        invoking for sandboxed CI environments.
        """
        binary = shutil.which("opencode")
        if binary is None:
            return HeadlessResult(
                host_id=self.host_id,
                output="",
                exit_code=-1,
                elapsed_ms=0,
                error="opencode binary not found on PATH",
            )
        argv = [
            binary,
            "run",
            prompt,
            "--format",
            "json",
        ]
        return await run_subprocess(
            host_id=self.host_id,
            argv=argv,
            cwd=cwd,
            stdin=None,
            timeout_s=timeout_s,
            runner=self._subprocess_runner,
        )

    # ── runtime primitives — Phase 5+ ────────────────────────────────────
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
            "Sub-agent dispatch via opencode session forking (POST /session/:id/fork) "
            "is Phase 5+. Phase 4 ships only the launcher; the Skill orchestrates "
            "dispatch in-session for now."
        )

    async def request_approval(self, q: str, choices: list[str]) -> str:
        raise NotImplementedError(
            "Native opencode UI approval is Phase 5+. Phase 4 records refusals "
            "to .nightly/runs/<run-id>/proposed/approvals/ for retro review."
        )
