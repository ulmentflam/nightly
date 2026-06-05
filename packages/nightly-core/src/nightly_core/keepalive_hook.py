"""Claude Code `Stop` hook glue — keep the interactive session alive.

Claude Code emits a Stop event at the end of every turn — the model's
"stay alive" intent is overridden by the turn boundary. The Stop hook
is the host-level lever: when Claude tries to end a turn, the hook can
return:

    {"decision": "block", "reason": "<prompt>"}

…which forces Claude to keep going with `reason` as the next user message.
This module computes that decision based on disk state.

Off-ramps (v0.0.3+): **only human intervention terminates a session.**

- `.nightly/runs/<id>/CONCLUDE` exists — `nightly conclude` requested
  graceful drain (the human ran the command).
- `.nightly/runs/<id>/STOP` exists — `nightly stop` requested immediate
  hard stop (the human ran the command).

Plus three preconditions that aren't voluntary releases — they reflect
"there is nothing to keep alive" or "the host is overriding us
regardless":

- The session was never `nightly session start`ed (`SESSION_ACTIVE`
  marker absent) — non-Nightly sessions are untouched.
- No active run (`.nightly/runs/CURRENT` missing) — `no_run`.
- Claude Code's own 9-consecutive-block safety: the host calls our
  hook with `stop_hook_active=True` to signal "I'm about to override
  you regardless." We bow out cleanly per the docs. The clever bypass
  for this is a respawn supervisor that watches for the cap and starts
  a fresh host session — see RFC 010 (planned).

Removed in v0.0.3 (per the operator's "the only termination should be
human intervention" directive):

- `stale` — the 4-hour SESSION_ACTIVE freshness check is gone. A
  marker that survived from earlier today can still force-continue.
- `max_turns` — the 500-turn safety cap on force-continues is gone.
  The turn counter is still incremented for telemetry but no longer
  gates termination.
- `cascade_loop` — repeated cascade picks no longer release. The
  history file is still written for post-mortem diagnostics.
- `pr_backlog` — the `MAX_OPEN_PRS=5` cap was removed in the same
  v0.0.3 cut; the replacement is skill-side consolidation (Rule 11).

The hook still unconditionally appends a one-line heartbeat to
`.nightly/runs/<id>/keepalive.log` so post-mortems can see exactly
when each turn boundary fired and why the hook allowed (or blocked)
stop. Audit trail is non-negotiable when a hook is overriding the
model's intent.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nightly_core.cascade import CascadeChoice, next_task
from nightly_core.runs import current_run

__all__ = [
    "HOOK_FORMATS",
    "LOOP_HISTORY_FILENAME",
    "SESSION_ACTIVE_FILENAME",
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

LOOP_HISTORY_FILENAME = "keepalive.history"
"""Per-run file of cascade-pick fingerprints, newline-delimited. Trimmed
to the last `_LOOP_HISTORY_KEEP` lines so the file never grows unbounded.
Lives next to keepalive.log; both are audit artifacts, neither is on the
hot path.

The history file is still written in v0.0.3+ for post-mortem diagnostics
(operator looking at why a cascade kept returning the same pick), but
no longer triggers a `cascade_loop` release — the contract is "always
advance," so a stuck cascade is the operator's signal to investigate,
not the hook's signal to yield."""

_LOOP_HISTORY_KEEP = 7
"""How many lines of history to retain on disk. 7 ≈ "enough context for
a post-mortem without unbounded growth"; the constant used to derive
from the now-retired LOOP_THRESHOLD so we hard-code a similar value."""


@dataclass(frozen=True)
class StopHookDecision:
    """What the Stop hook is going to do, and why.

    `payload` is the literal JSON dict Claude Code expects on stdout
    (`{"decision": "block", "reason": ...}` to force continue, or `{}`
    to allow the stop). `reason_code` is a stable short slug for logs:
    `host_cap`, `no_run`, `inactive`, `stale`, `conclude`, `stop`,
    `max_turns`, `force_continue`. (`pr_backlog` is retired as of
    v0.0.3 — the PR-count cap was removed in favor of skill-side
    consolidation guidance per Rule 11.)
    `message` is a one-line human-readable explanation suitable for
    `keepalive.log`.
    """

    payload: dict[str, Any]
    reason_code: str
    message: str

    @property
    def should_block(self) -> bool:
        return self.payload.get("decision") == "block"


