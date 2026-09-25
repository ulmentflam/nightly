"""Respawn supervisor — auto-resume a Nightly session that died involuntarily.

RFC 010 Phase B. The Stop hook keeps an interactive session alive across
turn boundaries, but it cannot survive the two failure modes that
actually end overnight runs: Claude Code's without-progress cap
overriding the hook, and the host process dying outright (crash, OOM,
disconnect). Neither fires another hook event, so nothing in-process can
react.

The supervisor is out-of-process by necessity. It watches for the
combination of a `RESPAWN_REQUESTED` marker (written preemptively by the
hook, meaning "a death here is resumable") and a stale `keepalive.log`
heartbeat (meaning "a death has happened"), then re-invokes the host.

Module layout:

- `trigger`  — `should_respawn()`, the four-condition decision. Pure.
- `registry` — which repos to watch, on disk, one daemon per machine.
- `respawn`  — how to actually get a host running again (tmux → Terminal
               → headless fallback).
- `daemon`   — the poll loop and its state.
- `service`  — launchd / systemd registration.

The operator-facing verbs are `nightly supervisor install / uninstall /
start / status / logs`. Per the rules block, **the agent never runs
these** — installing a background daemon is an operator decision.
"""

from __future__ import annotations

from nightly_core.supervisor.daemon import (
    PollResult,
    SupervisorState,
    already_running,
    main,
    poll_once,
    read_state,
    run_forever,
)
from nightly_core.supervisor.registry import (
    WatchedRepo,
    forget_repo,
    list_repos,
    prune_repos,
    register_repo,
)
from nightly_core.supervisor.respawn import RespawnOutcome, respawn_host
from nightly_core.supervisor.service import (
    ServiceResult,
    install_service,
    service_status,
    uninstall_service,
)
from nightly_core.supervisor.trigger import (
    SUPERVISOR_ABORTED_FILENAME,
    RespawnVerdict,
    should_respawn,
)

__all__ = [
    "SUPERVISOR_ABORTED_FILENAME",
    "PollResult",
    "RespawnOutcome",
    "RespawnVerdict",
    "ServiceResult",
    "SupervisorState",
    "WatchedRepo",
    "already_running",
    "forget_repo",
    "install_service",
    "list_repos",
    "main",
    "poll_once",
    "prune_repos",
    "read_state",
    "register_repo",
    "respawn_host",
    "run_forever",
    "service_status",
    "should_respawn",
    "uninstall_service",
]
