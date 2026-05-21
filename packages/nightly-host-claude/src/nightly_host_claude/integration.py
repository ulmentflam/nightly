"""ClaudeHostIntegration — Nightly's primary host.

Phase 1 implements:
- `install` / `uninstall` / `is_installed` — write & remove the SKILL.md
- `session_id` — pull from Claude Code's env vars, or generate detached id
- `auth_status` — heuristic `claude --version` probe

Sub-agent dispatch via the Task tool and native UI approval prompts arrive in
Phase 2 and raise `NotImplementedError` until then.
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
from nightly_host_claude.skill import SKILL_MD

__all__ = ["ClaudeHostIntegration"]

_SESSION_ID_ENV_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
"""Env vars Claude Code is known to expose for the active session id."""


class ClaudeHostIntegration(NightlyHostIntegration):
    """Nightly host integration for Claude Code (primary host)."""

    host_id: HostId = "claude"

    PROJECT_SKILL_RELATIVE = Path(".claude/skills/nightly/SKILL.md")
    USER_SKILL_ABSOLUTE = Path.home() / ".claude/skills/nightly/SKILL.md"

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
        # Trim empty parents up to .claude/skills/, but never go above that.
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

    # ── auth status (heuristic for Phase 1) ──────────────────────────────
    async def auth_status(self) -> AuthStatus:
        """Heuristic: claude CLI present and `claude --version` exits 0.

        A real subscription / plan check lands in Phase 2 — for now we only
        confirm the binary is reachable and `claude login` was run at least
        once. `plan` is set to "unknown" rather than guessing.

        Note: this is a one-shot startup check, not on a hot path, so calling
        `subprocess.run` synchronously inside the async method is intentional.
        """
        binary = shutil.which("claude")
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
        """Spawn `claude -p --output-format json` and normalize the result.

        Subscription credentials inherit through the environment — the
        spawned `claude` binary reads `~/.claude/` for cached creds. Set
        `ANTHROPIC_API_KEY` before invoking for sandboxed CI environments.
        """
        binary = shutil.which("claude")
        if binary is None:
            return HeadlessResult(
                host_id=self.host_id,
                output="",
                exit_code=-1,
                elapsed_ms=0,
                error="claude binary not found on PATH",
            )
        # `--permission-mode acceptEdits` is the autonomy contract in argv
        # form: it silences Claude Code's edit-approval prompts so Nightly
        # never blocks on "may I edit foo.py?" dialogs. Combined with the
        # AGENTS.md / CLAUDE.md rules block (no `AskUserQuestion`), this
        # closes both the model-side and host-side prompt surfaces.
        argv = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--session-id",
            self.session_id(),
        ]
        return await run_subprocess(
            host_id=self.host_id,
            argv=argv,
            cwd=cwd,
            stdin=None,  # prompt passed as CLI arg, not stdin
            timeout_s=timeout_s,
            runner=self._subprocess_runner,
        )

    # ── runtime primitives — Phase 2+ ────────────────────────────────────
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
            "Sub-agent dispatch via the Claude Code Task tool is Phase 2. "
            "Phase 1 executes the task inline in the active Claude Code session."
        )

    async def request_approval(self, q: str, choices: list[str]) -> str:
        raise NotImplementedError(
            "Native Claude Code UI approval is Phase 2. Phase 1 records "
            "refusals to .nightly/runs/<run-id>/proposed/approvals/ for "
            "retro review per the always-advance principle."
        )
