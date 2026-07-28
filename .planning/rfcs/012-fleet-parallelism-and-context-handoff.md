---
status: accepted
sized: true
title: Fleet parallelism and context-handoff protocol
created: 2026-07-28
sized_on: 2026-07-28
accepted_on: 2026-07-28
author: operator
source: interactive_seed
estimated_effort: ~5h across 3 phases
phase_a: implemented
phase_b: implemented
phase_c: implemented
---

# RFC 012 — Fleet parallelism and context-handoff protocol

## Status

`implemented` — all three phases landed 2026-07-28. Operator seed in the 2026-07-28 interactive session,
alongside the RFC 007 amendment. Two knobs that only make sense
together: how *wide* the fleet runs, and what an individual agent does
when its context fills up. Phase A lands the config schema and the
threshold resolver; Phase B enforces the caps at dispatch admission;
Phase C wires the handoff protocol into the keepalive hook.

## Context

RFC 007 answers "which model runs this task." It does not answer the two
questions that actually determine overnight throughput:

**How many agents run at once?** Nightly dispatches specialists one at a
time today. Every structural prerequisite for running them wide already
exists — background dispatch keeps the operator's chat free (v0.0.7),
worktrees isolate the filesystem per task (RFC 002/004), and
`nightly verify` gates every diff before it can reach a PR. What is
missing is a declared ceiling and something that honors it. Without a
ceiling the agent has no basis for deciding whether to fan out; without
enforcement, a ceiling is a comment.

**What happens when an agent fills its context?** Today: nothing
principled. The v0.0.12 `context.budget_tokens` soft budget nudges the
*session* toward hygiene via a context-diet block in the continuation
prompt, but a specialist that has been grinding for hours has no defined
exit. It degrades — re-reading its own history, thrashing, and
eventually truncating mid-write. The failure is worst precisely when the
agent is deepest into valuable work.

These are one problem seen twice. A wide fleet is only useful if
individual agents recycle cleanly; agent recycling is only affordable if
you can spin up replacements in parallel.

## Non-goals

- **A scheduler.** This RFC declares ceilings and a handoff protocol. It
  does not add a queue, priority preemption, or work stealing. Admission
  control is "count what is running, refuse to exceed the cap."
- **Cross-machine distribution.** Everything is one host, one repo.
- **Automatic context measurement inside specialists.** The keepalive
  hook already estimates the *session's* context from the host
  transcript. Specialists self-report against a threshold they are told;
  Nightly does not instrument the sub-agent's own token stream.
- **Killing an agent at the hard threshold.** The hard threshold is an
  instruction to the agent, not a `SIGKILL`. An agent asked to stop and
  summarize produces a usable handoff; a killed one produces nothing.
- **Merging handoff summaries automatically.** The successor agent reads
  the summary as context. Nightly does not diff or reconcile summaries.

## Proposed direction

Three approaches; **Approach C** ships as v1.

### A — Absolute token thresholds

Config declares `handoff_soft_tokens: 256000` and
`handoff_hard_tokens: 500000` as absolute numbers.

**Pros:** trivially understandable; the operator states exactly the
numbers they mean.

**Cons:** wrong for every model but the one the numbers were chosen for.
A 200K-context lite agent would never reach a 256K soft threshold — it
would hit its real ceiling and truncate, with the "protection" never
firing. Under a heterogeneous fleet (which RFC 007 explicitly creates)
this is not an edge case; it is the common case.

### B — Per-model absolute thresholds

Config declares a soft/hard pair per model id.

**Pros:** exact control per model.

**Cons:** N models × 2 numbers of bookkeeping, re-derived by hand every
time a model is added or its window changes. The numbers are not
independent — they are always "about a quarter" and "about half" of the
window. Encoding a ratio as 2N hand-maintained integers invites drift.

### C — Ratios of the model's context window

Config declares `handoff_soft_ratio` and `handoff_hard_ratio` as
fractions; Nightly multiplies by the dispatched model's known window.

**Pros:** one setting governs a mixed fleet. At the 0.25 / 0.50 defaults
a 1M-context model hands off at 250K and hard-stops at 500K, while a
200K-context model does the same at 50K and 100K — the same *behavior*,
proportionally. Adding a model means declaring its window once (a fact
about the model), not re-deriving two thresholds (a policy decision).

**Cons:** requires knowing each model's context window. Mitigated by
shipping the Anthropic windows as defaults and falling back to a
conservative 200K for anything undeclared — under-estimating costs one
harmless early handoff, over-estimating costs a truncated one.

