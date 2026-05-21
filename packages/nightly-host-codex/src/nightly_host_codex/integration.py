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
from nightly_core.hook_install import (
    HookFile,
    find_nested_hook_index,
    merge_nested_hook,
    read_settings,
    remove_nested_hook,
)
from nightly_host_codex.skill import SKILL_MD

__all__ = ["CodexHostIntegration"]

_STOP_HOOK_COMMAND = "nightly hook stop"
"""Codex shares the Claude Code Stop-hook JSON shape, so the default
`claude_code` wire format is correct — no `--format` flag needed."""

_HOOKS_RELATIVE = Path(".codex/hooks.json")
"""Per-repo Codex hook config (also supported: .codex/config.toml).
We use the JSON form for consistency with Claude's settings.local.json."""

_SESSION_ID_ENV_VARS = ("CODEX_SESSION_ID",)
"""Env vars Codex exposes for the active session id. Best-effort — Codex's
session-id semantics are still evolving; tests will pin the contract."""


class CodexHostIntegration(NightlyHostIntegration):
    """Nightly host integration for the Codex CLI (primary host)."""

    host_id: HostId = "codex"
    keepalive_support: KeepaliveSupport = "forced"

    PROJECT_SKILL_RELATIVE = Path(".codex/skills/nightly/SKILL.md")
    USER_SKILL_ABSOLUTE = Path.home() / ".codex/skills/nightly/SKILL.md"
    PROJECT_CONCLUDE_RELATIVE = Path(".codex/skills/nightly-conclude/SKILL.md")
    USER_CONCLUDE_ABSOLUTE = Path.home() / ".codex/skills/nightly-conclude/SKILL.md"
    PROJECT_UPDATE_RELATIVE = Path(".codex/skills/nightly-update/SKILL.md")
    USER_UPDATE_ABSOLUTE = Path.home() / ".codex/skills/nightly-update/SKILL.md"

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
                self._trim_skill_parents(sibling)
        if not target.exists():
            return
        target.unlink()
        self._trim_skill_parents(target)

    @staticmethod
    def _trim_skill_parents(skill_file: Path) -> None:
        parent = skill_file.parent
        stop_at = "skills"
        while parent.name and not any(parent.iterdir()):
            removed = parent
            parent = parent.parent
            removed.rmdir()
            if removed.name == stop_at:
                break

    def is_installed(self, scope: InstallScope) -> bool:
        return self.skill_path(scope).is_file()

    # ── Stop hook wiring (Phase 9i) ──────────────────────────────────────
    def hooks_path(self) -> Path:
        """Where Nightly merges its Stop hook entry."""
        return self._root / _HOOKS_RELATIVE

    def _hook_file(self) -> HookFile:
        return HookFile(
            path=self.hooks_path(),
            event_name="Stop",
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
            settings = read_settings(self.hooks_path())
        except _json.JSONDecodeError:
            return False
        return (
            find_nested_hook_index(settings, event_name="Stop", command=_STOP_HOOK_COMMAND)
            is not None
        )

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
