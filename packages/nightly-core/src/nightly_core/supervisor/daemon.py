"""The supervisor loop — poll watched repos, respawn dead sessions.

Structure is deliberately boring: `poll_once` does one full pass and is
pure enough to test directly, and `run_forever` is a thin `while True`
around it with a sleep. Everything interesting — the trigger, the
launcher, the registry — lives in its own module. A daemon whose logic
can only be tested by running the daemon is a daemon nobody tests.

State lives at `~/.cache/nightly/supervisor/state.json` (liveness, last
events) and `events.jsonl` (append-only history). Per-run respawn counts
live under the run itself, not in daemon state, so they survive a daemon
restart and reset naturally when a new run starts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from nightly_core.config import load_supervisor_config
from nightly_core.paths import current_run_pointer, run_dir
from nightly_core.supervisor.registry import (
    WatchedRepo,
    list_repos,
    prune_repos,
    supervisor_home,
)
from nightly_core.supervisor.respawn import RespawnOutcome, respawn_host
from nightly_core.supervisor.trigger import (
    SUPERVISOR_ABORTED_FILENAME,
    RespawnVerdict,
    should_respawn,
)
from nightly_core.telemetry import build_summary, record_respawn

if TYPE_CHECKING:
    from nightly_core.config import SupervisorConfig

__all__ = [
    "Launcher",
    "PollResult",
    "SupervisorState",
    "already_running",
    "events_path",
    "main",
    "pid_path",
    "poll_once",
    "read_state",
    "run_forever",
    "state_path",
    "write_pid",
]

_log = logging.getLogger(__name__)

_RESPAWN_COUNT_FILENAME = "supervisor.count"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_DEFAULT_POLL_SECONDS = 30


class Launcher(Protocol):
    """How the daemon re-invokes a host.

    Injectable so tests can assert on respawn *decisions* without any
    terminal being opened — the decisions are the part worth testing.

    The repo argument is positional-only (`/`): a Protocol matches
    positional-or-keyword parameters by *name*, so without it every
    implementation would be forced to call its first parameter `repo`.
    """

    def __call__(self, repo: Path, /, *, host: str) -> RespawnOutcome: ...


def state_path() -> Path:
    return supervisor_home() / "state.json"


def events_path() -> Path:
    return supervisor_home() / "events.jsonl"


def pid_path() -> Path:
    return supervisor_home() / "supervisor.pid"


@dataclass
class SupervisorState:
    """What `nightly supervisor status` renders."""

    pid: int | None = None
    started_at: str | None = None
    last_poll: str | None = None
    polls: int = 0
    respawns: int = 0
    watched: list[str] = field(default_factory=list)
    last_event: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "last_poll": self.last_poll,
            "polls": self.polls,
            "respawns": self.respawns,
            "watched": self.watched,
            "last_event": self.last_event,
        }


@dataclass(frozen=True)
class PollResult:
    """One pass over every watched repo."""

    checked: int
    fired: int
    verdicts: tuple[tuple[Path, RespawnVerdict], ...] = ()


# ── pid / liveness ────────────────────────────────────────────────────────


def write_pid(pid: int | None = None) -> Path:
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid or os.getpid()}\n", encoding="utf-8")
    return path


def read_pid() -> int | None:
    try:
        return int(pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
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


def already_running() -> int | None:
    """PID of a live supervisor, or None.

    A stale PID file (daemon killed without cleanup) reads as not-running
    so the operator can start a fresh one without hunting for the file."""
    pid = read_pid()
    if pid is None or not _pid_alive(pid):
        return None
    return pid


# ── per-run respawn accounting ────────────────────────────────────────────


def read_respawn_count(run_path: Path) -> int:
    """How many times this run has been respawned.

    Stored under the run rather than in daemon state so it survives a
    daemon restart — otherwise restarting the supervisor would silently
    reset a budget that exists to stop runaway loops."""
    try:
        return int((run_path / _RESPAWN_COUNT_FILENAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def bump_respawn_count(run_path: Path) -> int:
    count = read_respawn_count(run_path) + 1
    try:
        (run_path / _RESPAWN_COUNT_FILENAME).write_text(f"{count}\n", encoding="utf-8")
    except OSError as exc:
        _log.debug("could not persist respawn count: %s", exc)
    return count


def _write_abort_marker(run_path: Path, detail: str) -> None:
    try:
        (run_path / SUPERVISOR_ABORTED_FILENAME).write_text(
            f"{datetime.now(UTC).strftime(_STAMP)}\n{detail}\n", encoding="utf-8"
        )
    except OSError as exc:
        _log.debug("could not write abort marker: %s", exc)


# ── event log ─────────────────────────────────────────────────────────────


def log_event(kind: str, **fields: Any) -> None:
    """Append one daemon event. Best-effort; never raises."""
    event = {"ts": datetime.now(UTC).strftime(_STAMP), "kind": kind, **fields}
    path = events_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True, default=str) + "\n")
    except OSError as exc:
        _log.debug("event log write failed: %s", exc)


def read_state() -> SupervisorState:
    try:
        parsed = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SupervisorState()
    if not isinstance(parsed, dict):
        return SupervisorState()
    return SupervisorState(
        pid=parsed.get("pid"),
        started_at=parsed.get("started_at"),
        last_poll=parsed.get("last_poll"),
        polls=parsed.get("polls", 0) or 0,
        respawns=parsed.get("respawns", 0) or 0,
        watched=parsed.get("watched") or [],
        last_event=parsed.get("last_event"),
    )


def write_state(state: SupervisorState) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _log.debug("state write failed: %s", exc)


# ── the loop ──────────────────────────────────────────────────────────────


def _current_run_path(repo: Path) -> Path | None:
    """Resolve `<repo>/.nightly/runs/CURRENT` to a run directory."""
    try:
        run_id = current_run_pointer(repo).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not run_id:
        return None
    path = run_dir(run_id, repo)
    return path if path.is_dir() else None


def poll_once(
    *,
    repos: list[WatchedRepo] | None = None,
    launcher: Launcher | None = None,
    now: datetime | None = None,
) -> PollResult:
    """One full pass: check every watched repo, respawn what needs it.

    `launcher` is injectable so tests can assert on respawn decisions
    without spawning terminals.
    """
    watched = repos if repos is not None else list_repos()
    verdicts: list[tuple[Path, RespawnVerdict]] = []
    fired = 0

    for repo in watched:
        run_path = _current_run_path(repo.path)
        if run_path is None:
            verdicts.append((repo.path, RespawnVerdict(False, "missing_run", "no CURRENT run")))
            continue

        cfg = load_supervisor_config(repo.path)
        count = read_respawn_count(run_path)
        verdict = should_respawn(
            run_path,
            stale_threshold_s=cfg.heartbeat_stale_seconds,
            max_respawns=cfg.max_respawns,
            respawn_count=count,
            enabled=cfg.enabled,
            now=now,
        )
        verdicts.append((repo.path, verdict))
        if not verdict.fire:
            continue

        _fire(repo.path, run_path, cfg=cfg, attempt=count + 1, launcher=launcher)
        fired += 1

    return PollResult(checked=len(watched), fired=fired, verdicts=tuple(verdicts))


def _fire(
    repo_path: Path,
    run_path: Path,
    *,
    cfg: SupervisorConfig,
    attempt: int,
    launcher: Launcher | None,
) -> None:
    """Execute one respawn attempt, with backoff, budget, and logging."""
    delay = _backoff_for(attempt, cfg.backoff_seconds)
    if delay:
        log_event("backoff", repo=str(repo_path), attempt=attempt, seconds=delay)
        _record(run_path, event="backoff", attempt=attempt, backoff_seconds=delay)
        time.sleep(delay)

    count = bump_respawn_count(run_path)

    try:
        outcome = (
            launcher(repo_path, host=cfg.host)
            if launcher is not None
            else respawn_host(repo_path, host=cfg.host)
        )
    except NotImplementedError as exc:
        log_event("unsupported_host", repo=str(repo_path), detail=str(exc))
        _record(run_path, event="skipped", attempt=count, detail=str(exc))
        _write_abort_marker(run_path, f"unsupported host {cfg.host!r}")
        return
    except Exception as exc:
        log_event("respawn_error", repo=str(repo_path), detail=repr(exc))
        _record(run_path, event="skipped", attempt=count, detail=repr(exc))
        return

    assert isinstance(outcome, RespawnOutcome)
    log_event(
        "respawn" if outcome.ok else "respawn_failed",
        repo=str(repo_path),
        attempt=count,
        strategy=outcome.strategy,
        detail=outcome.detail,
        full_resume=outcome.is_full_resume,
    )
    _record(
        run_path,
        event="respawn" if outcome.ok else "skipped",
        attempt=count,
        detail=f"{outcome.strategy}: {outcome.detail}",
    )

    if count >= cfg.max_respawns:
        detail = f"respawn budget of {cfg.max_respawns} exhausted"
        _write_abort_marker(run_path, detail)
        log_event("abort", repo=str(repo_path), detail=detail)
        _record(run_path, event="abort", attempt=count, detail=detail)


def _backoff_for(attempt: int, schedule: tuple[int, ...]) -> int:
    """Delay for `attempt` (1-based), clamped to the last schedule entry."""
    if not schedule:
        return 0
    index = min(max(attempt - 1, 0), len(schedule) - 1)
    return schedule[index]


def _record(run_path: Path, **kwargs: Any) -> None:
    """Mirror a supervisor event into the run's telemetry. Never raises."""
    try:
        record_respawn(run_path, **kwargs)
        build_summary(run_path)
    except Exception as exc:
        _log.debug("supervisor telemetry write failed: %s", exc)


