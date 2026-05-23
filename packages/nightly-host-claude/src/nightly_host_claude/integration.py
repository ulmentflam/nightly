"""ClaudeHostIntegration — Nightly's primary host.

Phase 1 implements:
- `install` / `uninstall` / `is_installed` — write & remove the SKILL.md
- `session_id` — pull from Claude Code's env vars, or generate detached id
- `auth_status` — heuristic `claude --version` probe

Sub-agent dispatch via the Task tool and native UI approval prompts arrive in
Phase 2 and raise `NotImplementedError` until then.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from nightly_core import (
    BUG_SKILL_MD,
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
from nightly_host_claude.skill import SKILL_MD

__all__ = ["ClaudeHostIntegration"]

_SESSION_ID_ENV_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
"""Env vars Claude Code is known to expose for the active session id."""

_STOP_HOOK_COMMAND = "nightly hook stop"
"""Stable command used to identify Nightly's Stop hook in settings.local.json.

Claude Code shares the same JSON payload shape as Codex CLI, so neither
host needs a `--format` flag — the default `claude_code` format applies."""

_SETTINGS_LOCAL_RELATIVE = Path(".claude/settings.local.json")
"""Per-user (gitignored) Claude Code settings file the Stop hook is merged into."""


class ClaudeHostIntegration(NightlyHostIntegration):
    """Nightly host integration for Claude Code (primary host)."""

    host_id: HostId = "claude"
    keepalive_support: KeepaliveSupport = "forced"

    PROJECT_SKILL_RELATIVE = Path(".claude/skills/nightly/SKILL.md")
    USER_SKILL_ABSOLUTE = Path.home() / ".claude/skills/nightly/SKILL.md"
    PROJECT_CONCLUDE_RELATIVE = Path(".claude/skills/nightly-conclude/SKILL.md")
    USER_CONCLUDE_ABSOLUTE = Path.home() / ".claude/skills/nightly-conclude/SKILL.md"
    PROJECT_UPDATE_RELATIVE = Path(".claude/skills/nightly-update/SKILL.md")
    USER_UPDATE_ABSOLUTE = Path.home() / ".claude/skills/nightly-update/SKILL.md"
    PROJECT_BUG_RELATIVE = Path(".claude/skills/nightly-bug/SKILL.md")
    USER_BUG_ABSOLUTE = Path.home() / ".claude/skills/nightly-bug/SKILL.md"

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
        """Path to the `/nightly-conclude` SKILL.md for `scope`."""
        if scope == "project":
            return self._root / self.PROJECT_CONCLUDE_RELATIVE
        return self.USER_CONCLUDE_ABSOLUTE

    def update_skill_path(self, scope: InstallScope) -> Path:
        """Path to the `/nightly-update` SKILL.md for `scope`."""
        if scope == "project":
            return self._root / self.PROJECT_UPDATE_RELATIVE
        return self.USER_UPDATE_ABSOLUTE

    def bug_skill_path(self, scope: InstallScope) -> Path:
        """Path to the `/nightly-bug` SKILL.md for `scope`."""
        if scope == "project":
            return self._root / self.PROJECT_BUG_RELATIVE
        return self.USER_BUG_ABSOLUTE

    async def install(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SKILL_MD, encoding="utf-8")
        # Companion skills (`/nightly-conclude`, `/nightly-update`,
        # `/nightly-bug`) live alongside the main one. Each gets its
        # own folder under skills/.
        for sibling_path, sibling_md in (
            (self.conclude_skill_path(scope), CONCLUDE_SKILL_MD),
            (self.update_skill_path(scope), UPDATE_SKILL_MD),
            (self.bug_skill_path(scope), BUG_SKILL_MD),
        ):
            if sibling_path is not None:
                sibling_path.parent.mkdir(parents=True, exist_ok=True)
                sibling_path.write_text(sibling_md, encoding="utf-8")
        # The Stop hook is per-repo because it references the local
        # `.nightly/` state. User-scope installs share `~/.claude/skills/`
        # across repos and shouldn't drop a per-repo hook there — the
        # contract's `install_keepalive_hook` is a no-op for `user`.
        self.install_keepalive_hook(scope)

    async def uninstall(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        self.uninstall_keepalive_hook(scope)
        # Remove every companion skill first (same parent cleanup applies).
        for sibling in (
            self.conclude_skill_path(scope),
            self.update_skill_path(scope),
            self.bug_skill_path(scope),
        ):
            if sibling is not None and sibling.exists():
                sibling.unlink()
                self._trim_skill_parents(sibling)
        if not target.exists():
            return
        target.unlink()
        self._trim_skill_parents(target)

    @staticmethod
    def _trim_skill_parents(skill_file: Path) -> None:
        """Walk up `<skills>/<name>/` removing empty parents, stopping at `skills/`."""
        parent = skill_file.parent
        stop_at = "skills"
        while parent.name and not any(parent.iterdir()):
            removed = parent
            parent = parent.parent
            removed.rmdir()
            if removed.name == stop_at:
                break

    # ── Stop hook wiring (Phase 9h) ──────────────────────────────────────
    def settings_local_path(self) -> Path:
        """Where Nightly merges its Stop hook entry."""
        return self._root / _SETTINGS_LOCAL_RELATIVE

    def is_keepalive_hook_installed(self, scope: InstallScope = "project") -> bool:
        """True iff `.claude/settings.local.json` contains Nightly's Stop hook.

        A malformed settings file is treated as "not installed" rather
        than raising — callers (tests, status output) shouldn't have to
        defensively catch JSONDecodeError just to introspect the install
        state.
        """
        if scope != "project":
            return False
        try:
            settings = read_settings(self.settings_local_path())
        except json.JSONDecodeError:
            return False
        return (
            find_nested_hook_index(settings, event_name="Stop", command=_STOP_HOOK_COMMAND)
            is not None
        )

    # Back-compat alias kept for the older test names; identical behavior.
    def is_stop_hook_installed(self) -> bool:
        return self.is_keepalive_hook_installed("project")

    def _hook_file(self) -> HookFile:
        return HookFile(
            path=self.settings_local_path(),
            event_name="Stop",
            command=_STOP_HOOK_COMMAND,
        )

    def install_keepalive_hook(self, scope: InstallScope) -> None:
        """Merge Nightly's Stop hook entry into `.claude/settings.local.json`.

        Idempotent and JSON-safe — see `hook_install.merge_nested_hook`.
        Only `project` scope writes a hook; `user` scope shares
        `~/.claude/` across repos and shouldn't pin one repo's
        `.nightly/` state into the user-global config.
        """
        if scope != "project":
            return
        merge_nested_hook(self._hook_file())

    def uninstall_keepalive_hook(self, scope: InstallScope) -> None:
        """Remove Nightly's Stop hook entry; clean up now-empty containers."""
        if scope != "project":
            return
        remove_nested_hook(self._hook_file())

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
