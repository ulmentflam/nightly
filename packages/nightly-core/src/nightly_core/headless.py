"""Headless (subprocess) execution primitives.

When Nightly runs from cron, CI, or any other non-interactive context,
there's no host UI to drive the loop. The agent has to be spawned as a
subprocess via the host's non-interactive CLI entry point:

- Claude Code: `claude -p --output-format json`
- Codex CLI:   `codex exec --json`
- opencode:    `opencode run --format json`

Each primary host implements `NightlyHostIntegration.run_headless`, which
spawns the appropriate CLI and returns a normalized `HeadlessResult`.
Subscription credentials propagate naturally through the environment
(the spawned CLI reads `~/.claude/credentials.json`, `~/.codex/sessions/`,
etc.); Nightly's Python code never sees the token. If the user has set
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / etc. as a fallback for sandboxed
CI environments, those propagate too.

Cursor and Antigravity don't ship a synchronous headless CLI shape —
their remote queues have a different lifecycle. Their `run_headless`
stays `NotImplementedError`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from pydantic import BaseModel

__all__ = [
    "HeadlessResult",
    "SubprocessRunner",
    "default_subprocess_runner",
    "run_subprocess",
]


class HeadlessResult(BaseModel):
    """Normalized outcome of a single headless-subprocess invocation."""

    host_id: str
    """Which host produced this result (`claude` / `codex` / `opencode` / ...)."""

    output: str
    """Combined stdout — typically JSON when the CLI was asked for it."""

    stderr: str = ""
    """Combined stderr. Often empty on success."""

    exit_code: int
    """Process exit code. 0 = success."""

    elapsed_ms: int
    """Wall-clock duration. Useful for cost / pacing diagnostics."""

    error: str | None = None
    """Set when the runner couldn't even launch (binary missing, timeout, OSError).
    `output`/`exit_code` will be empty/non-zero in those cases."""

    @property
    def ok(self) -> bool:
        return self.error is None and self.exit_code == 0


# A runner returns (stdout_bytes, stderr_bytes, exit_code) for a given
# argv + cwd + stdin-bytes triple. Injectable so tests can substitute a
# deterministic fake without spawning real processes.
SubprocessRunner = Callable[
    [Sequence[str], Path | None, bytes | None, float | None],
    Awaitable[tuple[bytes, bytes, int]],
]


async def default_subprocess_runner(
    argv: Sequence[str],
    cwd: Path | None,
    stdin: bytes | None,
    timeout_s: float | None,
) -> tuple[bytes, bytes, int]:
    """Default `SubprocessRunner` — uses `asyncio.create_subprocess_exec`.

    Returns `(stdout, stderr, returncode)`. Raises `TimeoutError` if the
    process didn't exit within `timeout_s`; raises `FileNotFoundError` /
    `PermissionError` if the binary itself can't be launched. Per-host
    `run_headless` implementations catch those and surface them in the
    `HeadlessResult.error` field.
    """
    # Local import keeps module import cheap when headless isn't used.
    import asyncio  # noqa: PLC0415

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin),
            timeout=timeout_s,
        )
    except TimeoutError:
        proc.kill()
        # Drain so we don't leak file descriptors.
        import contextlib  # noqa: PLC0415

        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    return stdout, stderr, proc.returncode or 0


async def run_subprocess(  # noqa: PLR0913 - the canonical subprocess primitive needs every dimension
    *,
    host_id: str,
    argv: Sequence[str],
    cwd: Path | None = None,
    stdin: bytes | None = None,
    timeout_s: float | None = None,
    runner: SubprocessRunner | None = None,
) -> HeadlessResult:
    """Spawn a subprocess and normalize its outcome into a `HeadlessResult`.

    Each primary host's `run_headless` calls this with its own argv. All
    of the expected failure modes (timeout, missing binary, OSError) are
    caught and surfaced via `HeadlessResult.error`, so callers can branch
    on `result.ok` without exception handling.
    """
    import time  # noqa: PLC0415

    chosen_runner = runner or default_subprocess_runner
    start = time.monotonic()
    try:
        stdout, stderr, exit_code = await chosen_runner(argv, cwd, stdin, timeout_s)
    except TimeoutError:
        return HeadlessResult(
            host_id=host_id,
            output="",
            stderr="",
            exit_code=-1,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            error=f"timeout after {timeout_s}s",
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return HeadlessResult(
            host_id=host_id,
            output="",
            stderr="",
            exit_code=-1,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            error=f"subprocess error: {exc}",
        )
    return HeadlessResult(
        host_id=host_id,
        output=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        exit_code=exit_code,
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )
