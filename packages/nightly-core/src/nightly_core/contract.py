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

HostId = Literal["claude", "codex", "cursor", "opencode", "antigravity"]
"""The five supported interactive hosts."""

SpecialistRole = Literal["implementer", "tester", "reviewer", "researcher"]
"""Roles dispatched as sub-agents through the host's native primitive."""

InstallScope = Literal["project", "user"]
"""Where to install the host launcher — repo-local or user-global."""


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
