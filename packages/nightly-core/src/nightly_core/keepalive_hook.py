"""Claude Code `Stop` hook glue — keep the interactive session alive.

Claude Code stopped my Nightly session last night despite the AGENTS.md /
CLAUDE.md "never stop" rule (see the screenshot in PR review). The rules
text alone isn't sufficient because Claude Code emits a Stop event at the
end of every turn — the model's "stay alive" intent is overridden by the
turn boundary. The Stop hook is the host-level lever: when Claude tries
to end a turn, the hook can return:

    {"decision": "block", "reason": "<prompt>"}

…which forces Claude to keep going with `reason` as the next user message.
This module computes that decision based on disk state.

Off-ramps (any one of these lets the session stop):

- The session was never `nightly session start`ed (`SESSION_ACTIVE`
  marker absent) — non-Nightly sessions are untouched.
- The `SESSION_ACTIVE` marker is older than `SESSION_TTL_SECONDS`
  (default 4h) — handles stale markers from yesterday.
- `.nightly/runs/<id>/CONCLUDE` exists — `nightly conclude` requested
  graceful drain.
- `.nightly/runs/<id>/STOP` exists — `nightly stop` requested immediate
  hard stop.
- Turn count has exceeded `MAX_TURNS` (default 500) — safety cap so a
  runaway loop can't burn through the credentialed account forever.

The hook also unconditionally appends a one-line heartbeat to
`.nightly/runs/<id>/keepalive.log` so post-mortems can see exactly
when each turn boundary fired and why the hook allowed (or blocked)
stop. Audit trail is non-negotiable when a hook is overriding the
model's intent.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nightly_core.cascade import next_task
from nightly_core.runs import current_run

__all__ = [
    "HOOK_FORMATS",
    "MAX_TURNS",
    "SESSION_ACTIVE_FILENAME",
    "SESSION_TTL_SECONDS",
    "STOP_FILENAME",
    "HookFormat",
    "StopHookDecision",
    "arm_session",
    "compute_stop_hook_decision",
    "disarm_session",
    "format_decision",
    "log_heartbeat",
    "request_stop",
]


# ── wire formats ──────────────────────────────────────────────────────────


# Four wire shapes for the same conceptual "force-continue" decision, one
# per host family that exposes a Stop-equivalent hook:
#
# - claude_code: `{"decision":"block","reason":"..."}` — Claude Code & Codex.
#   Same JSON shape across both; only the host's settings file location
#   differs (.claude/settings.local.json vs .codex/hooks.json).
# - cursor: `{"followup_message":"..."}` — Cursor 1.7+. Auto-continues
#   if the field is set; capped by `loop_limit` (default 5).
# - gemini_cli: `{"decision":"deny","reason":"..."}` — Gemini CLI &
#   Antigravity. The `AfterAgent` hook fires per turn; `deny` triggers a
#   retry with the reason text fed back as the next user prompt.
# - empty: `{}` — any host that doesn't honor `{}` as allow-stop just
#   needs to not have a hook installed; this is the default for opencode.
HookFormat = str  # Literal narrowing avoided to keep typer happy
HOOK_FORMATS: tuple[str, ...] = ("claude_code", "cursor", "gemini_cli")


SESSION_ACTIVE_FILENAME = "SESSION_ACTIVE"
"""Marker file under the run dir. Presence = Nightly armed this session."""

STOP_FILENAME = "STOP"
"""Marker file under the run dir. Presence = immediate hard stop requested."""

SESSION_TTL_SECONDS = 4 * 60 * 60
"""SESSION_ACTIVE markers older than this are treated as stale (4h)."""

MAX_TURNS = 500
"""Safety cap on how many times the Stop hook will force-continue per run."""


@dataclass(frozen=True)
class StopHookDecision:
    """What the Stop hook is going to do, and why.

    `payload` is the literal JSON dict Claude Code expects on stdout
    (`{"decision": "block", "reason": ...}` to force continue, or `{}`
    to allow the stop). `reason_code` is a stable short slug for logs:
    `no_run`, `inactive`, `conclude`, `stop`, `max_turns`, `force_continue`.
    `message` is a one-line human-readable explanation suitable for
    `keepalive.log`.
    """

    payload: dict[str, Any]
    reason_code: str
    message: str

    @property
    def should_block(self) -> bool:
        return self.payload.get("decision") == "block"


def compute_stop_hook_decision(  # noqa: PLR0911 — one return per off-ramp is the whole point
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> StopHookDecision:
    """Decide whether to block-and-continue or allow the current stop."""
    moment = now or datetime.now(UTC)
    run = current_run(root)
    if run is None:
        return StopHookDecision(
            payload={},
            reason_code="no_run",
            message="no active run; allowing stop.",
        )

    if not (run.path / SESSION_ACTIVE_FILENAME).is_file():
        return StopHookDecision(
            payload={},
            reason_code="inactive",
            message=f"run {run.id} has no SESSION_ACTIVE marker; allowing stop.",
        )

    if _marker_is_stale(run.path / SESSION_ACTIVE_FILENAME, moment):
        return StopHookDecision(
            payload={},
            reason_code="stale",
            message=(
                f"run {run.id} SESSION_ACTIVE marker is older than "
                f"{SESSION_TTL_SECONDS // 3600}h; allowing stop."
            ),
        )

    if (run.path / "CONCLUDE").is_file():
        return StopHookDecision(
            payload={},
            reason_code="conclude",
            message=f"run {run.id} has CONCLUDE; allowing stop (graceful drain).",
        )

    if (run.path / STOP_FILENAME).is_file():
        return StopHookDecision(
            payload={},
            reason_code="stop",
            message=f"run {run.id} has STOP sentinel; allowing stop (hard).",
        )

    turn_count = _bump_and_read_turn_count(run.path)
    if turn_count >= MAX_TURNS:
        return StopHookDecision(
            payload={},
            reason_code="max_turns",
            message=(
                f"run {run.id} hit MAX_TURNS={MAX_TURNS}; allowing stop "
                "(safety cap — investigate runaway loop)."
            ),
        )

    reason = _build_continue_reason(root, run_id=run.id, turn=turn_count)
    return StopHookDecision(
        payload={"decision": "block", "reason": reason},
        reason_code="force_continue",
        message=(
            f"run {run.id} turn {turn_count}/{MAX_TURNS}: blocking stop and "
            "injecting continuation prompt."
        ),
    )


def _marker_is_stale(marker: Path, now: datetime) -> bool:
    try:
        age = now - datetime.fromtimestamp(marker.stat().st_mtime, tz=UTC)
    except OSError:
        return True
    return age > timedelta(seconds=SESSION_TTL_SECONDS)


_TURN_FILENAME = "keepalive.turns"


def _bump_and_read_turn_count(run_path: Path) -> int:
    """Atomic-ish counter for how many times the hook has force-continued."""
    counter = run_path / _TURN_FILENAME
    try:
        prior = int(counter.read_text(encoding="utf-8").strip()) if counter.is_file() else 0
    except (OSError, ValueError):
        prior = 0
    nxt = prior + 1
    # Best-effort; failure to persist isn't fatal — the hook still returns
    # a decision. We still log the heartbeat separately.
    with contextlib.suppress(OSError):
        counter.write_text(f"{nxt}\n", encoding="utf-8")
    return nxt


def _build_continue_reason(root: Path | None, *, run_id: str, turn: int) -> str:
    """Build the smart `continue on X` prompt the hook injects.

    Walks the cascade so the prompt names a concrete next step. Falls
    back to the keepalive doctrine when the cascade has no work.
    """
    try:
        choice = next_task(root)
    except Exception as exc:  # hook must never crash the session
        # Even if the cascade explodes (corrupt plan frontmatter, etc),
        # the hook should still keep the session moving with a generic
        # nudge — never crash, never block the session forever.
        return (
            f"[Nightly keepalive · run {run_id} · turn {turn}] "
            f"The cascade raised {type(exc).__name__}: {exc}. "
            "Continue per the AGENTS.md / CLAUDE.md autonomy contract: "
            "investigate the cascade error first, then resume work."
        )

    header = f"[Nightly keepalive · run {run_id} · turn {turn}]"

    if choice.source == "nothing":
        # The auto-ideate fallback should have caught this when the session
        # is armed — reaching this branch with an armed session means the
        # proposer suite returned literally zero candidates. Tell the model
        # to *make* a recommendation, not to deliberate.
        return (
            f"{header}\n"
            "The cascade returned `nothing` and the proposer suite is empty. "
            "Do NOT render the briefing and exit. Make a recommendation right "
            "now and execute it:\n"
            "  1. Pick the most consequential open question you can name from "
            ".planning/, README.md, AGENTS.md, or a recent uncertainty.md.\n"
            "  2. Scope it as a new task with `nightly task <slug>`.\n"
            "  3. Start executing — do not write a plan first, do not ask, do "
            "not park.\n"
            "If you can articulate a 'here's what I'd do', that IS the "
            "recommendation. Ship it."
        )

    target_hint = ""
    if choice.target_path is not None:
        target_hint = f"\nTarget: {choice.target_path}"

    return (
        f"{header}\n"
        f"Continue on: {choice.summary}\n"
        f"Cascade source: {choice.source}{target_hint}\n"
        f"{choice.rationale or ''}\n"
        "Pick this up where the previous turn left off. Do not ask for "
        "confirmation, do not deliberate, do not end the turn waiting for "
        "input. If you can name a recommendation, execute it — read the plan, "
        "advance it, commit, move on. The user is asleep."
    ).strip()


def format_decision(
    decision: StopHookDecision,
    *,
    fmt: HookFormat = "claude_code",
) -> dict[str, Any]:
    """Convert a `StopHookDecision` to the JSON shape `fmt` expects.

    `claude_code` (default) is the Claude Code / Codex CLI shape — these
    two share the same Stop-hook JSON exactly. `cursor` and `gemini_cli`
    each have their own quirks; see HOOK_FORMATS for the full taxonomy.

    The `{}` payload (allow stop) is universal — every host treats an
    empty JSON object as "no decision, let it stop." So that branch is
    a single line at the top.
    """
    if not decision.should_block:
        return {}
    reason = decision.payload.get("reason", "")
    if fmt == "cursor":
        return {"followup_message": reason}
    if fmt == "gemini_cli":
        return {"decision": "deny", "reason": reason}
    # claude_code default — same payload Claude Code and Codex emit.
    return {"decision": "block", "reason": reason}


# ── session lifecycle helpers ─────────────────────────────────────────────


def arm_session(root: Path | None = None, *, now: datetime | None = None) -> Path | None:
    """Touch SESSION_ACTIVE under the current run. Returns the marker path.

    Idempotent — re-touching just refreshes the mtime, extending the TTL
    by another `SESSION_TTL_SECONDS`. Returns None if no run is active
    (the caller should `nightly start` first).
    """
    run = current_run(root)
    if run is None:
        return None
    marker = run.path / SESSION_ACTIVE_FILENAME
    marker.write_text(
        (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ\n"),
        encoding="utf-8",
    )
    return marker


def disarm_session(root: Path | None = None) -> Path | None:
    """Remove SESSION_ACTIVE under the current run. Returns the marker path."""
    run = current_run(root)
    if run is None:
        return None
    marker = run.path / SESSION_ACTIVE_FILENAME
    if marker.is_file():
        marker.unlink()
    return marker


def request_stop(root: Path | None = None) -> Path | None:
    """Touch the STOP sentinel under the current run. Returns the marker path.

    Unlike `conclude`, this does not wait for the current task to drain
    — the next Stop hook firing will allow the model to end its turn
    cleanly. Used for "the human walked over and wants this off right now".
    """
    run = current_run(root)
    if run is None:
        return None
    marker = run.path / STOP_FILENAME
    marker.write_text(
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ\n"),
        encoding="utf-8",
    )
    return marker


# ── logging ───────────────────────────────────────────────────────────────


def log_heartbeat(
    decision: StopHookDecision,
    root: Path | None = None,
    *,
    hook_input: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Append a one-line audit entry to `keepalive.log` under the current run."""
    run = current_run(root)
    if run is None:
        return None
    log_path = run.path / "keepalive.log"
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id = (hook_input or {}).get("session_id") or "?"
    line = (
        f"{stamp}  decision={decision.reason_code:<16}  "
        f"session={session_id}  msg={decision.message}\n"
    )
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        return None
    return log_path


def parse_hook_input(raw: str) -> dict[str, Any]:
    """Best-effort parse of the JSON Claude Code pipes to the hook on stdin.

    Tolerates empty input (returns `{}`). Never raises — the hook must
    keep working even if Claude Code's hook contract drifts.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
