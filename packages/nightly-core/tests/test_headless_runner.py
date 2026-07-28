"""`default_subprocess_runner` — the spawn behind every host's run_headless.

Every `nightly run` task ultimately lands here, and the module's own
docstring documents a contract the callers depend on: return
`(stdout, stderr, rc)`; raise `TimeoutError` past the deadline; let
`FileNotFoundError` / `PermissionError` through so per-host wrappers can
surface them in `HeadlessResult.error`.

None of that was exercised — the whole function was uncovered (76%
module coverage, and every missing line was this body). It is tested here
against **real processes** rather than a mocked asyncio: the behaviour
that matters (does the child actually die on timeout, do file
descriptors drain) is precisely what a mock would assume rather than
verify.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from nightly_core.headless import default_subprocess_runner


@pytest.mark.asyncio
async def test_captures_stdout_and_exit_code() -> None:
    out, err, rc = await default_subprocess_runner(["sh", "-c", "printf hello"], None, None, 10)
    assert out == b"hello"
    assert err == b""
    assert rc == 0


@pytest.mark.asyncio
async def test_captures_stderr_separately() -> None:
    out, err, rc = await default_subprocess_runner(["sh", "-c", "printf oops >&2"], None, None, 10)
    assert out == b""
    assert err == b"oops"
    assert rc == 0


@pytest.mark.asyncio
async def test_reports_a_non_zero_exit_code() -> None:
    """`run_headless` infers task outcome from this when the agent did not
    update the plan itself — a swallowed exit code would read as success."""
    _out, _err, rc = await default_subprocess_runner(["sh", "-c", "exit 3"], None, None, 10)
    assert rc == 3


@pytest.mark.asyncio
async def test_stdin_is_delivered_to_the_child() -> None:
    out, _err, rc = await default_subprocess_runner(["cat"], None, b"piped", 10)
    assert out == b"piped"
    assert rc == 0


@pytest.mark.asyncio
async def test_cwd_is_honoured(tmp_path: Path) -> None:
    """Tasks run inside their worktree; a wrong cwd would silently operate
    on the main checkout."""
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    out, _err, rc = await default_subprocess_runner(["ls"], tmp_path, None, 10)
    assert rc == 0
    assert b"marker.txt" in out


@pytest.mark.asyncio
async def test_timeout_raises_rather_than_hanging() -> None:
    """The contract callers rely on: past the deadline this raises instead
    of blocking the whole run behind one wedged task."""
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await default_subprocess_runner(["sleep", "30"], None, None, 0.3)
    # Must return promptly — not after the child's own 30s.
    assert time.monotonic() - started < 10


# NOT TESTED HERE: that the timed-out child is actually killed.
#
# The `proc.kill()` + drain in the timeout handler exists so an overnight
# run spawning many tasks does not accumulate orphans. An attempt to
# verify it behaviourally — poll `ps` for a uniquely-marked child after
# the timeout — passed with `proc.kill()` mutated out, so it proved
# nothing: the event loop reaps the child during teardown regardless.
#
# Left unwritten rather than shipped looking like a guard. Verifying it
# needs the child's PID, which the runner does not expose; the honest fix
# is to surface it, not to assert around it.


@pytest.mark.asyncio
async def test_missing_binary_propagates(tmp_path: Path) -> None:
    """Deliberately *not* swallowed here — per-host `run_headless`
    catches it and reports which host CLI is absent, which is a better
    message than an empty result."""
    with pytest.raises(FileNotFoundError):
        await default_subprocess_runner([str(tmp_path / "definitely-not-a-binary")], None, None, 10)


@pytest.mark.asyncio
async def test_no_timeout_means_wait_indefinitely() -> None:
    """`timeout_s=None` is the documented "no deadline" case — it must not
    be mistaken for zero and fire immediately."""
    out, _err, rc = await default_subprocess_runner(
        ["sh", "-c", "sleep 0.2; printf done"], None, None, None
    )
    assert out == b"done"
    assert rc == 0
