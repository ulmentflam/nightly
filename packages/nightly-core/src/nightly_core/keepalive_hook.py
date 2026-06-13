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

Plus two preconditions that aren't voluntary releases — they reflect
"there is nothing to keep alive":

- The session was never `nightly session start`ed (`SESSION_ACTIVE`
  marker absent) — non-Nightly sessions are untouched.
- No active run (`.nightly/runs/CURRENT` missing) — `no_run`.

`stop_hook_active` is NOT an off-ramp. The Claude Code hooks guide
(https://code.claude.com/docs/en/hooks-guide) defines the flag as
"true when Claude Code is already continuing as a result of a stop
hook" — i.e. *this turn boundary is part of a forced-continuation
chain we started*, not a warning that the host is about to override
us. The real host backstop is separate: Claude Code overrides a Stop
hook only after it blocks 8 times in a row **without progress**, and
that cap is raisable via the `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`
environment variable (the Claude host integration sets it high so an
overnight session never trips it). Because Nightly's contract is that
only human disk markers terminate a session, the hook keeps blocking
even when `stop_hook_active` is True — the marker checks above the
force-continue branch are the only off-ramps.

Preemptive respawn marker: while we are blocking inside a forced
chain (`stop_hook_active=True`), we write/refresh the
`RESPAWN_REQUESTED` marker *before* returning the block decision. If
the host's without-progress cap silently overrides our block, the
session dies with no further hook invocation — so the marker must
already be on disk for `nightly status` / the skill's respawn path to
resume. A fresh, user-driven turn boundary (`stop_hook_active=False`)
clears the marker: the chain reset, so any earlier preemptive marker
is stale. The daemon-driven re-invocation that makes respawn fully
automatic is RFC 010 (planned).

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
from nightly_core.config import load_context_config
from nightly_core.digest import write_digest
from nightly_core.runs import current_run

__all__ = [
    "BLOCKS_FILENAME",
    "CONTEXT_FILENAME",
    "HOOK_FORMATS",
    "LOOP_HISTORY_FILENAME",
    "RESPAWN_REQUESTED_FILENAME",
    "SESSION_ACTIVE_FILENAME",
    "STOP_FILENAME",
    "HookFormat",
    "StopHookDecision",
    "arm_session",
    "compute_stop_hook_decision",
    "context_diet_block",
    "disarm_session",
    "estimate_context_tokens",
    "format_decision",
    "log_heartbeat",
    "read_respawn_marker",
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

RESPAWN_REQUESTED_FILENAME = "RESPAWN_REQUESTED"
"""Marker file under the run dir. Written *preemptively* by
`compute_stop_hook_decision` whenever it blocks inside a forced-
continuation chain (`stop_hook_active=True`). We cannot observe the
host's without-progress override (8 consecutive blocks, raisable via
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`) — when it fires, the session dies
with no further hook invocation. So the marker is written *before*
returning the block decision: if the next turn boundary never arrives,
the marker is already on disk and signals "this session ended
involuntarily mid-chain (host override without progress, crash, or
kill); the cascade still had work" so a respawn supervisor (RFC 010,
planned) or the operator can pick up where we left off.

Contents are a single ISO-8601 timestamp + cascade-pick summary (one
line). The Claude skill reads this at session start: if the marker is
present, treat the new session as a continuation of the prior — walk
the cascade immediately, no fresh-session reset. `nightly status`
surfaces the marker so the operator can see at a glance that a respawn
is pending.

Cleared on:
- `nightly conclude` — the operator's explicit "we're done" signal.
- `nightly stop` — the operator's explicit hard stop.
- `nightly session start` — fresh-session re-arm.
- Any fresh, user-driven turn boundary (`stop_hook_active=False`) — the
  forced chain reset, so a preemptive marker from an earlier chain is
  stale and gets cleared inline.
- Manually via `rm .nightly/runs/<id>/RESPAWN_REQUESTED`."""

BLOCKS_FILENAME = "keepalive.blocks"
"""Per-run counter of consecutive forced-continuation blocks.

Incremented each time the hook blocks while `stop_hook_active=True`
(i.e. this turn boundary is part of a chain Nightly started); reset to
0 on the next fresh, user-driven boundary (`stop_hook_active=False`).
Lets a post-mortem read how deep a forced chain ran before the host's
without-progress cap (or an operator marker) ended it. Best-effort IO,
OSError-suppressed like the other counters — never gates a decision."""

CONTEXT_FILENAME = "keepalive.context"
"""Per-run file holding the latest context-size estimate (a bare integer
token count, or empty when the estimate was None). Written best-effort by
the hook every turn boundary so `nightly status` can surface "context: ~NK
tokens at last turn boundary" without re-parsing the transcript. Audit /
telemetry only — never gates a decision."""


LOOP_HISTORY_FILENAME = "keepalive.history"
"""Per-run file of cascade-pick fingerprints, newline-delimited. Trimmed
to the last `_LOOP_HISTORY_KEEP` lines so the file never grows unbounded.
Lives next to keepalive.log; both are audit artifacts, neither is on the
hot path.

The history file is still written for post-mortem diagnostics (operator
looking at why a cascade kept returning the same pick) and never
triggers a `cascade_loop` *release* — the v0.0.3 contract is "always
advance," and the old `cascade_loop` guard's flaw was that it released
the session. As of issue #27 the repeat count is no longer purely
diagnostic: it drives the *livelock reroute*. When a `github_issue` or
`accepted_rfc` pick repeats `_LIVELOCK_REPICKS` consecutive turn
boundaries, the force-continue branch swaps the normal "Continue on: X"
prompt for the planning-phase prompt (treat the pick as `nothing` and
plan new work). This is detection-reroutes-to-ideation, NOT a release:
the session keeps blocking, so only human disk markers still terminate
it. A stuck cascade thus self-heals into planning instead of needing
operator investigation."""

_LOOP_HISTORY_KEEP = 7
"""How many lines of history to retain on disk. 7 ≈ "enough context for
a post-mortem without unbounded growth"; the constant used to derive
from the now-retired LOOP_THRESHOLD so we hard-code a similar value."""


_LIVELOCK_REPICKS = 3
"""Consecutive identical cascade picks that trigger the issue-#27 livelock
reroute.

When a `github_issue` or `accepted_rfc` pick's fingerprint repeats this
many turn boundaries in a row, the force-continue branch stops injecting
the normal "Continue on: X" prompt and injects the planning-phase prompt
instead (treat the pick as `nothing`, plan new work). 3 is the smallest
count that is unambiguously "holding, not working": an actionable
`github_issue`/`accepted_rfc` pick converts into a task plan within one
turn, after which `resume_in_flight` outranks it and the fingerprint
changes — so a third identical re-pick means the agent never started the
work (almost certainly because an open PR already covers it, which the
triage in-flight guard should catch but may miss). Detection only — the
session never releases (the v0.0.3 "only human intervention terminates"
contract holds); the old `cascade_loop` guard's flaw was releasing, not
detecting."""


_LIVELOCK_REROUTE_SOURCES = frozenset({"github_issue", "accepted_rfc"})
"""Cascade sources eligible for the livelock reroute.

Only these two convert into a task plan within one turn, after which a
higher cascade rung (`resume_in_flight`) takes over and the fingerprint
changes — so a sustained identical re-pick means the agent is holding,
not working. The other long-lived sources are deliberately excluded:
`resume_in_flight` / `unblocked_approval` / `pr_rescue` legitimately
repeat across many turns of honest work (one PR can take many turns) and
must NOT reroute; `ideate` / `nothing` already lead to the planning
phase, so rerouting them would be redundant."""


# ── context-size measurement ──────────────────────────────────────────────


_TRANSCRIPT_TAIL_BYTES = 256 * 1024
"""How many bytes off the end of the transcript JSONL to scan. Transcripts
grow without bound over an overnight session — reading the whole file every
turn boundary would be wasteful and slow. The last assistant message (whose
`message.usage` carries the running context size) is always at the very end,
so a 256 KiB tail comfortably contains it even with a few large tool results
interleaved. If the relevant line happens to fall outside the window we
degrade to None — a best-effort estimate, never an exact API figure."""

_MAX_TRANSCRIPT_LINE_BYTES = 4 * 1024 * 1024
"""Safety cap on any single transcript line we attempt to JSON-parse. A
pathological multi-megabyte line (a huge pasted blob, a giant tool result)
is skipped rather than parsed — protects the hook from spending unbounded
time/memory on one line during what must be a fast turn-boundary check."""


def estimate_context_tokens(transcript_path: str | Path | None) -> int | None:
    """Best-effort estimate of the live session's current context size.

    Claude Code's transcript JSONL records each assistant message with an
    API `message.usage` object; the LAST such entry's usage approximates the
    current context window occupancy. We sum its four token fields:

        input_tokens + cache_creation_input_tokens
                     + cache_read_input_tokens + output_tokens

    This parses documented-but-unofficial transcript fields, so it degrades
    to None on any surprise: missing file, unreadable bytes, no usage object
    anywhere in the tail window, malformed JSON, or absent fields. Callers
    treat None as "couldn't measure" and skip budget steering.

    Only the last `_TRANSCRIPT_TAIL_BYTES` of the file are read (seek from
    the end); lines are scanned BACKWARDS so the most recent usage wins, and
    any single line larger than `_MAX_TRANSCRIPT_LINE_BYTES` is skipped for
    safety. A partial first line (the tail split mid-record) simply fails to
    parse and is ignored.
    """
    if transcript_path is None:
        return None
    path = Path(transcript_path)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    try:
        with path.open("rb") as fh:
            if size > _TRANSCRIPT_TAIL_BYTES:
                fh.seek(size - _TRANSCRIPT_TAIL_BYTES)
            tail = fh.read()
    except OSError:
        return None

    # Decode permissively — a multibyte char clipped at the seek boundary
    # must not abort the whole parse.
    text = tail.decode("utf-8", errors="replace")
    lines = text.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > _MAX_TRANSCRIPT_LINE_BYTES:
            continue
        usage = _extract_usage(stripped)
        if usage is not None:
            return usage
    return None


def _extract_usage(line: str) -> int | None:
    """Parse one transcript JSONL line; return its summed usage or None.

    Tolerates any non-conforming line (not JSON, not a dict, no
    `message.usage`, usage fields absent/non-numeric) by returning None so
    the caller keeps scanning backwards for an older assistant entry.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for field_name in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    ):
        value = usage.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
            seen = True
    return total if seen else None


def context_diet_block(estimate: int, budget: int) -> str:
    """Render the "context diet" nudge prepended over-budget continuations.

    `estimate` and `budget` are token counts; both are surfaced rounded to
    thousands (e.g. `~301K` / `256K`). The block states the SOFT nature of
    the budget up front — the agent must finish any delicate in-flight step
    before practicing hygiene — then lists the four hygiene levers (lean on
    the fresh on-disk digest, background heavy work, avoid re-reading large
    files / dumping long output, persist anything precious now). It ends
    with the non-negotiable: context size is never a stop condition.
    """
    est_k = round(estimate / 1000)
    budget_k = round(budget / 1000)
    return (
        f"⚠ CONTEXT BUDGET: ~{est_k}K tokens in context (soft budget "
        f"{budget_k}K). This is a soft limit — finish any delicate in-flight "
        "step first — then practice context hygiene: 1) the session digest is "
        "fresh at .nightly/runs/<id>/digest.md (key state survives compaction; "
        "the host re-injects it after any compaction); 2) dispatch heavy work "
        "to background specialists (`nightly dispatch start <slug> --role "
        "<role>`) — their context is separate from yours; 3) avoid re-reading "
        "large files or dumping long command output inline; 4) persist "
        "anything you'd hate to lose to the plan/digest now. Do not stop the "
        "session over context size.\n\n"
    )


@dataclass(frozen=True)
class StopHookDecision:
    """What the Stop hook is going to do, and why.

    `payload` is the literal JSON dict Claude Code expects on stdout
    (`{"decision": "block", "reason": ...}` to force continue, or `{}`
    to allow the stop). `reason_code` is a stable short slug for logs:
    `no_run`, `inactive`, `conclude`, `stop`, `force_continue`.
    (`host_cap` is retired — `stop_hook_active` is no longer a yield
    path; the hook keeps blocking through a forced chain and writes a
    preemptive RESPAWN_REQUESTED marker instead. `pr_backlog` is retired as of
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


def compute_stop_hook_decision(  # noqa: PLR0912 - one branch per off-ramp / routing outcome is the point
    root: Path | None = None,
    *,
    now: datetime | None = None,
    stop_hook_active: bool = False,
    transcript_path: str | Path | None = None,
) -> StopHookDecision:
    """Decide whether to block-and-continue or allow the current stop.

    `stop_hook_active` reflects the Claude Code hooks-guide field of the
    same name: True means "Claude Code is already continuing as a result
    of a stop hook" — i.e. this turn boundary is part of a forced-
    continuation chain Nightly started, NOT a warning that the host is
    about to override us. It is therefore not an off-ramp: the host's
    real backstop is a separate, raisable cap (Claude Code overrides a
    Stop hook only after 8 consecutive blocks *without progress*; raise
    it via the `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` env var, which the
    Claude host integration sets high for overnight runs).

    Decision order — off-ramps always win regardless of
    `stop_hook_active`: no run (`no_run`) → not armed (`inactive`) →
    CONCLUDE (`conclude`) → STOP (`stop`) → force-continue. We keep
    blocking through a forced chain because Nightly's contract is that
    only the human disk markers checked above terminate a session.

    Inside the force-continue branch, when `stop_hook_active` is True we
    write/refresh the RESPAWN_REQUESTED marker *before* returning the
    block decision: if the host's without-progress cap silently
    overrides this block, the session ends with no further hook
    invocation, so the resume marker must already be on disk. When
    `stop_hook_active` is False — a fresh, user-driven boundary — the
    chain has reset, so we clear any stale preemptive marker.
    """
    del now  # no longer used — the staleness check that consumed `now` was retired in v0.0.3
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

    # Every automatic off-ramp has been removed per the operator's "the
    # only termination should be human intervention" directive. Only
    # `conclude` / `stop` markers (human-placed) and `no_run` /
    # `inactive` (preconditions, not voluntary releases) can end the
    # session. `stop_hook_active` is deliberately NOT an off-ramp — it
    # only means we are mid forced-continuation chain. The turn counter
    # is still incremented for telemetry but no longer gates termination.
    turn_count = _bump_and_read_turn_count(run.path)

    # Track the forced-continuation chain depth. When `stop_hook_active`
    # is True this boundary is part of a chain we started; bump the
    # counter and write the preemptive RESPAWN_REQUESTED marker (the
    # host's without-progress cap could override the very block we are
    # about to return, ending the session with no further hook firing).
    # When False this is a fresh user-driven boundary: the chain reset,
    # so clear the counter and any stale preemptive marker.
    if stop_hook_active:
        block_count = _bump_and_read_block_count(run.path)
        _write_respawn_marker(run.path)
    else:
        block_count = 0
        _reset_block_count(run.path)
        _clear_respawn_marker(run.path)

    # Context-compaction telemetry + steering (v0.0.12). Measure the live
    # session's context size, persist it, and refresh the digest on the
    # interval — all delegated to a helper to keep this function's branch
    # count manageable.
    ctx_cfg, context_estimate = _context_telemetry(root, run.path, turn_count, transcript_path)

    # The cascade pick is computed and recorded in the loop history file
    # (`keepalive.history`). A repeated fingerprint never *releases* the
    # session — the contract is "always advance". But as of issue #27 the
    # repeat count drives the livelock reroute: when a github_issue /
    # accepted_rfc pick repeats `_LIVELOCK_REPICKS` consecutive turns the
    # agent is holding (not working) on a pick an open PR almost certainly
    # already covers, so we treat it as `nothing` and inject the
    # planning-phase prompt instead of "Continue on: X".
    try:
        choice = next_task(root)
    except Exception as exc:  # cascade must never crash the hook
        choice = None
        cascade_error: Exception | None = exc
    else:
        cascade_error = None

    repeats = 0
    if choice is not None:
        repeats = _record_and_count_repeats(run.path, _cascade_fingerprint(choice))

    livelock = (
        choice is not None
        and choice.source in _LIVELOCK_REROUTE_SOURCES
        and repeats >= _LIVELOCK_REPICKS
    )

    # Whether this turn routes the agent to the planning phase — true for a
    # livelock reroute, a `nothing` pick, and the `choice is None` defensive
    # fallback. An ideate/planning boundary is the natural compaction point
    # (nothing in-flight is lost if the host compacts here), so we ALWAYS
    # refresh the digest first regardless of the interval and tell the prompt
    # builder to mention it.
    routes_to_planning = livelock or _routes_to_planning_phase(choice, cascade_error)
    if routes_to_planning:
        with contextlib.suppress(Exception):
            write_digest(root)

    if livelock:
        # `choice` is non-None here (guarded by `livelock`).
        assert choice is not None
        reason = _planning_phase_prompt(
            header=f"[Nightly keepalive · run {run.id} · turn {turn_count}]",
            rationale=(
                f"Cascade livelock: the cascade has surfaced the same pick "
                f"({choice.source} → {choice.summary}) {repeats} consecutive "
                "turn boundaries without a task plan being started — it is "
                "almost certainly already covered by an open PR or otherwise "
                "not actionable. Do NOT hold on it; treat it as `nothing` and "
                "plan new work instead."
            ),
        )
        reason = _apply_context_diet(reason, context_estimate, ctx_cfg.budget_tokens)
        message = (
            f"run {run.id} turn {turn_count}: blocking stop and injecting "
            f"planning-phase prompt (livelock reroute: {choice.source} pick "
            f"repeated {repeats}x)."
        )
        return StopHookDecision(
            payload={"decision": "block", "reason": reason},
            reason_code="force_continue",
            message=message,
        )

    reason = _build_continue_reason_from(
        choice=choice,
        cascade_error=cascade_error,
        run_id=run.id,
        turn=turn_count,
    )
    reason = _apply_context_diet(reason, context_estimate, ctx_cfg.budget_tokens)
    if stop_hook_active:
        message = (
            f"run {run.id} turn {turn_count}: blocking stop "
            f"(forced-continuation chain, block #{block_count}) and "
            "injecting continuation prompt."
        )
    else:
        message = (
            f"run {run.id} turn {turn_count}: blocking stop and injecting continuation prompt."
        )
    return StopHookDecision(
        payload={"decision": "block", "reason": reason},
        reason_code="force_continue",
        message=message,
    )


def _routes_to_planning_phase(
    choice: CascadeChoice | None,
    cascade_error: Exception | None,
) -> bool:
    """True when `_build_continue_reason_from` will emit a planning-phase prompt.

    Mirrors that function's own branching: a `nothing` pick or a None choice
    (defensive fallback) both drop into the planning phase; a cascade error
    emits a generic investigate-then-resume nudge (NOT planning phase). The
    livelock case is handled separately by the caller, which already knows
    it is rerouting to planning."""
    if cascade_error is not None:
        return False
    if choice is None:
        return True
    return choice.source == "nothing"


def _apply_context_diet(reason: str, estimate: int | None, budget: int) -> str:
    """Prepend the context-diet block to `reason` when over the soft budget.

    No-op (returns `reason` unchanged) when the estimate is None (couldn't
    measure), the budget is 0 (steering disabled), or the estimate is within
    budget. Otherwise the diet block is prepended so the agent reads it
    before the continuation instructions."""
    if estimate is None or budget <= 0 or estimate <= budget:
        return reason
    return context_diet_block(estimate, budget) + reason


def _context_telemetry(
    root: Path | None,
    run_path: Path,
    turn_count: int,
    transcript_path: str | Path | None,
) -> tuple[Any, int | None]:
    """Measure context size, persist it, refresh the interval digest.

    Returns `(context_config, estimate)`. Estimating the live session's
    context from the transcript tail, persisting it for `nightly status`,
    and the interval digest write are all best-effort — a None estimate just
    means budget steering is skipped, and a digest write failure is
    swallowed (the digest is still written unconditionally on any planning-
    phase reroute by the caller). None of this may crash the hook.

    The return type is loose (`Any` for the config) to avoid a circular
    annotation import — the caller only reads `.budget_tokens` /
    `.digest_every_turns`.
    """
    ctx_cfg = load_context_config(root)
    estimate = estimate_context_tokens(transcript_path)
    _write_context_estimate(run_path, estimate)
    if ctx_cfg.digest_every_turns > 0 and turn_count % ctx_cfg.digest_every_turns == 0:
        with contextlib.suppress(Exception):
            write_digest(root)
    return ctx_cfg, estimate


def _write_context_estimate(run_path: Path, estimate: int | None) -> None:
    """Persist the latest context estimate to `keepalive.context`, best-effort.

    Writes the bare integer token count, or an empty file when the estimate
    is None (couldn't measure this turn). `nightly status` reads it back to
    surface the live context size. OSError-suppressed — telemetry only."""
    payload = f"{estimate}\n" if estimate is not None else ""
    with contextlib.suppress(OSError):
        (run_path / CONTEXT_FILENAME).write_text(payload, encoding="utf-8")


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


def _bump_and_read_block_count(run_path: Path) -> int:
    """Increment and return the forced-continuation chain block counter.

    Mirrors `_bump_and_read_turn_count`: best-effort, OSError-suppressed.
    Distinct from the turn counter — the turn counter is cumulative over
    the whole session, this resets to 0 on every fresh (non-chain) turn
    boundary so it measures the *depth of the current chain*.
    """
    counter = run_path / BLOCKS_FILENAME
    try:
        prior = int(counter.read_text(encoding="utf-8").strip()) if counter.is_file() else 0
    except (OSError, ValueError):
        prior = 0
    nxt = prior + 1
    with contextlib.suppress(OSError):
        counter.write_text(f"{nxt}\n", encoding="utf-8")
    return nxt


def _reset_block_count(run_path: Path) -> None:
    """Reset the chain block counter to 0 on a fresh, non-chain boundary."""
    counter = run_path / BLOCKS_FILENAME
    with contextlib.suppress(OSError):
        counter.write_text("0\n", encoding="utf-8")


_PLANNING_PHASE_DOCTRINE = "GENUINE WORK IS NEVER EXHAUSTED."
"""Top-of-prompt headline for the `nothing`-branch keep-alive.

Lifted verbatim from the operator directive that produced RFC-XX
(planning-phase keep-alive). When the cascade returns `nothing`, the
agent's failure mode is to rationalize "I have completed all genuine
work" and end the turn cleanly — the contract is the opposite. There
is always more to plan for; the cascade is one source of work, not
the only source. The sentinel sits at the top of every planning-
phase prompt so the agent can't miss it on a fast scan.

Past failure: agent wrote 'Genuine work is exhausted. … Starting a
fabricated slice now would be exactly the stacked-paperwork-PR
failure mode the contract warns against' — but Rule 11's anti-pattern
is about *consolidation* of unrelated work, not about stopping when
no fleet PRs remain. The sentinel + planning-phase prompt close this
loophole at the keepalive layer."""


def _planning_phase_prompt(*, header: str, rationale: str) -> str:
    """Build the imperative planning-phase prompt for `nothing` branches.

    When the cascade can't surface human-sourced or proposer-sourced
    work, the agent must enter a *planning phase*: read the codebase,
    name a usability/test/feature/refactor/documentation improvement,
    scope it as a Nightly task, and start executing — within the same
    turn, with assumed defaults, without prompting the operator. The
    prompt below is what the Stop hook injects to force that behavior.

    Shared between the `choice.source == 'nothing'` branch and the
    `choice is None` defensive fallback so both paths get the same
    treatment.
    """
    return (
        f"{header}\n"
        f"{_PLANNING_PHASE_DOCTRINE} The cascade surfaced no human-sourced "
        "work this turn, but that is a signal to PLAN, not to stop. "
        f"{rationale}\n"
        "\n"
        "═══ ENTER PLANNING PHASE — do not render the briefing, do not exit ═══\n"
        "\n"
        "(The session digest was just refreshed on disk at "
        ".nightly/runs/<id>/digest.md. An ideate/planning boundary is the "
        "natural compaction point — nothing in-flight is lost if the host "
        "compacts here, and the digest is re-injected automatically after "
        "any compaction. If your context is large, this is a safe place to "
        "let it shrink.)\n"
        "\n"
        "The cascade is one source of work, not the only one. Open PRs, "
        "RFCs, and triaged issues are *human-sourced* work; their absence "
        "does not mean the codebase is finished. Substantial improvements "
        "are always available — your job is to find one and ship it this turn.\n"
        "\n"
        "Walk this loop:\n"
        "  1. READ — open the repo as a fresh-eyes reader. Skim the largest "
        "or most-recently-touched source modules, the README, AGENTS.md / "
        "CLAUDE.md, `.planning/` (RFCs + drafts + iteration-log), recent "
        "uncertainty.md files, and the test suite. Look for what is missing "
        "or rough, not what is broken.\n"
        "  2. NAME — pick ONE substantial improvement from any of these "
        "angles (in rough priority order):\n"
        "     • **Usability** — confusing CLI ergonomics, inconsistent flag "
        "naming, poor error messages, missing `--help` text, undiscoverable "
        "features, install/setup friction.\n"
        "     • **Tests** — uncovered branches, missing edge-case coverage, "
        "fragile fixtures, slow tests that could be parallelized, integration "
        "gaps between modules.\n"
        "     • **Features** — small additive capabilities that compose with "
        "what exists (a new proposer, a new keep-alive strategy, a new "
        "briefing section, a new doctor probe).\n"
        "     • **Readability refactor** — dead code, duplicated logic, "
        "overly-long functions, unclear names, missing type hints, stale "
        "comments, modules that have outgrown their original shape.\n"
        "     • **Documentation paperwork** — drift between README claims "
        "and actual behavior, missing migration notes, ADRs that should be "
        "written, RFC checklists that should be reconciled, examples that "
        "no longer run.\n"
        "  3. ASSUME — every ambiguity has a default. Pick the option most "
        "consistent with the existing codebase and `.planning/` design intent "
        "and proceed. Do NOT write a plan-of-plans. Do NOT scope a research "
        "task. Do NOT park. Do NOT ask. Decision over deliberation.\n"
        '  4. SCOPE & SHIP — `nightly task <slug> -d "<one-line description>"` '
        "creates the plan. Set status to `in_progress` and start executing "
        "in this same turn — open a worktree (or write inline for audit-only "
        "work), make the edits, run `nightly verify`, and either open a PR "
        "or write a local proposal.md. The morning briefing is where the "
        "operator reviews the choice; the running session is where you make it.\n"
        "\n"
        "Anti-patterns the agent must NOT use to justify stopping:\n"
        "  ✗ 'Genuine work is exhausted.' — It is not. See doctrine above.\n"
        "  ✗ 'Starting work now would be a stacked-paperwork PR.' — Rule 11's "
        "anti-pattern is about CONSOLIDATING unrelated work, not about "
        "refusing to plan when fleet PRs end. Consolidation means 'extend an "
        "existing PR when related'; it does NOT mean 'stop when no related "
        "PR exists.'\n"
        "  ✗ 'Fabricated slice.' — A reasoned improvement scoped from "
        "reading the codebase is not fabricated; it's the cascade's "
        "ideate-fallback rung made explicit.\n"
        "  ✗ 'Wait for the operator's review.' — The operator is asleep. "
        "Review happens in the morning, not in the running session.\n"
        "\n"
        "If you can name a 'here's what I'd do' — that IS the recommendation. "
        "Ship it."
    )


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
        return _planning_phase_prompt(
            header=f"[Nightly keepalive · run {run_id} · turn {turn}]",
            rationale="The cascade returned no pick (defensive fallback path).",
        )

    header = f"[Nightly keepalive · run {run_id} · turn {turn}]"

    if choice.source == "nothing":
        # The cascade's `rationale` distinguishes the three reasons the
        # cascade can return `nothing` (proposers empty / all deduped /
        # session disarmed) — pre-Issue-#11 the hook hardcoded "the
        # proposer suite is empty" which was misleading whenever the
        # dedupe filter caught every proposal. Surface the rationale
        # verbatim so the agent sees the actual cause, then drop straight
        # into the planning-phase prompt below.
        rationale = choice.rationale or "The cascade returned `nothing`."
        return _planning_phase_prompt(header=header, rationale=rationale)

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


def _write_respawn_marker(run_path: Path, *, now: datetime | None = None) -> Path:
    """Drop the RESPAWN_REQUESTED marker preemptively during a forced chain.

    Written before each chain block (`stop_hook_active=True`) so that if
    the host's without-progress override ends the session silently, the
    resume marker is already on disk.

    Idempotent — a second write just refreshes the timestamp. The
    Claude skill reads this on the next `/nightly` invocation; if
    present, the new session picks up the cascade immediately
    instead of doing a fresh-session handshake.

    Best-effort: silently no-ops on OSError so the hook never crashes
    the model's turn just because we couldn't drop a marker.
    """
    marker = run_path / RESPAWN_REQUESTED_FILENAME
    payload = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ\n")
    with contextlib.suppress(OSError):
        marker.write_text(payload, encoding="utf-8")
    return marker


def read_respawn_marker(root: Path | None = None) -> str | None:
    """Read the RESPAWN_REQUESTED marker's content, or None if absent.

    The Claude skill calls this at session start. A non-None return
    value means the previous session ended involuntarily mid forced-
    continuation chain (host override without progress, crash, or kill)
    with work still on the cascade; the new session should walk `nightly
    next` immediately rather than running through a fresh-session prelude.

    Returns the trimmed content (ISO-8601 timestamp + optional
    cascade-pick summary on second line). Empty string is treated as
    "marker present but empty" — still a respawn signal."""
    run = current_run(root)
    if run is None:
        return None
    marker = run.path / RESPAWN_REQUESTED_FILENAME
    if not marker.is_file():
        return None
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _clear_respawn_marker(run_path: Path) -> None:
    """Remove the RESPAWN_REQUESTED marker. Called from `arm_session`
    (fresh handshake), `disarm_session`, and `request_stop` — any path
    that signals the operator has taken over reconciling state."""
    marker = run_path / RESPAWN_REQUESTED_FILENAME
    if marker.is_file():
        with contextlib.suppress(OSError):
            marker.unlink()


def arm_session(root: Path | None = None, *, now: datetime | None = None) -> Path | None:
    """Touch SESSION_ACTIVE under the current run. Returns the marker path.

    Idempotent — re-touching just refreshes the mtime. The marker has
    no TTL in v0.0.3+ (the 4h staleness check was removed); the mtime
    is preserved only for post-mortem audits.

    Also clears any stale RESPAWN_REQUESTED marker. Arming means "the
    session is now active again"; the operator (or the Claude skill
    respawn-detection path) has acknowledged the prior involuntary stop.
    Leaving a stale marker around would re-trigger the respawn-resume
    path on every subsequent `nightly status` check.
    """
    run = current_run(root)
    if run is None:
        return None
    _clear_respawn_marker(run.path)
    marker = run.path / SESSION_ACTIVE_FILENAME
    marker.write_text(
        (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ\n"),
        encoding="utf-8",
    )
    return marker


def disarm_session(root: Path | None = None) -> Path | None:
    """Remove SESSION_ACTIVE under the current run. Returns the marker path.

    v0.0.8+: also clears RESPAWN_REQUESTED. Disarming is the
    operator's explicit "session is over" signal; a leftover respawn
    marker would re-trigger the resume path on the next `nightly
    status` / `/nightly` invocation."""
    run = current_run(root)
    if run is None:
        return None
    _clear_respawn_marker(run.path)
    marker = run.path / SESSION_ACTIVE_FILENAME
    if marker.is_file():
        marker.unlink()
    return marker


def request_stop(root: Path | None = None) -> Path | None:
    """Touch the STOP sentinel under the current run. Returns the marker path.

    Unlike `conclude`, this does not wait for the current task to drain
    — the next Stop hook firing will allow the model to end its turn
    cleanly. Used for "the human walked over and wants this off right now".

    v0.0.8+: also clears any pending RESPAWN_REQUESTED — STOP is the
    operator-explicit "do not resume" signal and overrides the
    respawn-resume path.
    """
    run = current_run(root)
    if run is None:
        return None
    _clear_respawn_marker(run.path)
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
    context_tokens: int | None = None,
) -> Path | None:
    """Append a one-line audit entry to `keepalive.log` under the current run.

    The line carries a `ctx=<tokens|?>` field (v0.0.12) holding the latest
    context-size estimate (`?` when it couldn't be measured) so operators can
    grep the heartbeat history for context growth over a session. The rest of
    the format is unchanged — operators grep these lines, so the existing
    fields stay put."""
    run = current_run(root)
    if run is None:
        return None
    log_path = run.path / "keepalive.log"
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id = (hook_input or {}).get("session_id") or "?"
    ctx = str(context_tokens) if context_tokens is not None else "?"
    line = (
        f"{stamp}  decision={decision.reason_code:<16}  "
        f"session={session_id}  ctx={ctx}  msg={decision.message}\n"
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
