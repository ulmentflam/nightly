"""When to respawn — the four-condition trigger, as a pure function.

This is the whole safety argument for the supervisor, so it lives alone
in a module with no I/O beyond `stat` and no daemon machinery, and it
returns a *reason* rather than a bare bool so every decision the daemon
makes is explainable in its log.

The condition that matters, and why it is not obvious:
`RESPAWN_REQUESTED` is written **preemptively** on every forced-
continuation block (v0.0.10), so it is present throughout a healthy
overnight session. Its presence means "if this session dies right now,
resume it" — not "this session has died." The death signal is the
heartbeat going stale *while* the marker is present. Confusing those two
would make the supervisor respawn into a live session on its first poll,
producing two agents racing on one repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "SUPERVISOR_ABORTED_FILENAME",
    "RespawnVerdict",
    "heartbeat_age_seconds",
    "should_respawn",
]

SUPERVISOR_ABORTED_FILENAME = "SUPERVISOR_ABORTED"
"""Written beside RESPAWN_REQUESTED when the respawn budget is spent.

Presence means "the supervisor gave up on this run" — it stops further
respawns and surfaces in the morning briefing, so an operator who wakes
to a stalled session learns it stalled *and* that the supervisor tried."""

_HEARTBEAT_FILENAME = "keepalive.log"
_SESSION_ACTIVE_FILENAME = "SESSION_ACTIVE"
_RESPAWN_REQUESTED_FILENAME = "RESPAWN_REQUESTED"


@dataclass(frozen=True)
class RespawnVerdict:
    """Whether to respawn, and the reason either way.

    `reason` is a stable slug for logs and tests, never prose: `fire`,
    `no_session`, `concluded`, `stopped`, `no_marker`, `heartbeat_fresh`,
    `budget_exhausted`, `disabled`, `missing_run`.
    """

    fire: bool
    reason: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.fire


def heartbeat_age_seconds(run_path: Path, *, now: datetime | None = None) -> float | None:
    """Seconds since `keepalive.log` was last written, or None if absent.

    Absent is *not* treated as stale by the caller. A run whose hook has
    never fired has no heartbeat yet, and respawning it would mean
    launching a second session against a repo the operator may have only
    just started.
    """
    path = run_path / _HEARTBEAT_FILENAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    moment = (now or datetime.now(UTC)).timestamp()
    return max(0.0, moment - mtime)


def should_respawn(  # noqa: PLR0911, PLR0913 - one branch and one knob per trigger condition
    run_path: Path,
    *,
    stale_threshold_s: int = 90,
    max_respawns: int = 5,
    respawn_count: int = 0,
    enabled: bool = True,
    now: datetime | None = None,
) -> RespawnVerdict:
    """Decide whether `run_path` needs a respawn right now.

    Order is chosen so the most decisive negative wins: an operator who
    ran `nightly conclude` must never see a respawn, regardless of what
    the markers or the heartbeat say.
    """
    if not enabled:
        return RespawnVerdict(False, "disabled", "supervisor.enabled is false for this repo")

    if not run_path.is_dir():
        return RespawnVerdict(False, "missing_run", f"{run_path} is not a directory")

    # 1. Never supervise a session Nightly did not arm.
    if not (run_path / _SESSION_ACTIVE_FILENAME).is_file():
        return RespawnVerdict(False, "no_session", "no SESSION_ACTIVE marker")

    # 2. Operator off-ramps outrank everything.
    if (run_path / "CONCLUDE").is_file():
        return RespawnVerdict(False, "concluded", "CONCLUDE marker present")
    if (run_path / "STOP").is_file():
        return RespawnVerdict(False, "stopped", "STOP marker present")

    # A spent budget is sticky — without this the daemon would re-fire
    # every poll forever once the count passed the cap.
    if (run_path / SUPERVISOR_ABORTED_FILENAME).is_file():
        return RespawnVerdict(False, "budget_exhausted", "SUPERVISOR_ABORTED marker present")
    if respawn_count >= max_respawns:
        return RespawnVerdict(
            False,
            "budget_exhausted",
            f"{respawn_count} respawn(s) already, cap is {max_respawns}",
        )

    # 3. The hook must have signalled that a death here is resumable.
    if not (run_path / _RESPAWN_REQUESTED_FILENAME).is_file():
        return RespawnVerdict(False, "no_marker", "no RESPAWN_REQUESTED marker")

    # 4. And the session must actually be dead, not merely blocking.
    age = heartbeat_age_seconds(run_path, now=now)
    if age is None:
        return RespawnVerdict(False, "heartbeat_fresh", "no keepalive.log yet")
    if age < stale_threshold_s:
        return RespawnVerdict(
            False,
            "heartbeat_fresh",
            f"heartbeat {age:.0f}s old, threshold {stale_threshold_s}s",
        )

    return RespawnVerdict(
        True,
        "fire",
        f"heartbeat stale {age:.0f}s (>{stale_threshold_s}s) with RESPAWN_REQUESTED present",
    )
