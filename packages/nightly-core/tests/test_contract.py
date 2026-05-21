"""Tests for the NightlyHostIntegration contract surface."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest

from nightly_core import (
    AuthStatus,
    HostId,
    InstallScope,
    NightlyHostIntegration,
    SpecialistRole,
    SubAgentResult,
)


def test_host_id_literal_covers_five_supported_hosts() -> None:
    assert set(get_args(HostId)) == {"claude", "codex", "cursor", "opencode", "antigravity"}


def test_specialist_role_literal() -> None:
    assert set(get_args(SpecialistRole)) == {
        "implementer",
        "tester",
        "reviewer",
        "researcher",
    }


def test_install_scope_literal() -> None:
    assert set(get_args(InstallScope)) == {"project", "user"}


def test_sub_agent_result_round_trips() -> None:
    result = SubAgentResult(
        role="implementer",
        output="ok",
        tool_calls=[{"name": "edit"}],
        elapsed_ms=123,
    )
    assert result.role == "implementer"
    assert result.tool_calls[0]["name"] == "edit"


def test_auth_status_defaults() -> None:
    auth = AuthStatus(ok=False)
    assert auth.ok is False
    assert auth.plan is None
    assert auth.expires_at is None


def test_auth_status_full() -> None:
    expires = datetime(2026, 12, 31, tzinfo=UTC)
    auth = AuthStatus(ok=True, plan="pro", expires_at=expires)
    assert auth.plan == "pro"
    assert auth.expires_at == expires


def test_abc_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        NightlyHostIntegration()  # type: ignore[abstract]


def test_minimal_subclass_can_instantiate() -> None:
    class FakeHost(NightlyHostIntegration):
        host_id: HostId = "claude"

        async def install(self, scope: InstallScope) -> None:
            return None

        async def uninstall(self, scope: InstallScope) -> None:
            return None

        def is_installed(self, scope: InstallScope) -> bool:
            return False

        def session_id(self) -> str:
            return "fake-session"

        async def dispatch_sub_agent(self, **_: object) -> SubAgentResult:
            raise NotImplementedError

        async def request_approval(self, q: str, choices: list[str]) -> str:
            raise NotImplementedError

        async def auth_status(self) -> AuthStatus:
            return AuthStatus(ok=True)

    host = FakeHost()
    assert host.host_id == "claude"
    assert host.session_id() == "fake-session"
