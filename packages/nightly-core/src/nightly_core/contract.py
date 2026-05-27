"""The NightlyHostIntegration contract.

Every per-host package (nightly-host-claude, nightly-host-codex, ...)
implements this ABC. The shared core (loop, priority cascade, drain,
briefing renderer) calls these methods without knowing which host it's
inside — that's the seam between Nightly and the world.

See `.planning/brainstorm.html` §05 ("Hosts & how Nightly plugs into each").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from nightly_core.headless import HeadlessResult

HostId = Literal["claude", "codex", "cursor", "opencode", "antigravity", "gemini"]
"""The six supported interactive hosts.

`antigravity` and `gemini` both write under `.gemini/` — the former is the
desktop IDE's managed-agent surface (`.gemini/antigravity/agents/`), the
latter is vanilla Gemini CLI custom commands (`.gemini/commands/`)."""

SpecialistRole = Literal["implementer", "tester", "reviewer", "researcher"]
"""Roles dispatched as sub-agents through the host's native primitive."""

InstallScope = Literal["project", "user"]
"""Where to install the host launcher — repo-local or user-global."""


KeepaliveSupport = Literal["forced", "soft", "none"]
"""How a host enforces Nightly's never-stop contract.

- `forced`: the host exposes a Stop-style hook that can force the model
  to continue with a new prompt at every turn boundary. Claude Code,
  Codex CLI, and Cursor 1.7+ are in this tier.
- `soft`: no hook surface, but the host honors AGENTS.md / CLAUDE.md
  rules text. The keep-alive is best-effort: the model is *told* to
  never stop. opencode and Antigravity sit here today.
- `none`: neither hook nor rules are honored. No host is in this tier
  today; reserved for hypothetical hostile hosts.
"""


class SubAgentResult(BaseModel):
    """Normalized result of a sub-agent dispatch through the host's primitive."""

    role: SpecialistRole
    output: str
    tool_calls: list[dict]
    elapsed_ms: int


class AuthStatus(BaseModel):
    """Subscription health check result."""

    ok: bool
    plan: str | None = None
    expires_at: datetime | None = None


