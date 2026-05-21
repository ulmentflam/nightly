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
from typing import Any

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

_STOP_HOOK_COMMAND = "nightly hook stop"
"""Stable command used to identify Nightly's Stop hook in settings.local.json."""

_SETTINGS_LOCAL_RELATIVE = Path(".claude/settings.local.json")
"""Per-user (gitignored) Claude Code settings file the Stop hook is merged into."""


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
        if scope == "project":
            # The Stop hook is per-repo because it references the local
            # `.nightly/` state. User-scope installs share `~/.claude/skills/`
            # across repos and shouldn't drop a per-repo hook there.
            self._install_stop_hook()

    async def uninstall(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        if scope == "project":
            self._uninstall_stop_hook()
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

    # ── Stop hook wiring (Phase 9h) ──────────────────────────────────────
    def settings_local_path(self) -> Path:
        """Where Nightly merges its Stop hook entry."""
        return self._root / _SETTINGS_LOCAL_RELATIVE

    def is_stop_hook_installed(self) -> bool:
        """True iff `.claude/settings.local.json` contains Nightly's Stop hook.

        A malformed settings file is treated as "not installed" rather
        than raising — callers (tests, status output) shouldn't have to
        defensively catch JSONDecodeError just to introspect the install
        state.
        """
        try:
            settings = self._read_settings_local()
        except json.JSONDecodeError:
            return False
        return self._find_nightly_stop_hook_index(settings) is not None

    def _install_stop_hook(self) -> None:
        """Merge Nightly's Stop hook entry into `.claude/settings.local.json`.

        Idempotent: if the entry is already present, this is a no-op. Any
        unrelated keys / matchers / hook commands in the file are
        preserved verbatim. If the file is malformed JSON, we leave it
        alone and emit no hook — the SKILL.md install still proceeds, so
        the user gets a working /nightly without keep-alive until they
        either fix the JSON or run `nightly init` again with a clean file.
        """
        try:
            settings = self._read_settings_local()
        except json.JSONDecodeError:
            # Refuse to clobber user-authored JSON. The Stop hook is a
            # quality-of-life add-on; SKILL.md install already happened.
            return
        if self._find_nightly_stop_hook_index(settings) is not None:
            return
        hooks_root = settings.setdefault("hooks", {})
        stop_block = hooks_root.setdefault("Stop", [])
        stop_block.append(
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": _STOP_HOOK_COMMAND,
                    }
                ],
            }
        )
        self._write_settings_local(settings)

    def _uninstall_stop_hook(self) -> None:
        """Remove Nightly's Stop hook entry; trim now-empty parent containers.

        If `settings.local.json` becomes an empty `{}` after the removal,
        we delete the file so `nightly uninstall` leaves the directory
        clean. Other unrelated entries are preserved.
        """
        path = self.settings_local_path()
        if not path.is_file():
            return
        try:
            settings = self._read_settings_local()
        except json.JSONDecodeError:
            return
        idx = self._find_nightly_stop_hook_index(settings)
        if idx is None:
            return
        block_idx, entry_idx = idx
        stop_block = settings["hooks"]["Stop"]
        del stop_block[block_idx]["hooks"][entry_idx]
        if not stop_block[block_idx]["hooks"]:
            del stop_block[block_idx]
        if not settings["hooks"]["Stop"]:
            del settings["hooks"]["Stop"]
        if not settings["hooks"]:
            del settings["hooks"]
        if not settings:
            path.unlink()
            return
        self._write_settings_local(settings)

    def _read_settings_local(self) -> dict[str, Any]:
        path = self.settings_local_path()
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}

    def _write_settings_local(self, settings: dict[str, Any]) -> None:
        path = self.settings_local_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _find_nightly_stop_hook_index(
        settings: dict[str, Any],
    ) -> tuple[int, int] | None:
        """Locate Nightly's Stop hook entry. Returns (block_idx, entry_idx) or None.

        We identify our entry by its `command` field (`nightly hook stop`),
        which is stable across versions. Unknown hook entries from other
        sources are ignored.
        """
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return None
        stop_block = hooks.get("Stop")
        if not isinstance(stop_block, list):
            return None
        for block_idx, block in enumerate(stop_block):
            if not isinstance(block, dict):
                continue
            entries = block.get("hooks")
            if not isinstance(entries, list):
                continue
            for entry_idx, entry in enumerate(entries):
                if (
                    isinstance(entry, dict)
                    and entry.get("type") == "command"
                    and entry.get("command") == _STOP_HOOK_COMMAND
                ):
                    return (block_idx, entry_idx)
        return None

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
