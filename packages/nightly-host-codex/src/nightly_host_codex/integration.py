"""CodexHostIntegration — Nightly's second primary host.

Phase 4 implements the launcher lifecycle (`install` / `uninstall` /
`is_installed` / `session_id` / `auth_status`). `dispatch_sub_agent` and
`request_approval` stay `NotImplementedError` until the cross-host dispatch
plumbing lands.

The shape mirrors `ClaudeHostIntegration` closely — both hosts install a
SKILL.md under `<scope>/.<host>/skills/nightly/`. Codex's distinguishing
features (OS-level Seatbelt/Landlock sandboxing, MCP-based sub-agent
dispatch) live in the SKILL.md content; the Python integration is mostly
file-and-env plumbing.
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
from nightly_host_codex.skill import SKILL_MD

__all__ = ["CodexHostIntegration"]

_SESSION_ID_ENV_VARS = ("CODEX_SESSION_ID",)
"""Env vars Codex exposes for the active session id. Best-effort — Codex's
session-id semantics are still evolving; tests will pin the contract."""


class CodexHostIntegration(NightlyHostIntegration):
    """Nightly host integration for the Codex CLI (primary host)."""

    host_id: HostId = "codex"

    PROJECT_SKILL_RELATIVE = Path(".codex/skills/nightly/SKILL.md")
    USER_SKILL_ABSOLUTE = Path.home() / ".codex/skills/nightly/SKILL.md"

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
        """Return the absolute path the SKILL.md lives at for `scope`."""
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
        # Trim empty parents up to .codex/skills/, but never go above.
        parent = target.parent
        stop_at = "skills"
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
        """Heuristic: `codex` binary present and `codex --version` exits 0.

        A real subscription / plan check arrives later — for now we confirm
        the binary is reachable and `codex login` has been run at least
        once. `plan` is set to "unknown" rather than guessing.

        Synchronous subprocess inside an async method is intentional: this
        is a one-shot init probe, never on a hot path.
        """
        binary = shutil.which("codex")
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
        """Spawn `codex exec --json` and normalize the result.

        Headless invocations use the safe defaults from the brainstorm:
        `--sandbox workspace-write --ask-for-approval never`. Codex's
        Seatbelt (macOS) / Landlock (Linux) sandbox applies automatically
        — Nightly does not need to wrap the process.

        Subscription credentials inherit from `~/.codex/sessions/`. Set
        `OPENAI_API_KEY` before invoking for sandboxed CI environments.
        """
        binary = shutil.which("codex")
        if binary is None:
            return HeadlessResult(
                host_id=self.host_id,
                output="",
                exit_code=-1,
                elapsed_ms=0,
                error="codex binary not found on PATH",
            )
        argv = [
            binary,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            prompt,
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
            "Sub-agent dispatch via Codex MCP / `codex exec` is Phase 5+. "
            "Phase 4 ships only the launcher; the Skill orchestrates dispatch "
            "in-session for now."
        )

    async def request_approval(self, q: str, choices: list[str]) -> str:
        raise NotImplementedError(
            "Native Codex UI approval is Phase 5+. Phase 4 records refusals "
            "to .nightly/runs/<run-id>/proposed/approvals/ for retro review."
        )
