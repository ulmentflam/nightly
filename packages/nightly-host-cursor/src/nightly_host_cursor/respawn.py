"""Respawn launcher for Cursor — not yet implemented (RFC 010 v2).

The supervisor (RFC 010 Phase B) ships a working launcher for Claude
Code only. This stub exists so the daemon can dispatch by host id and
fail *loudly and specifically* rather than silently doing nothing: an
operator who sets `supervisor.host: cursor` deserves to learn on the
first respawn attempt, not to discover months later that their
overnight runs were never being resumed.

What blocks v2 here:
Cursor's `stop` hook fires with different semantics (it auto-continues via `followup_message`, capped by `loop_limit`), and its Background Agents run in a cloud VM this daemon cannot reach. A respawn would have to drive the local IDE, which has no documented headless entry point.
"""

from __future__ import annotations

from pathlib import Path

from nightly_core.supervisor.respawn import RespawnOutcome

__all__ = ["respawn"]

HOST_ID = "cursor"


def respawn(repo: Path, **_kwargs: object) -> RespawnOutcome:
    """Raise — this host has no respawn launcher yet.

    Signature matches `nightly_core.supervisor.respawn.respawn_host` so
    v2 can drop a real implementation in without touching the daemon.
    """
    msg = (
        f"respawn launcher for host {HOST_ID!r} is not implemented (RFC 010 v2). "
        "v1 supports Claude Code only. Set `supervisor.enabled: false` in "
        ".nightly/config.yml to silence the supervisor for this repo, or run "
        "`nightly supervisor uninstall`."
    )
    raise NotImplementedError(msg)