class NightlyHostIntegration(ABC):
    """Abstract contract for a per-host Nightly integration.

    Phase 1 of any host implementation needs only `install`, `uninstall`,
    `is_installed`, and `session_id`. The async methods that depend on the
    host's sub-agent primitives may raise `NotImplementedError` until the
    matching phase lands.
    """

    host_id: HostId

    keepalive_support: KeepaliveSupport = "soft"
    """Default to `soft` (rules-only). Hosts with a real Stop-hook
    surface override this to `forced` and implement the hook-install
    methods below."""

    # ── launcher lifecycle ────────────────────────────────────────────────
    @abstractmethod
    async def install(self, scope: InstallScope) -> None:
        """Idempotent: install the launcher (Skill / command / agent) into the host."""

    @abstractmethod
    async def uninstall(self, scope: InstallScope) -> None:
        """Idempotent: remove the launcher previously installed by `install`."""

    @abstractmethod
    def is_installed(self, scope: InstallScope) -> bool:
        """Return True if the launcher is currently present at the given scope."""

    # ── session identity ──────────────────────────────────────────────────
    @abstractmethod
    def session_id(self) -> str:
        """The host's session id — recorded in session.jsonl for audit / replay.

        When called outside an active host session, implementations may
        return a synthetic id (e.g., "detached-<uuid>") so callers can still
        correlate artifacts. The returned id must be filesystem-safe.
        """

    # ── runtime primitives — async, often Phase 2+ ───────────────────────
    @abstractmethod
    async def dispatch_sub_agent(
        self,
        *,
        role: SpecialistRole,
        prompt: str,
        cwd: str,
        allowed_tools: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> SubAgentResult:
        """Dispatch a specialist sub-agent through the host's native primitive."""

    @abstractmethod
    async def request_approval(self, q: str, choices: list[str]) -> str:
        """Surface an approval question through the host's native UI."""

    @abstractmethod
    async def auth_status(self) -> AuthStatus:
        """Verify the subscription is alive and won't expire mid-session."""

    # ── headless mode — Phase 7+ ──────────────────────────────────────────
    async def run_headless(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        timeout_s: float | None = None,
    ) -> HeadlessResult:
        """Spawn the host's non-interactive CLI and return a normalized result.

        Default raises `NotImplementedError` — secondary hosts whose
        headless story is a remote queue (Cursor Cloud, Antigravity
        Managed Agents) don't ship this synchronous shape. Primary hosts
        (Claude Code, Codex, opencode) override.

        Subscription credentials propagate via the environment: the
        spawned CLI reads its own cached creds (no Nightly token plumbing).
        Set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / etc. before invoking
        for sandboxed CI environments that lack a persistent home dir.
        """
        msg = (
            f"Host '{self.host_id}' does not support headless mode. "
            "Headless is currently implemented for the primary hosts only "
            "(claude, codex, opencode); secondary hosts use a remote queue "
            "with a different lifecycle shape."
        )
        raise NotImplementedError(msg)

    # ── conclude skill (Phase 9i) ────────────────────────────────────────
    def conclude_skill_path(self, scope: InstallScope) -> Path | None:
        """Per-host path to the `/nightly-conclude` skill file.

        Default returns None — opt-in. Hosts that override return the
        absolute path where the conclude skill should be written. Each
        host is responsible for whether its conclude skill is a flat
        `.md` file (Cursor) or a `<name>/SKILL.md` folder (the others).
        """
        return None

    def is_conclude_installed(self, scope: InstallScope) -> bool:
        path = self.conclude_skill_path(scope)
        return path is not None and path.is_file()

    # ── update skill (Phase 9j) ──────────────────────────────────────────
    def update_skill_path(self, scope: InstallScope) -> Path | None:
        """Per-host path to the `/nightly-update` skill file.

        Same lifecycle as `conclude_skill_path`. Hosts that ship a real
        update skill override this to return the right per-host path.
        """
        return None

    def is_update_installed(self, scope: InstallScope) -> bool:
        path = self.update_skill_path(scope)
        return path is not None and path.is_file()

    # ── init skill (global-install bootstrap) ────────────────────────────
    def init_skill_path(self, scope: InstallScope) -> Path | None:
        """Per-host path to the `/nightly-init` skill file.

        Default returns None — opt-in. Hosts override to return the
        absolute path where the init skill should be written. The init
        skill exists primarily for the user-scope install path: drop
        Nightly into `~/.<host>/skills/` once, then bootstrap any repo
        from inside the host by typing `/nightly-init`.
        """
        return None

    def is_init_installed(self, scope: InstallScope) -> bool:
        path = self.init_skill_path(scope)
        return path is not None and path.is_file()

    # ── bug skill (Phase 9n) ─────────────────────────────────────────────
    def bug_skill_path(self, scope: InstallScope) -> Path | None:
        """Per-host path to the `/nightly-bug` skill file.

        Default returns None — opt-in. Hosts that override return the
        absolute path where the bug skill should be written. The bug
        skill mirrors `/nightly-conclude`'s lifecycle and is also a
        HUMAN-ONLY off-ramp (see `nightly_core.bug` and rules.py
        rule 10).
        """
        return None

    def is_bug_installed(self, scope: InstallScope) -> bool:
        path = self.bug_skill_path(scope)
        return path is not None and path.is_file()

    # ── keep-alive hook (Phase 9h+) ──────────────────────────────────────
    def install_keepalive_hook(self, scope: InstallScope) -> None:
        """Idempotent: write the Stop-hook entry into the host's config.

        Default is a no-op for hosts whose `keepalive_support == "soft"`.
        Hosts in the `forced` tier override to merge into their respective
        settings file (.claude/settings.local.json, .codex/hooks.json,
        .cursor/hooks.json). Only the `project` scope writes a hook —
        `user` scope shares hooks across repos and shouldn't pin one
        repo's `.nightly/` state into the user-global config.
        """
        return

    def uninstall_keepalive_hook(self, scope: InstallScope) -> None:
        """Idempotent: remove the Stop-hook entry. Default no-op."""
        return

    def is_keepalive_hook_installed(self, scope: InstallScope) -> bool:
        """True iff this host has a hook entry pointing at `nightly hook stop`."""
        return False