## Resolved technical decisions

**1. Approach C ships as v1.** Ratios, not absolutes.

**2. Default ratios are 0.25 soft / 0.50 hard.** Derived from the
operator's stated shape — 256K and 500K on a 1M-context model — and
generalized. An agent is most productive in the first quarter of its
window; past halfway, "just one more step" risks truncating a write,
which loses work rather than merely wasting tokens.

**3. Two thresholds with different semantics.** `soft` = *finish the
task you are on*, write a handoff summary, and let a fresh agent resume
the same goals with a clean context. `hard` = *stop now*, mid-task,
and write the summary. The soft threshold optimizes for clean seams;
the hard one is a guard against the one failure mode that destroys work.

**4. Handoff summaries carry goals, not transcripts.** The summary
states what the task is, what is done, what is left, and what was
learned that is not on disk. It is not a conversation dump — the point
of the handoff is to *shed* the history, so re-injecting it defeats the
exercise. The existing `digest.md` machinery (v0.0.12) is the natural
carrier.

**5. Parallelism has a global cap and a per-tier cap; the tighter
wins.** `max_concurrent_specialists` bounds the whole fleet;
`per_tier` bounds each RFC 007 tier. Defaults: 8 global, and
`lite: 8 / coding: 6 / reasoning: 2`.

The gradient is the entire cost-control mechanism. Running eight lite
agents is cheap and is exactly what "maximize parallelism" should mean.
Running eight reasoning agents is neither cheap nor useful —
adjudication work largely serializes anyway, so the marginal agent
mostly adds cost. Capping the expensive tier is what lets the cheap
tiers run wide without the bill scaling with the fan-out.

**6. `0` means unlimited, on every axis.** Consistent with the rest of
the config, where `0` disables a limiter rather than meaning "zero
allowed" (cf. `context.budget_tokens: 0`). Negative values clamp to `0`.

**7. `max_worktrees` is the task-level fan-out cap.** Each parallel task
needs a worktree, so this is effectively "how many tasks in flight."
Default 8, matching the specialist cap.

**8. Thresholds resolve against the *dispatched* model.** A lite
researcher on a 200K model gets 50K/100K in the same session where a
reasoning reviewer on a 1M model gets 250K/500K. This is why the
resolver lives next to `resolve_model_for_task` — the model decision and
the budget decision are the same decision.

**9. Unknown models fall back to 200K.** Conservative by construction;
operators declare real windows under `context.model_context_tokens:`.

**10. Inverted ratios fall back to defaults, loudly.** A config where
soft exceeds hard would fire the soft threshold *after* the hard stop,
inverting the protocol. Rather than guess which the operator meant, both
revert to defaults with a logged warning.

## Risks

- **A wide fleet multiplies a bad decision.** Eight agents acting on a
  wrong plan produce eight wrong diffs. Mitigation: fan-out happens
  below the plan level — the plan is settled by a reasoning-tier
  orchestrator before specialists spawn — and `nightly verify` gates
  every diff independently.

- **Worktree contention.** Eight concurrent worktrees on one repo is
  real disk and real `git` load. Mitigation: `max_worktrees` is a
  separate knob from `max_concurrent_specialists` precisely so an
  operator can run many specialists across few worktrees.

- **Handoff loses tacit context.** The successor agent knows what the
  summary says and nothing more. Mitigation: this is the same tradeoff
  compaction already makes, and the soft threshold exists so the common
  case hands off at a task boundary where there is little tacit state to
  lose.

- **Threshold thrash.** An agent hovering at the soft threshold could
  hand off repeatedly, each successor immediately re-crossing it.
  Mitigation: the soft threshold requires *finishing the current task*
  first, so a handoff always makes forward progress. Phase C should also
  record handoff count per task so a pathological loop is visible in the
  briefing.

- **Per-tier caps interact with the cascade.** A cascade pick needing a
  reasoning dispatch when both reasoning slots are busy must wait rather
  than silently downgrade. Phase B: admission returns "wait," never
  "run it on a cheaper tier" — a silent downgrade would defeat RFC 007
  Resolved #4's whole argument about the reviewer.

## Implementation phases

### Phase A — Config schema + threshold resolver (~2h)

- **A1.** `ParallelismConfig` + `load_parallelism_config` in
  `nightly_core.config`.