def run_forever(
    *,
    poll_interval: int | None = None,
    max_polls: int | None = None,
    launcher: Launcher | None = None,
) -> int:
    """Poll until signalled. Returns the number of polls completed.

    `max_polls` bounds the loop for tests and for `--once` debugging;
    production passes None and relies on SIGTERM.
    """
    stopping = {"now": False}

    def _handle(signum: int, _frame: object) -> None:
        _log.info("supervisor received signal %s; stopping", signum)
        stopping["now"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Off the main thread (tests, embedded use) signal registration
        # raises; the `max_polls` bound is what terminates the loop there.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handle)

    write_pid()
    state = SupervisorState(pid=os.getpid(), started_at=datetime.now(UTC).strftime(_STAMP))
    dropped = prune_repos()
    if dropped:
        log_event("pruned_registry", dropped=dropped)
    log_event("started", pid=os.getpid())

    polls = 0
    while not stopping["now"]:
        if max_polls is not None and polls >= max_polls:
            break
        repos = list_repos()
        interval = poll_interval or _interval_for(repos)
        result = poll_once(repos=repos, launcher=launcher)
        polls += 1

        state.polls = polls
        state.respawns += result.fired
        state.last_poll = datetime.now(UTC).strftime(_STAMP)
        state.watched = [str(r.path) for r in repos]
        write_state(state)

        if stopping["now"] or (max_polls is not None and polls >= max_polls):
            break
        time.sleep(interval)

    log_event("stopped", polls=polls)
    with contextlib.suppress(OSError):
        pid_path().unlink()
    return polls


def _interval_for(repos: list[WatchedRepo]) -> int:
    """Poll interval — the shortest any watched repo asks for.

    Different repos can configure different intervals; the daemon is one
    loop, so it runs at the tightest requested cadence rather than
    silently ignoring the repo that wanted faster checks."""
    intervals = [load_supervisor_config(r.path).poll_interval_seconds for r in repos]
    return min(intervals) if intervals else _DEFAULT_POLL_SECONDS


def main() -> int:
    """Entry point for the `nightly-supervisor` script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
    )
    if (pid := already_running()) is not None:
        _log.error("supervisor already running (pid %s)", pid)
        return 1
    run_forever()
    return 0
