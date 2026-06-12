---
status: accepted
sized: true
title: Interactive context compaction — digest-based hygiene and SessionStart re-injection
created: 2026-06-11
sized_on: 2026-06-11
accepted_on: 2026-06-11
shipped_on: 2026-06-11
shipped_version: 0.0.12
author: nightly-seed
source: interactive_seed
estimated_effort: ~6h across 2 phases
---

# RFC 011 — Interactive context compaction

## Status

`accepted` — shipped in v0.0.12. All five design pieces implemented and
green. Operator request: "give Nightly the ability to compact context
automatically at ideate-reroute boundaries and on a configurable
interval, targeting a soft 256K token ceiling."

## Context

### The host constraint

Claude Code's `/compact` slash command is the user-facing lever for
compressing the conversation context. It is not programmable from inside
a running session:

- Hook `reason` text is plain prose injected as a user message; the host
  does not interpret it as a slash command.
- There is no tool or API surface that triggers compaction from within a
  Stop hook or any other hook type.
- Claude Code's auto-compact threshold is set by the host at launch time
  and is not configurable at runtime by a hook or by `settings.json`.

Verified against the Claude Code hooks guide at `code.claude.com/docs`
(fetched 2026-06-11): the Stop hook shape is `{"decision": "block",
"reason": "<text>"}` — `reason` is a plain-text prompt, not a command
interpreter. No compaction API is listed in the hook specification.

### The problem

An overnight Nightly session accumulates context across many force-
continue chains. By the early morning hours the context window can be
substantially filled, which:

1. Raises the per-token cost of every continuation prompt.
2. Eventually triggers Claude Code's auto-compact (threshold unknown,
   host-controlled), which discards most session state — leaving the
   agent without run id, plan status, cascade history, or open PR list.
3. Makes the agent's next response unpredictable: it may hallucinate
   prior state or restart from scratch.

Since Nightly cannot *force* compaction, the implementable equivalent
is to make compaction *lossless*: render the handful of facts the agent
needs to keep working after a compaction and re-inject them via the
host's sanctioned `SessionStart(compact)` hook.

### Motivation: the operator request

The operator's 2026-06-11 request, paraphrased: "compact-equivalent
behavior at ideate reroutes and on a configurable interval, with a soft
256K token target." The host constraint means we cannot trigger
compaction; the response is a five-piece design that makes compaction
safe whenever it does happen and steers the agent toward hygiene before
the context bloats.

## Design

Five pieces, shipped together in v0.0.12.

### Piece 1 — Session digest (`nightly_core/digest.py`)

`render_digest` produces a compact markdown document (~30–60 lines)
covering: Nightly version + run id, keepalive turn / chain-block
counters, lifecycle markers (SESSION_ACTIVE, CONCLUDE, STOP,
RESPAWN_REQUESTED), git branch, last cascade pick, active plans grouped
by status (in_progress / blocked: approval / done count), open Nightly
PRs (`#N branch-name`), and the autonomy one-liner
("if you can name a recommendation, execute it").

`write_digest` persists it to `.nightly/runs/<id>/digest.md`. Every
section is independently fault-isolated (a failure in the PR-list
section degrades to a one-line "unavailable" note rather than aborting
the whole render) because this code runs inside a hook that must never
crash the model's turn.

### Piece 2 — Context telemetry

The Stop hook now calls `estimate_context_tokens(transcript_path)` on
every turn boundary. The estimate sums the four usage fields
(`input_tokens + cache_creation_input_tokens + cache_read_input_tokens +
output_tokens`) from the last assistant message in the Claude Code
transcript JSONL. Only the final `_TRANSCRIPT_TAIL_BYTES` (256 KiB) of
the transcript are read so the hook stays fast on very long sessions.
Returns `None` on any parse failure — the estimate is best-effort and
never gates a decision.

The estimate is persisted to `keepalive.context` (a bare integer) so
`nightly status` can show "context: ~NK tokens at last turn boundary"
without re-parsing the transcript. Every heartbeat line in
`keepalive.log` gains a `ctx=<N>` field (or `?` when unknown).

### Piece 3 — Soft budget steering

`context.budget_tokens` in `.nightly/config.yml` (default 256000; `0`
disables) is the soft ceiling. When `estimate > budget`, the injected
continuation prompt is prefixed with a "context diet" block rendered by
`context_diet_block(estimate, budget)`. The block:

1. States the soft nature of the limit (finish any delicate in-flight
   step first).
2. Points at the fresh session digest.
3. Recommends dispatching heavy work to background specialists (separate
   context windows).
4. Discourages re-reading large files or dumping long command output
   inline.
5. Instructs the agent to persist precious state now.
6. Closes with: "Do not stop the session over context size."

