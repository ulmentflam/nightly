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
    "RESPAWN_REQUESTED_FILENAME",
    "SESSION_ACTIVE_FILENAME",
    "STOP_FILENAME",
    "HookFormat",
    "StopHookDecision",
    "arm_session",
    "compute_stop_hook_decision",
    "disarm_session",
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
"""Marker file under the run dir. Written when `compute_stop_hook_decision`
yields with `host_cap` — Claude Code's own 9-consecutive-block override
that we can't fight from Python. The marker signals "this session ended
involuntarily, not because the operator asked for it; the cascade still
had work" so a respawn supervisor (RFC 010, planned) or the operator can
pick up where we left off.

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
- Manually via `rm .nightly/runs/<id>/RESPAWN_REQUESTED`."""

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
        # Yield to the host's 9-consecutive-block override (we cannot
        # fight it from Python — the documented protocol is to bow out
        # cleanly), but drop a RESPAWN_REQUESTED marker on disk so the
        # operator (or an RFC 010 supervisor) knows this stop was
        # involuntary and the cascade still had work. Issues #13 and
        # #16 both hit this exact failure mode: 1 force_continue
        # followed by host_cap within ~17 minutes, work still pending.
        run = current_run(root)
        if run is not None:
            _write_respawn_marker(run.path)
        return StopHookDecision(
            payload={},
            reason_code="host_cap",
            message=(
                "host signaled stop_hook_active — yielding to the host's "
                "consecutive-block cap; will not force-continue this turn. "
                "RESPAWN_REQUESTED marker written — re-invoke `/nightly` "
                "to resume the cascade where this session left off."
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
    """Drop the RESPAWN_REQUESTED marker for an involuntary host-cap stop.

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
    value means the previous session ended via `host_cap` with work
    still on the cascade; the new session should walk `nightly next`
    immediately rather than running through a fresh-session prelude.

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

    v0.0.8+: also clears any stale RESPAWN_REQUESTED marker. Arming
    means "the session is now active again"; the operator (or the
    Claude skill respawn-detection path) has acknowledged the prior
    host_cap stop. Leaving a stale marker around would re-trigger the
    respawn-resume path on every subsequent `nightly status` check.
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
