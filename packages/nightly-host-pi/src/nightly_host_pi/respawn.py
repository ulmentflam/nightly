"""Respawn launcher for pi — the first host where respawn is a true resume.

The Claude launcher restarts a session by invoking `/nightly` fresh: the
cascade picks up where it left off, but the conversation does not. pi has
`-c`, which continues the most recent session for the cwd — so a respawned
pi comes back with its actual context, not a reconstruction of it.

Same three-strategy ladder as the Claude launcher, and for the same
reason: a detached daemon has no TTY, so something has to supply a
terminal. tmux leads because it survives disconnect and leaves scrollback
the operator can read in the morning.

The supervisor matters less for pi than for Claude Code, and the docs say
so plainly: pi has no consecutive-block cap, so the failure mode that
motivated RFC 010 does not exist here. Crashes, OOM, and disconnects
still do — that is what this covers.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from nightly_core.supervisor.respawn import RespawnOutcome, Runner

__all__ = ["HOST_ID", "respawn"]

_log = logging.getLogger(__name__)

HOST_ID = "pi"

_SPAWN_TIMEOUT = 30


def _default_runner(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_SPAWN_TIMEOUT,
        check=False,
        cwd=cwd,
    )


def _quote(text: str) -> str:
    import shlex  # noqa: PLC0415

    return shlex.quote(text)


def _command_for(repo: Path) -> str:
    """Shell line that resumes the most recent pi session in `repo`.

    `-c` continues rather than starting fresh; `-a` approves project trust
    for the run, without which a respawned session silently loses the
    project's skills and settings.
    """
    return f"cd {_quote(str(repo))} && pi -c -a"


def _applescript_str(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def respawn(
    repo: Path,
    *,
    host: str = HOST_ID,
    tmux_session: str | None = None,
    runner: Runner | None = None,
) -> RespawnOutcome:
    """Re-invoke pi in `repo`, resuming its most recent session."""
    if host != HOST_ID:
        msg = f"pi respawn launcher called for host {host!r}"
        raise NotImplementedError(msg)

    run: Runner = runner or _default_runner
    session = tmux_session or os.environ.get("NIGHTLY_TMUX_SESSION")

    if (outcome := _try_tmux(repo, session=session, run=run)) is not None:
        return outcome
    if (outcome := _try_osascript(repo, run=run)) is not None:
        return outcome
    return _try_headless(repo, run=run)


def _try_tmux(repo: Path, *, session: str | None, run: Runner) -> RespawnOutcome | None:
    if shutil.which("tmux") is None:
        return None
    target = session or "nightly"
    try:
        completed = run(
            [
                "tmux",
                "new-session",
                "-d",
                "-A",
                "-s",
                target,
                "-c",
                str(repo),
                _command_for(repo),
            ]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("tmux respawn failed: %s", exc)
        return None
    if completed.returncode != 0:
        _log.debug("tmux respawn non-zero: %s", completed.stderr)
        return None
    return RespawnOutcome(True, "tmux", f"pi resumed in tmux session {target!r} ({repo})")


def _try_osascript(repo: Path, *, run: Runner) -> RespawnOutcome | None:
    if shutil.which("osascript") is None:
        return None
    script = f'tell application "Terminal" to do script {_applescript_str(_command_for(repo))}'
    try:
        completed = run(["osascript", "-e", script])
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("osascript respawn failed: %s", exc)
        return None
    if completed.returncode != 0:
        _log.debug("osascript respawn non-zero: %s", completed.stderr)
        return None
    return RespawnOutcome(True, "osascript", f"pi resumed in a Terminal window ({repo})")


def _try_headless(repo: Path, *, run: Runner) -> RespawnOutcome:
    """Last resort: one headless turn against the Nightly skill.

    Not a resume — `-p` runs a single prompt to completion with no
    extension keep-alive holding the session open. Labelled `headless` so
    `RespawnOutcome.is_full_resume` reports False and the briefing does
    not claim the night was recovered.
    """
    if shutil.which("pi") is None:
        return RespawnOutcome(False, "failed", "`pi` not on PATH")
    try:
        completed = run(
            ["pi", "-p", "--mode", "json", "-a", "/skill:nightly"],
            cwd=str(repo),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RespawnOutcome(False, "failed", f"headless spawn failed: {exc!r}")
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        return RespawnOutcome(False, "failed", f"headless pi exited non-zero: {stderr[:200]}")
    return RespawnOutcome(True, "headless", "one headless pi turn (no live keep-alive)")