`context.digest_every_turns` (default 1) controls how often the digest
is refreshed on disk independent of the budget check. 0 disables the
interval write; the digest is still written unconditionally before any
planning-phase reroute.

### Piece 4 — Planning-phase digest flush

An ideate/planning-phase boundary is the natural compaction point:
nothing is in-flight there (no code change is mid-edit), so the host
can compact without losing work. The hook detects all planning-phase
reroutes (`choice.source == "nothing"`, livelock reroute, `choice is
None` defensive fallback) and calls `write_digest(root)` before
building the continuation prompt, regardless of the interval setting.
The planning-phase prompt text also mentions that the digest is fresh
and that this is a safe compaction point.

### Piece 5 — `SessionStart(compact)` hook re-injection

`nightly init` (via `ClaudeHostIntegration.install`) now merges a second
hook entry into `.claude/settings.local.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "compact",
        "hooks": [{"type": "command", "command": "nightly hook session-start"}]
      }
    ]
  }
}
```

Claude Code fires `SessionStart` with `source="compact"` immediately
after any compaction (auto or manual `/compact`). The handler
(`nightly hook session-start`, implemented as `hook_session_start()` in
`cli.py`) renders the digest fresh and emits it as JSON:

```json
{"additionalContext": "<digest markdown>"}
```

The `additionalContext` field is injected by Claude Code as context at
the start of the new post-compaction session, giving the agent immediate
access to run id, plan status, open PRs, and the autonomy one-liner.
The handler emits `{}` (no-op) when there is no active Nightly run or
the SESSION_ACTIVE marker is absent, so it is safe to install globally
and never interferes with non-Nightly sessions.

## What was NOT done and why

**No programmatic `/compact` injection.** Even if the host did interpret
`reason` as a slash command (it does not), injecting `/compact` in a
Stop hook `reason` text would stall an unattended overnight session: the
compact operation leaves the session idle waiting for the host to fire
the next event, and in an unattended context there is no user to resume
it. The correct design is to make compaction lossless when it happens
naturally, not to force it at arbitrary boundaries.

**No auto-compact threshold configuration.** Claude Code's auto-compact
threshold is not exposed in any config or hook API surface verified
against the current docs. Adjusting it is not possible from Nightly's
side.

**No polling for post-compaction resumption.** The `SessionStart` hook
fires synchronously when Claude Code processes the compact event; no
polling or re-arm is needed. The digest is rendered at hook-fire time
rather than stale from disk because a compaction discards the model's
prior view of the digest anyway.

## Future work

- **Other hosts.** The `SessionStart(compact)` hook is Claude Code–
  specific. Codex CLI, Cursor, Antigravity, and Gemini CLI each have
  their own hook models (or none); their compact-equivalent behavior is
  unverified and deferred.
- **Tighter context estimates.** The current estimate sums usage fields
  from the last transcript assistant message, which approximates but
  does not exactly equal the live context window. If Claude Code ever
  exposes the current context size to hooks (e.g. as a `context_tokens`
  field in the Stop-hook stdin payload), the estimate can be replaced
  with the exact figure.
- **Digest size tuning.** The digest is currently ~30–60 lines. For very
  long sessions with many tasks and PRs it could grow larger; a future
  iteration might cap individual sections (e.g. top-N plans, top-N PRs)
  to keep the `additionalContext` payload within Claude Code's injection
  limits.

## Sized checklist

**Phase A — Digest + telemetry + budget steering**
- [x] A1. `nightly_core/digest.py`: `render_digest` + `write_digest` with per-section fault isolation
- [x] A2. `estimate_context_tokens(transcript_path)` in `keepalive_hook.py`, 256 KiB tail scan
- [x] A3. `context_diet_block(estimate, budget)` renderer
- [x] A4. `_context_telemetry()` helper wires estimate, `keepalive.context`, interval digest write
- [x] A5. `log_heartbeat` gains `ctx=` field; `CONTEXT_FILENAME` exported
- [x] A6. `ContextConfig` dataclass + `load_context_config()` in `config.py`
- [x] A7. `context:` block in `_DEFAULT_CONFIG_YML` in `cli.py`

**Phase B — Planning-phase flush + SessionStart hook**
- [x] B1. Planning-phase reroute always calls `write_digest()` before building the prompt
- [x] B2. Planning-phase prompt text mentions digest freshness and safe-compaction-point note
- [x] B3. `hook_session_start()` CLI command (`nightly hook session-start`) re-injects digest
- [x] B4. `ClaudeHostIntegration.install` merges `SessionStart(compact)` hook entry
- [x] B5. `nightly status` context line via `_render_context_line()` in `cli.py`
