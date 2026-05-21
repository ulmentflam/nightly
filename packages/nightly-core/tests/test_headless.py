"""Tests for nightly_core.headless."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from nightly_core.headless import HeadlessResult, SubprocessRunner, run_subprocess


def _fake_runner(
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int = 0,
    *,
    raise_with: BaseException | None = None,
) -> SubprocessRunner:
    """Build a fake `SubprocessRunner` that returns a fixed triple."""

    async def fake(
        argv: Sequence[str],
        cwd: Path | None,
        stdin: bytes | None,
        timeout_s: float | None,
    ) -> tuple[bytes, bytes, int]:
        if raise_with is not None:
            raise raise_with
        return stdout, stderr, exit_code

    return fake


# ── HeadlessResult ────────────────────────────────────────────────────────


def test_headless_result_ok_when_exit_zero_no_error() -> None:
    result = HeadlessResult(host_id="claude", output="ok", exit_code=0, elapsed_ms=10)
    assert result.ok is True


def test_headless_result_not_ok_when_nonzero_exit() -> None:
    result = HeadlessResult(host_id="claude", output="", exit_code=1, elapsed_ms=10)
    assert result.ok is False


def test_headless_result_not_ok_when_error_set() -> None:
    result = HeadlessResult(
        host_id="claude",
        output="",
        exit_code=0,
        elapsed_ms=10,
        error="timeout",
    )
    assert result.ok is False


# ── run_subprocess success / failure modes ──────────────────────────────


@pytest.mark.asyncio
async def test_run_subprocess_normalizes_success() -> None:
    result = await run_subprocess(
        host_id="claude",
        argv=("claude", "-p", "hi"),
        runner=_fake_runner(stdout=b'{"msg":"hi"}', exit_code=0),
    )
    assert result.host_id == "claude"
    assert result.output == '{"msg":"hi"}'
    assert result.exit_code == 0
    assert result.error is None
    assert result.ok is True
    assert result.elapsed_ms >= 0


@pytest.mark.asyncio
async def test_run_subprocess_carries_nonzero_exit() -> None:
    result = await run_subprocess(
        host_id="codex",
        argv=("codex",),
        runner=_fake_runner(stderr=b"boom", exit_code=7),
    )
    assert result.exit_code == 7
    assert result.stderr == "boom"
    assert result.error is None  # subprocess ran but failed
    assert result.ok is False


@pytest.mark.asyncio
async def test_run_subprocess_surfaces_timeout_as_error() -> None:
    result = await run_subprocess(
        host_id="claude",
        argv=("claude",),
        runner=_fake_runner(raise_with=TimeoutError("too slow")),
        timeout_s=0.1,
    )
    assert result.exit_code == -1
    assert result.error is not None
    assert "timeout" in result.error.lower()
    assert result.ok is False


@pytest.mark.asyncio
async def test_run_subprocess_surfaces_missing_binary() -> None:
    result = await run_subprocess(
        host_id="opencode",
        argv=("/nope/opencode",),
        runner=_fake_runner(raise_with=FileNotFoundError("/nope/opencode")),
    )
    assert result.exit_code == -1
    assert result.error is not None
    assert "subprocess error" in result.error.lower()


@pytest.mark.asyncio
async def test_run_subprocess_decodes_bytes_with_replacement() -> None:
    """Output isn't always valid UTF-8 (CLI tools sometimes mix encodings).
    The decoder should never raise."""
    result = await run_subprocess(
        host_id="claude",
        argv=("claude",),
        runner=_fake_runner(stdout=b"valid\xff\xfeinvalid", exit_code=0),
    )
    # Replacement characters land in place of invalid bytes.
    assert "valid" in result.output
    assert "invalid" in result.output


# ── default contract: secondary hosts inherit NotImplementedError ────────


@pytest.mark.asyncio
async def test_contract_default_raises_for_unsupported_host() -> None:
    """The ABC default tells the caller that headless isn't wired for this host."""
    from nightly_core.contract import (
        AuthStatus,
        HostId,
        InstallScope,
        NightlyHostIntegration,
        SubAgentResult,
    )

    class FakeSecondaryHost(NightlyHostIntegration):
        host_id: HostId = "cursor"

        async def install(self, scope: InstallScope) -> None:
            return None

        async def uninstall(self, scope: InstallScope) -> None:
            return None

        def is_installed(self, scope: InstallScope) -> bool:
            return False

        def session_id(self) -> str:
            return "fake"

        async def dispatch_sub_agent(self, **_: object) -> SubAgentResult:
            raise NotImplementedError

        async def request_approval(self, q: str, choices: list[str]) -> str:
            raise NotImplementedError

        async def auth_status(self) -> AuthStatus:
            return AuthStatus(ok=True)

    host = FakeSecondaryHost()
    with pytest.raises(NotImplementedError, match="does not support headless"):
        await host.run_headless("hi")