- **A2.** `ContextConfig` gains `handoff_soft_ratio`,
  `handoff_hard_ratio`, `default_context_tokens`,
  `model_context_tokens`.
- **A3.** `DEFAULT_MODEL_CONTEXT_TOKENS` seeded with the Anthropic
  windows.
- **A4.** `ContextThresholds` + `resolve_context_thresholds` +
  `context_window_for` in `nightly_core.routing`.
- **A5.** `ParallelismConfig.limit_for(tier)` — tighter-of-two.
- **A6.** `parallelism:` and the new `context:` keys in the default
  config template.
- **A7.** Unit tests: ratio scaling across window sizes, breach
  classification, `0`-means-unlimited, inverted-ratio fallback,
  clamping.

**Merge gate:** unit tests pass; existing suite green; a pre-RFC-012
config still parses.

### Phase B — Admission control (~2h)

- **B1.** `nightly dispatch start` counts live dispatches for the run
  and refuses to exceed `limit_for(tier)`, exiting with a distinct code.
- **B2.** `nightly dispatch status` shows current vs cap per tier.
- **B3.** Worktree creation honors `max_worktrees`.
- **B4.** Skill text on each host: fan out to the cap by default;
  dispatch specialists in one batch rather than serially.
- **B5.** Tests: admission at, below, and above cap; unlimited mode.

**Merge gate:** Phase A merged; caps observably enforced.

### Phase C — Handoff protocol (~1h)

- **C1.** Keepalive hook compares the estimate against the resolved
  thresholds and injects the soft/hard handoff instruction.
- **C2.** Handoff summary written through the existing `digest.md`
  path, with a `handoff:` section naming the unfinished goals.
- **C3.** Briefing surfaces handoff count per task.
- **C4.** Skill text documents the two-threshold protocol.

**Merge gate:** Phases A + B merged; a simulated over-budget session
produces a handoff summary naming its unfinished goals.

## Sized checklist

**Phase A — Config schema + threshold resolver** — *implemented 2026-07-28*
- [x] A1. `ParallelismConfig` + loader
- [x] A2. `ContextConfig` handoff keys
- [x] A3. `DEFAULT_MODEL_CONTEXT_TOKENS`
- [x] A4. `ContextThresholds` + resolver
- [x] A5. `limit_for(tier)` tighter-of-two
- [x] A6. Config template blocks
- [x] A7. Unit tests (in `tests/test_routing.py`)

**Phase B — Admission control** — *B1/B2/B5 landed 2026-07-28*
- [x] B1. `dispatch start` admission check — refuses with exit code 3 and
      names the cap that blocked it; `--force` overrides. Dispatch state
      now persists its tier so counting doesn't re-resolve the plan.
- [x] B2. `dispatch status` shows a `capacity:` line (live/cap per tier
      plus the global total)
- [x] B3. Worktree creation honors `max_worktrees` — raises the typed
      `WorktreeCapReached` (not a bare RuntimeError, so "at capacity" is
      distinguishable from "git failed"), checked before the branch is
      cut so a refused request leaves nothing behind. Both callers
      (`nightly worktree`, the headless driver) pass the configured cap;
      CLI exits 3, matching `dispatch start`.
- [x] B4. Fan-out-to-cap guidance — same delivery as RFC 007 C1: rule 12
      of the shared rules block, not six skill files.
- [x] B5. Admission tests (`tests/test_admission.py`, 14 cases) —
      including the liveness rule: a dispatch whose PID is gone must not
      occupy a slot, or an unpolled crash wedges the fleet

**Phase C — Handoff protocol** — *C1 landed 2026-07-28*
- [x] C1. Keepalive threshold comparison + prompt injection — a three-rung
      ladder (hard handoff > soft handoff > the v0.0.12 diet nudge). The
      handoff block *replaces* the diet block rather than stacking; two
      competing directives in one prompt is how an agent follows neither.
      Session thresholds resolve against the reasoning tier's model, per
      Resolved #8 and rule 12.
- [x] C2. `Pending handoffs` section in `digest.md` — the digest is what
      the `SessionStart(compact)` hook re-injects, so a handoff written
      just before a compaction survives the very event it exists for.
- [x] C3. Briefing handoff panel — names the task and its outstanding
      work rather than a bare count; "which work was left" is the
      operator's actual first question.
- [x] C4. Two-threshold protocol text — in rule 12. (C1–C3, the
      keepalive enforcement, remain open: the doctrine is documented but
      the hook does not yet compare against the thresholds.)
