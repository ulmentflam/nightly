"""Background dispatch — fire-and-forget specialist sub-processes.

The default for *interactive* Nightly sessions is to background every
specialist sub-agent (implementer / tester / reviewer / researcher) so
the operator's chat stays free for other work. This module owns the
spawn machinery and the on-disk state that makes the dispatch
inspectable across turns.

Why not just use the host's blocking sub-agent primitive (Claude
Code's Task tool, Codex MCP dispatch, opencode's `/session/:id/fork`)?
Because those primitives — by design — pause the *calling* session
until the specialist returns. For an unattended overnight run that's
fine. For an interactive session where the operator wants to alt-tab
between chat and a code reviewer, those primitives are a UX hostage
situation. Background spawn lets the operator keep typing.

The spawn shape:

    subprocess.Popen(
        argv,                        # per-host headless CLI argv
        cwd=worktree_path,
        stdout=dispatch.log,         # appended; readable via `nightly dispatch tail`
        stderr=STDOUT,
        stdin=DEVNULL,               # never blocks on input
        start_new_session=True,      # detached process group; parent can exit
    )

POSIX-only by design — Nightly targets macOS / Linux. Windows would
need `creationflags=DETACHED_PROCESS`, but no host's CLI ships
natively on Windows today.

State lives at `.nightly/runs/<run-id>/tasks/<slug>/dispatch.json`.
The file is updated when the spawn happens (status: `running`) and on
every `status`/`wait` poll that detects the PID has exited
(status: `completed` / `failed`). The log file lives next to it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from nightly_core.contract import HostId, SpecialistRole
from nightly_core.paths import repo_root

__all__ = [
    "DEFAULT_LOG_FILENAME",
    "DEFAULT_STATE_FILENAME",
    "BackgroundDispatchResult",
    "DispatchStatus",
    "build_argv",
    "is_alive",
    "list_dispatches",
    "read_dispatch_state",
    "start_background",
    "wait_for",
    "write_dispatch_state",
]


DEFAULT_STATE_FILENAME = "dispatch.json"
DEFAULT_LOG_FILENAME = "dispatch.log"

DispatchStatus = Literal["running", "completed", "failed", "unknown"]

# Module-local factory alias for the default Popen. Tests can monkeypatch
# THIS without affecting global `subprocess.Popen` (which click + typer
# use internally for I/O capture — patching the global one breaks
# CliRunner). Production code never touches this directly.
_DEFAULT_POPEN_FACTORY = subprocess.Popen


@dataclass(frozen=True)
class BackgroundDispatchResult:
    """Snapshot of one background dispatch.

    `status` reflects the last polled state — `running` if the PID is
    still alive, `completed` once the process exited cleanly,
    `failed` if it exited non-zero, `unknown` when the PID can't be
    probed (e.g. cross-machine state from a different operator).

    `exit_code` is filled in only for `completed`/`failed` results.
    """

    slug: str
    role: SpecialistRole
    host: HostId
    pid: int
    log_path: Path
    started_at: datetime
    argv: tuple[str, ...] = field(default_factory=tuple)
    cwd: Path | None = None
    status: DispatchStatus = "running"
    exit_code: int | None = None
    finished_at: datetime | None = None


# ── per-host argv ────────────────────────────────────────────────────────


def build_argv(host: HostId, prompt: str, *, session_id: str | None = None) -> list[str] | None:  # noqa: PLR0911 - one return per host backend is the whole point
    """Build the headless argv for `host`. Returns None when the host
    has no usable headless backend yet (cursor, antigravity).

    Reuses the same flags each host's `run_headless` already invokes —
    see the integration packages for canonical references.
    """
    if host == "claude":
        binary = shutil.which("claude")
        if binary is None:
            return None
        argv = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
        ]
        if session_id:
            argv += ["--session-id", session_id]
        return argv

    if host == "codex":
        binary = shutil.which("codex")
        if binary is None:
            return None
        return [
            binary,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            prompt,
        ]

    if host == "opencode":
        binary = shutil.which("opencode")
        if binary is None:
            return None
        return [binary, "run", prompt, "--format", "json"]

    if host == "gemini":
        binary = shutil.which("gemini")
        if binary is None:
            return None
        return [binary, "--prompt", prompt]

    # cursor + antigravity don't expose a usable headless CLI today.
    # Callers can fall back to the host's blocking primitive (Background
    # Agent / Agent Manager registration) or to claude/codex if those
    # binaries are also on PATH.
    return None


# ── spawn ────────────────────────────────────────────────────────────────


def start_background(  # noqa: PLR0913 - dispatch primitive needs every dimension
    slug: str,
    *,
    role: SpecialistRole,
    host: HostId,
    prompt: str,
    root: Path | None = None,
    cwd: Path | None = None,
    session_id: str | None = None,
    now: datetime | None = None,
    popen_factory: object | None = None,
) -> BackgroundDispatchResult:
    """Spawn the host's headless CLI as a detached background process.

    Returns immediately with a `BackgroundDispatchResult` carrying the
    PID + log path. The calling session is free; the spawn writes to
    the log file independently. Status is reachable later via
    `read_dispatch_state(slug)` + `is_alive(pid)`.

    `popen_factory` is injectable for tests — defaults to
    `subprocess.Popen`. Production callers leave it unset.
    """
    argv = build_argv(host, prompt, session_id=session_id)
    if argv is None:
        msg = (
            f"no background dispatch backend for host '{host}'. "
            "claude/codex/opencode/gemini are supported when their binaries "
            "are on PATH; cursor/antigravity have no headless CLI today — "
            "use the host's native primitive (Background Agent / Agent "
            "Manager) for those."
        )
        raise RuntimeError(msg)

    repo = (root or repo_root()).resolve()
    work_cwd = (cwd or repo).resolve()
    started_at = now or datetime.now(UTC)

    log_path = _task_dir(slug, repo) / DEFAULT_LOG_FILENAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    log_handle.write(
        f"--- dispatch started {started_at.strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"slug={slug} role={role} host={host} ---\n".encode()
    )
    log_handle.flush()

    factory = popen_factory or _DEFAULT_POPEN_FACTORY
    proc = factory(  # type: ignore[operator]
        argv,
        cwd=str(work_cwd),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    log_handle.close()  # ownership transferred to the child via fork

    result = BackgroundDispatchResult(
        slug=slug,
        role=role,
        host=host,
        pid=proc.pid,
        log_path=log_path,
        started_at=started_at,
        argv=tuple(argv),
        cwd=work_cwd,
        status="running",
        exit_code=None,
        finished_at=None,
    )
    write_dispatch_state(result, root=repo)
    return result


# ── state I/O ────────────────────────────────────────────────────────────


def _task_dir(slug: str, root: Path) -> Path:
    """Resolve `.nightly/runs/<current>/tasks/<NNNN>-<slug>/`.

    The directory must already exist — created by `nightly task <slug>`
    or `start_run(task=...)`. We don't create it here because callers
    that dispatch against an unknown slug deserve a clear error.
    """
    from nightly_core.runs import current_run  # noqa: PLC0415 - lazy

    run = current_run(root)
    if run is None:
        msg = "no active run; `nightly start` first"
        raise RuntimeError(msg)
    tasks = run.path / "tasks"
    for entry in tasks.iterdir():
        if entry.is_dir() and entry.name.endswith(f"-{slug}"):
            return entry
    msg = f"task `{slug}` not found in run {run.id}"
    raise RuntimeError(msg)


def write_dispatch_state(result: BackgroundDispatchResult, *, root: Path | None = None) -> Path:
    """Persist `result` to `.nightly/runs/<run-id>/tasks/<slug>/dispatch.json`.

    Returns the on-disk path. Best-effort: a write failure shouldn't
    crash the dispatcher (the PID is still spawned and reachable via
    /proc); we re-raise OSError so callers can decide.
    """
    repo = (root or repo_root()).resolve()
    state_path = _task_dir(result.slug, repo) / DEFAULT_STATE_FILENAME
    payload = asdict(result)
    payload["log_path"] = str(result.log_path)
    payload["cwd"] = str(result.cwd) if result.cwd is not None else None
    payload["started_at"] = result.started_at.isoformat()
    payload["finished_at"] = (
        result.finished_at.isoformat() if result.finished_at is not None else None
    )
    payload["argv"] = list(result.argv)
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return state_path


def read_dispatch_state(slug: str, *, root: Path | None = None) -> BackgroundDispatchResult | None:
    """Load the last-recorded dispatch state for `slug`, or None.

    A None return means either the task has never been dispatched or
    the state file was deleted. Either way, the dispatch is treated
    as not-in-flight.
    """
    repo = (root or repo_root()).resolve()
    try:
        task_dir = _task_dir(slug, repo)
    except RuntimeError:
        return None
    state_path = task_dir / DEFAULT_STATE_FILENAME
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return BackgroundDispatchResult(
            slug=data["slug"],
            role=data["role"],
            host=data["host"],
            pid=int(data["pid"]),
            log_path=Path(data["log_path"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            argv=tuple(data.get("argv", [])),
            cwd=Path(data["cwd"]) if data.get("cwd") else None,
            status=data.get("status", "unknown"),
            exit_code=data.get("exit_code"),
            finished_at=(
                datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None
            ),
        )
    except (KeyError, ValueError):
        return None


def list_dispatches(root: Path | None = None) -> list[BackgroundDispatchResult]:
    """Every dispatch in the current run that has a state file on disk.

    Used by `nightly dispatch status` to enumerate active + finished
    spawns. Results are not refreshed against `is_alive` here —
    callers that want live status should call `refresh()` per result.
    """
    repo = (root or repo_root()).resolve()
    from nightly_core.runs import current_run  # noqa: PLC0415 - lazy

    run = current_run(repo)
    if run is None:
        return []
    out: list[BackgroundDispatchResult] = []
    tasks = run.path / "tasks"
    if not tasks.is_dir():
        return []
    for task_dir in sorted(tasks.iterdir()):
        if not task_dir.is_dir():
            continue
        # Slug is everything after `NNNN-`.
        try:
            slug = task_dir.name.split("-", 1)[1]
        except IndexError:
            continue
        state = read_dispatch_state(slug, root=repo)
        if state is not None:
            out.append(state)
    return out


# ── liveness ─────────────────────────────────────────────────────────────


def is_alive(pid: int) -> bool:
    """POSIX-only liveness probe via signal 0.

    `os.kill(pid, 0)` raises ProcessLookupError if the PID is dead,
    PermissionError if it exists but we can't signal it (which still
    counts as "alive — owned by someone else"), OSError on unknown
    failures. Returns False on the dead-PID case, True otherwise.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def refresh(
    result: BackgroundDispatchResult, *, root: Path | None = None
) -> BackgroundDispatchResult:
    """Re-poll `result`'s PID; update status on disk if it transitioned.

    A `running` dispatch becomes `completed` when the PID is dead and
    the process group exit code is unavailable (which is the common
    case — we can't `waitpid` on detached children we didn't fork).
    Detecting `failed` vs `completed` here is best-effort; the dispatch
    log is the canonical source.
    """
    if result.status != "running":
        return result
    if is_alive(result.pid):
        return result
    finished = BackgroundDispatchResult(
        slug=result.slug,
        role=result.role,
        host=result.host,
        pid=result.pid,
        log_path=result.log_path,
        started_at=result.started_at,
        argv=result.argv,
        cwd=result.cwd,
        status="completed",
        exit_code=None,
        finished_at=datetime.now(UTC),
    )
    with contextlib.suppress(OSError):
        write_dispatch_state(finished, root=root)
    return finished


def wait_for(
    slug: str,
    *,
    root: Path | None = None,
    timeout_s: float | None = None,
    poll_interval_s: float = 1.0,
) -> BackgroundDispatchResult | None:
    """Block until the dispatch for `slug` finishes (or `timeout_s` elapses).

    Returns the final state, or None if no dispatch exists for `slug`.
    On timeout, returns the still-`running` state — callers should
    check `result.status` to decide.
    """
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None
    state = read_dispatch_state(slug, root=root)
    if state is None:
        return None
    while True:
        state = refresh(state, root=root)
        if state.status != "running":
            return state
        if deadline is not None and time.monotonic() >= deadline:
            return state
        time.sleep(poll_interval_s)


# ── exported helpers ─────────────────────────────────────────────────────


def supported_hosts() -> Sequence[HostId]:
    """Hosts with a usable background-dispatch backend.

    Returns the canonical list — does NOT probe PATH. Use this to
    populate help text and the SKILL.md "supported hosts" list.
    """
    return ("claude", "codex", "opencode", "gemini")