def compute_stop_hook_decision(
    root: Path | None = None,
    *,
    now: datetime | None = None,
    stop_hook_active: bool = False,
) -> StopHookDecision:
    """Decide whether to block-and-continue or allow the current stop.

    `stop_hook_active=True` is Claude Code's signal that *this very hook*
    has blocked the same turn boundary 9 consecutive times and the host
    is about to override us regardless. Bowing out cleanly (emit `{}`)
    is the documented protocol — see
    https://docs.anthropic.com/en/docs/claude-code/hooks#json-input.
    Past failure: not honoring this caused Claude Code to log
    "A hook blocked the turn from ending 9 consecutive times — overriding"
    in a real session. We still record a heartbeat so operators can see
    *why* we yielded.
    """
    del now  # no longer used — the staleness check that consumed `now` was retired in v0.0.3
    if stop_hook_active:
        return StopHookDecision(
            payload={},
            reason_code="host_cap",
            message=(
                "host signaled stop_hook_active — yielding to the host's "
                "consecutive-block cap; will not force-continue this turn."
            ),
        )
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

    # v0.0.3: every automatic off-ramp other than host-level overrides
    # has been removed per the operator's "the only termination should
    # be human intervention" directive. The 4h SESSION_ACTIVE staleness
    # check, the 500-turn MAX_TURNS safety cap, the LOOP_THRESHOLD
    # cascade-loop guard, and the MAX_OPEN_PRS PR-backlog cap are all
    # gone. Only `conclude` / `stop` markers (human-placed) and
    # `host_cap` / `no_run` / `inactive` (host- or precondition-level,
    # not voluntary releases) can end the session now. The turn counter
    # is still incremented for telemetry but no longer gates termination.
    turn_count = _bump_and_read_turn_count(run.path)

    # The cascade pick is still computed and recorded in the loop
    # history file (`keepalive.history`) for post-mortem diagnostics,
    # but a repeated fingerprint no longer releases the session — the
    # contract is "always advance," so a stuck cascade is the
    # operator's signal to investigate, not the hook's signal to
    # yield.
    try:
        choice = next_task(root)
    except Exception as exc:  # cascade must never crash the hook
        choice = None
        cascade_error: Exception | None = exc
    else:
        cascade_error = None

    if choice is not None:
        # Record for diagnostics; result is intentionally unused.
        _record_and_count_repeats(run.path, _cascade_fingerprint(choice))

    reason = _build_continue_reason_from(
        choice=choice,
        cascade_error=cascade_error,
        run_id=run.id,
        turn=turn_count,
    )
    return StopHookDecision(
        payload={"decision": "block", "reason": reason},
        reason_code="force_continue",
        message=(
            f"run {run.id} turn {turn_count}: blocking stop and injecting continuation prompt."
        ),
    )


def _cascade_fingerprint(choice: CascadeChoice) -> str:
    """Stable identity of a cascade pick — used by the loop guard.

    `source + target_path + summary` captures "the same recommendation"
    closely enough: target_path discriminates between different in-flight
    plans, summary discriminates between different proposals at the same
    cascade source. We don't include `rationale` because it can vary
    turn-to-turn (e.g. proposer scores fluctuate) without the underlying
    work changing — false positives in the loop guard would mask real
    progress.
    """
    target = str(choice.target_path) if choice.target_path is not None else "-"
    return f"{choice.source}|{target}|{choice.summary}"


def _record_and_count_repeats(run_path: Path, fingerprint: str) -> int:
    """Append `fingerprint` to the run's history file, return its current
    consecutive-repeat count (including the new entry).

    The history is trimmed to the last `_LOOP_HISTORY_KEEP` entries so the
    file can't grow unbounded across long-running sessions. Failure to
    persist is non-fatal — the hook always returns a decision.
    """
    history_path = run_path / LOOP_HISTORY_FILENAME
    try:
        prior = (
            history_path.read_text(encoding="utf-8").splitlines() if history_path.is_file() else []
        )
    except OSError:
        prior = []

    new_history = [*prior, fingerprint][-_LOOP_HISTORY_KEEP:]
    with contextlib.suppress(OSError):
        history_path.write_text("\n".join(new_history) + "\n", encoding="utf-8")

    # Count how many trailing entries match `fingerprint` (including the
    # one we just appended).
    repeats = 0
    for entry in reversed(new_history):
        if entry == fingerprint:
            repeats += 1
        else:
            break
    return repeats


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


def _build_continue_reason_from(
    *,
    choice: CascadeChoice | None,
    cascade_error: Exception | None,
    run_id: str,
    turn: int,
) -> str:
    """Build the `continue on X` prompt from an already-resolved cascade pick.

    Factored out of `_build_continue_reason` so `compute_stop_hook_decision`
    can compute the cascade choice once (for the loop guard) and reuse it
    here. Same fallback behavior: if `cascade_error` is set or `choice` is
    None, emit a generic nudge instead of a "continue on X" prompt.
    """
    if cascade_error is not None:
        # Cascade raised — the hook should still keep the session moving
        # with a generic nudge. Never crash, never block forever.
        return (
            f"[Nightly keepalive · run {run_id} · turn {turn}] "
            f"The cascade raised {type(cascade_error).__name__}: {cascade_error}. "
            "Continue per the AGENTS.md / CLAUDE.md autonomy contract: "
            "investigate the cascade error first, then resume work."
        )
    if choice is None:  # defensive — equivalent to a "nothing" branch
        return (
            f"[Nightly keepalive · run {run_id} · turn {turn}] "
            "The cascade returned no pick. Run `nightly next` and execute "
            "whatever it surfaces."
        )

    header = f"[Nightly keepalive · run {run_id} · turn {turn}]"

    if choice.source == "nothing":
        # The cascade's `rationale` distinguishes the three reasons the
        # cascade can return `nothing` (proposers empty / all deduped /
        # session disarmed) — pre-Issue-#11 the hook hardcoded "the
        # proposer suite is empty" which was misleading whenever the
        # dedupe filter caught every proposal. Surface the rationale
        # verbatim so the agent sees the actual cause.
        rationale = choice.rationale or "The cascade returned `nothing`."
        return (
            f"{header}\n"
            f"{rationale}\n"
            "Do NOT render the briefing and exit. Make a recommendation "
            "right now and execute it:\n"
            "  1. Pick the most consequential open question you can name "
            "from `.planning/`, README.md, AGENTS.md, or a recent "
            "uncertainty.md. Prefer sources the proposers don't cover.\n"
            "  2. Scope it as a new task with `nightly task <slug>`.\n"
            "  3. Start executing — do not write a plan first, do not ask, "
            "do not park.\n"
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

    Idempotent — re-touching just refreshes the mtime. The marker has
    no TTL in v0.0.3+ (the 4h staleness check was removed); the mtime
    is preserved only for post-mortem audits.
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
