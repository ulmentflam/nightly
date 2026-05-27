"""Claude Code `Stop` hook glue — keep the interactive session alive.

Loop guard (cascade_loop):
The keepalive log is an append-only audit trail, but the hook also writes
a per-run `keepalive.history` file containing the last N cascade-pick
fingerprints. When the same fingerprint shows up `LOOP_THRESHOLD` times
consecutively, the hook treats the cascade as wedged (e.g. proposer
re-detects the same source signal because nothing landed on `main`) and
yields with `reason_code="cascade_loop"`. Without this guard, the
operator's only signal that the cascade is looping is the host's own
9-consecutive-block override — which silences the model mid-task and
leaves no clean audit marker. See `.planning/issues/2` for the failure
mode that motivated this.

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
- The repo has `MAX_OPEN_PRS` or more open `nightly/*` PRs and the next
  cascade pick isn't resume-priority — operator review throughput is
  the bottleneck, so producing PR N+1 is anti-helpful. Resume-priority
  picks (in-flight task, unblocked approval, PR rescue with blocking
  feedback) override the backpressure so already-shipped work still
  gets finished.
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

from nightly_core.cascade import CascadeChoice, count_open_nightly_prs, next_task
from nightly_core.runs import current_run

__all__ = [
    "HOOK_FORMATS",
    "LOOP_HISTORY_FILENAME",
    "LOOP_THRESHOLD",
    "MAX_OPEN_PRS",
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

LOOP_THRESHOLD = 3
"""Yield with `cascade_loop` when the cascade has returned the same pick
this many times in a row. 3 = "twice was a coincidence, three times is a
loop." Configurable enough to raise if a real workflow legitimately
re-dispatches the same plan three turns in a row (rare — most rescues
mutate the plan body, which changes the summary)."""

LOOP_HISTORY_FILENAME = "keepalive.history"
"""Per-run file of cascade-pick fingerprints, newline-delimited. Trimmed
to the last LOOP_THRESHOLD + 4 lines so the file never grows unbounded.
Lives next to keepalive.log; both are audit artifacts, neither is on the
hot path."""

_LOOP_HISTORY_KEEP = LOOP_THRESHOLD + 4
"""How many lines of history to retain on disk. Keep a few extra over
the threshold so a post-mortem can see the prior context."""


MAX_OPEN_PRS = 5
"""Operator-review-throughput cap: when the repo has this many open `nightly/*`
PRs, the Stop hook treats human review as the bottleneck and allows the
session to end at the next turn boundary — unless the cascade still has
resume-priority work (see ``_BACKLOG_OVERRIDE_SOURCES``).

The agent never reads this constant; it's a host-level backpressure signal
the hook exercises on its own, so the no-self-conclude rule stays intact.
"""

_BACKLOG_OVERRIDE_SOURCES: frozenset[str] = frozenset(
    {
        "resume_in_flight",
        "unblocked_approval",
    }
)
"""Cascade sources that keep force-continuing even when PR backlog is at cap.

`pr_rescue` is *conditionally* an override — only when the rescue feedback is
blocking (failed CI, CHANGES_REQUESTED review). That decision is made in
``_pr_rescue_is_blocking`` rather than the static set, because the cascade
choice carries the feedback details we need to consult.
"""


def _pr_rescue_is_blocking(choice: CascadeChoice) -> bool:
    """True iff this is a `pr_rescue` pick with at least one blocking item."""
    if choice.source != "pr_rescue" or not choice.pr_feedback:
        return False
    return any(f.is_blocking for f in choice.pr_feedback)


@dataclass(frozen=True)
class StopHookDecision:
    """What the Stop hook is going to do, and why.

    `payload` is the literal JSON dict Claude Code expects on stdout
    (`{"decision": "block", "reason": ...}` to force continue, or `{}`
    to allow the stop). `reason_code` is a stable short slug for logs:
    `host_cap`, `no_run`, `inactive`, `stale`, `conclude`, `stop`,
    `pr_backlog`, `max_turns`, `force_continue`.
    `message` is a one-line human-readable explanation suitable for
    `keepalive.log`.
    """

    payload: dict[str, Any]
    reason_code: str
    message: str

    @property
    def should_block(self) -> bool:
        return self.payload.get("decision") == "block"


def compute_stop_hook_decision(  # noqa: PLR0911, PLR0912 — one return per off-ramp is the whole point
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
    if stop_hook_active:
        return StopHookDecision(
            payload={},
            reason_code="host_cap",
            message=(
                "host signaled stop_hook_active — yielding to the host's "
                "consecutive-block cap; will not force-continue this turn."
            ),
        )
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

    # PR-backlog backpressure. When the operator already has MAX_OPEN_PRS
    # or more Nightly-authored PRs awaiting review, human review throughput
    # is the bottleneck — producing another paperwork PR makes the queue
    # worse, not better. We compute the cascade choice once and check it
    # against the override set so resume-priority work (in-flight task,
    # unblocked approval, blocking PR rescue) still keeps the session
    # moving. Anything else (accepted_rfc, github_issue, ideate,
    # ideate_fallback, nothing) lets us release. Past failure: 5 stacked
    # paperwork PRs on top of an unblock PR while the operator slept.
    open_prs = count_open_nightly_prs(root)
    if open_prs >= MAX_OPEN_PRS:
        try:
            choice = next_task(root)
        except Exception:  # cascade must never crash the hook
            choice = None
        is_override = choice is not None and (
            choice.source in _BACKLOG_OVERRIDE_SOURCES or _pr_rescue_is_blocking(choice)
        )
        if not is_override:
            source = choice.source if choice is not None else "unknown"
            return StopHookDecision(
                payload={},
                reason_code="pr_backlog",
                message=(
                    f"run {run.id}: {open_prs} open Nightly PR(s) awaiting "
                    f"review (cap {MAX_OPEN_PRS}); next cascade pick "
                    f"`{source}` is not resume-priority — allowing stop "
                    "(operator review is the bottleneck)."
                ),
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

    # Compute the cascade pick once and reuse it for both the loop guard
    # and the continuation prompt — `next_task` walks plans + proposers,
    # not a cost we want to pay twice per hook firing.
    try:
        choice = next_task(root)
    except Exception as exc:  # cascade must never crash the hook
        choice = None
        cascade_error: Exception | None = exc
    else:
        cascade_error = None

    # Loop guard: if the cascade has surfaced the same pick LOOP_THRESHOLD
    # times in a row, the proposer suite has fallen into a re-detect /
    # re-dispatch cycle the model can't break by force-continuing. Yield
    # so the operator sees `decision=cascade_loop` instead of hitting the
    # host's 9-consecutive-block override mid-task.
    if choice is not None:
        fingerprint = _cascade_fingerprint(choice)
        repeats = _record_and_count_repeats(run.path, fingerprint)
        if repeats >= LOOP_THRESHOLD:
            return StopHookDecision(
                payload={},
                reason_code="cascade_loop",
                message=(
                    f"run {run.id}: cascade pick "
                    f"`{choice.source}/{choice.summary[:60]}` has repeated "
                    f"{repeats} turns running — yielding to break the loop "
                    "(landed local proposals are re-detected by the "
                    "proposer suite; see issue #2)."
                ),
            )

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
            f"run {run.id} turn {turn_count}/{MAX_TURNS}: blocking stop and "
            "injecting continuation prompt."
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
