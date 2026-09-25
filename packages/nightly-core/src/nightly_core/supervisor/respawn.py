"""Re-invoking the host after an involuntary death.

The awkward part of this problem: `/nightly` is an *interactive* session,
and the supervisor is a headless daemon. We cannot simply
`subprocess.Popen(["claude"])` — Claude Code's interactive mode wants a
TTY, and a detached process has none. Nor do we want the headless
`claude -p` path: that drives one prompt to completion and exits, which
is `nightly run`, not a resumed interactive session with a live Stop hook
keeping it alive.

So the launcher's job is to get a *terminal* to run the command. Three
strategies, tried in order, each with a real failure mode:

1. **tmux** — if the operator named a session in config, or one is
   already running, send the command to a new window there. Best option
   by far: survives disconnect, scrollback is inspectable in the morning,
   and it works identically over SSH.
2. **macOS Terminal via `osascript`** — opens a window and types the
   command. Requires an active GUI login session; fails on a locked
   screen in some configurations, and needs Automation permission.
3. **Headless fallback** — `claude -p "/nightly"`. Not a real interactive
   resume, but it drives the cascade forward rather than leaving the
   night dead, and it is the only option that works with no terminal at
   all. Logged distinctly so the morning briefing does not claim a full
   resume happened.

v1 implements Claude Code. Other hosts get an explicit
`NotImplementedError` naming this RFC rather than a silent no-op, so a
Codex operator who installs the supervisor learns immediately.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "SUPPORTED_HOSTS",
    "RespawnOutcome",
    "Runner",
    "respawn_host",
]

_log = logging.getLogger(__name__)

SUPPORTED_HOSTS = ("claude",)
"""Hosts with a working respawn launcher. Everything else raises."""

_SLASH_COMMAND = "/nightly"
_SPAWN_TIMEOUT = 30


class Runner(Protocol):
    """How the launcher shells out. Injectable so tests never spawn terminals.

    `argv` is positional-only so implementations may name it whatever they
    like — a Protocol otherwise matches by parameter name."""

    def __call__(
        self, argv: list[str], /, cwd: str | None = None
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class RespawnOutcome:
    """What the launcher managed to do."""

    ok: bool
    strategy: str
    """`tmux`, `osascript`, `headless`, or `failed`."""

    detail: str = ""

    @property
    def is_full_resume(self) -> bool:
        """True when a real interactive session came back.

        The headless fallback drives the cascade but has no Stop hook
        keeping it alive, so it is one turn, not a resumed night. Callers
        report it differently for that reason."""
        return self.ok and self.strategy in ("tmux", "osascript")


def respawn_host(
    repo: Path,
    *,
    host: str = "claude",
    tmux_session: str | None = None,
    runner: Runner | None = None,
) -> RespawnOutcome:
    """Re-invoke `host` in `repo` so it picks the cascade back up.

    `runner` is injectable for tests — a callable taking an argv list and
    returning a `CompletedProcess`-alike. Production leaves it None.
    """
    if host not in SUPPORTED_HOSTS:
        msg = (
            f"respawn launcher for host {host!r} is not implemented — "
            "v1 supports Claude Code only (RFC 010 §Non-goals). "
            "Set `supervisor.enabled: false` for this repo, or run "
            "`nightly supervisor uninstall`."
        )
        raise NotImplementedError(msg)

    run: Runner = runner or _default_runner

    session = tmux_session or os.environ.get("NIGHTLY_TMUX_SESSION")
    if outcome := _try_tmux(repo, session=session, run=run):
        return outcome
    if outcome := _try_osascript(repo, run=run):
        return outcome
    return _try_headless(repo, run=run)


def _default_runner(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_SPAWN_TIMEOUT,
        check=False,
        cwd=cwd,
    )


def _command_for(repo: Path) -> str:
    """The shell line that starts a fresh Nightly session in `repo`.

    `--permission-mode acceptEdits` matters: a respawned overnight
    session has nobody to answer permission prompts, so without it the
    resumed agent stalls on its first edit — which is exactly the
    stranded state the supervisor exists to prevent."""
    return (
        f"cd {_quote(str(repo))} && claude --permission-mode acceptEdits {_quote(_SLASH_COMMAND)}"
    )


def _quote(text: str) -> str:
    import shlex  # noqa: PLC0415

    return shlex.quote(text)


def _try_tmux(
    repo: Path,
    *,
    session: str | None,
    run: Runner,
) -> RespawnOutcome | None:
    """Open a new tmux window running the resume command. None if unavailable."""
    if shutil.which("tmux") is None:
        return None

    target = session or "nightly"
    try:
        # `new-session -A` attaches if it exists, creates if it doesn't —
        # one call covers both, and avoids a has-session/new-session race.
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
    return RespawnOutcome(True, "tmux", f"tmux session {target!r} in {repo}")


def _try_osascript(repo: Path, *, run: Runner) -> RespawnOutcome | None:
    """Open a macOS Terminal window running the resume command."""
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
    return RespawnOutcome(True, "osascript", f"Terminal window in {repo}")


def _applescript_str(text: str) -> str:
    """Quote a Python string as an AppleScript literal."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _try_headless(repo: Path, *, run: Runner) -> RespawnOutcome:
    """Last resort: drive one headless turn so the night isn't wasted.

    Deliberately not a full resume — there is no Stop hook holding this
    session open, so it runs one cascade step and exits. Better than
    nothing, and honestly labelled so the briefing doesn't overclaim.
    """
    if shutil.which("claude") is None:
        return RespawnOutcome(False, "failed", "`claude` not on PATH")
    try:
        completed = run(
            ["claude", "-p", _SLASH_COMMAND, "--permission-mode", "acceptEdits"],
            cwd=str(repo),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RespawnOutcome(False, "failed", f"headless spawn failed: {exc!r}")

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        return RespawnOutcome(False, "failed", f"headless claude exited non-zero: {stderr[:200]}")
    return RespawnOutcome(True, "headless", "one headless turn (no live Stop hook)")
