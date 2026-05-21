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
    CONCLUDE_SKILL_MD,
    UPDATE_SKILL_MD,
    AuthStatus,
    HeadlessResult,
    HostId,
    InstallScope,
    KeepaliveSupport,
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
    """Nightly host integration for opencode (primary host).

    Keep-alive level: `soft` — opencode's plugin system has reactive
    lifecycle events (`session.idle`, `session.updated`, tool hooks) but
    no force-continue mechanism. The keep-alive contract is honored
    purely through the AGENTS.md / CLAUDE.md NEVER STOP rule (the model
    is told to never stop). The disk-based off-ramps (`nightly stop`,
    `nightly conclude`) still work because they're host-portable.
    Reference: https://opencode.ai/docs/plugins/
    """

    host_id: HostId = "opencode"
    keepalive_support: KeepaliveSupport = "soft"

    PROJECT_SKILL_RELATIVE = Path(".opencode/agents/nightly/SKILL.md")
    USER_SKILL_ABSOLUTE = Path.home() / ".opencode/agents/nightly/SKILL.md"
    PROJECT_CONCLUDE_RELATIVE = Path(".opencode/agents/nightly-conclude/SKILL.md")
    USER_CONCLUDE_ABSOLUTE = Path.home() / ".opencode/agents/nightly-conclude/SKILL.md"
    PROJECT_UPDATE_RELATIVE = Path(".opencode/agents/nightly-update/SKILL.md")
    USER_UPDATE_ABSOLUTE = Path.home() / ".opencode/agents/nightly-update/SKILL.md"

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

    async def uninstall(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        for sibling in (self.conclude_skill_path(scope), self.update_skill_path(scope)):
            if sibling.exists():
                sibling.unlink()
                self._trim_agent_parents(sibling)
        if not target.exists():
            return
        target.unlink()
        self._trim_agent_parents(target)

    @staticmethod
    def _trim_agent_parents(skill_file: Path) -> None:
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
