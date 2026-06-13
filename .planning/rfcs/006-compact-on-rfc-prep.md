---
status: accepted
sized: true
title: Compact the session — boundary fire after RFC prep + threshold fire on long context
created: 2026-06-04
sized_on: 2026-06-04
accepted_on: 2026-06-04
author: nightly-seed
source: interactive_seed
estimated_effort: ~5h across 3 phases
---

# RFC 006 — Compact the session: boundary + threshold triggers

## Status

`accepted` — operator seed in the 2026-06-04 interactive session
(authoring RFCs 006–008 as a bundle), plus a follow-up amendment in
the same session asking for an *additional* threshold-based trigger
that fires whenever the conversation context exceeds a configurable
cap (default 256k tokens, with the 1M-context-window models like
Opus 4.7 allowed to bleed slightly over). Approach C ships as v1 —
skill-only, host-gated by compaction capability, with the threshold
cap living in `.nightly/config.yml`.

Phase A wires the boundary trigger (after RFC prep) into the
Claude Code skill. Phase B documents the no-op on the other five
hosts. Phase C adds the threshold-based mid-loop trigger and the
config schema — same compaction primitive, fired by a different
signal.

## Context

Interactive `/nightly` invocations open with a heavy reading pass:
the agent loads `AGENTS.md`, `CLAUDE.md`, every accepted RFC under
`.planning/rfcs/`, the cascade's rationale, any seed prompt the
operator typed, and — in the RFC 005 pathway — possibly drafts a
new RFC body that itself runs hundreds of lines. By the time
`nightly next` returns the first cascade pick, the conversation
buffer is already deep into the host's context window.

Every subsequent turn — cascade walk, plan dispatch, specialist
sub-agent invocation, briefing render — pays for that context
again. On Claude Code with Opus the effect is acute: a 60k-token
prep pass turns each later turn into a 60k+N token request, even
though the agent only needs the *summary* of what it read, not the
verbatim sources. The model rereads `CLAUDE.md` every turn instead
of working from a distillation.

Claude Code exposes `/compact` — a built-in slash command that
summarizes the conversation in place and lets the session continue
from the summary. (Beyond that, the host APIs also offer
`compactArgsHint` and a programmatic compaction primitive; for v1
we lean on the user-facing slash command because it's stable,
documented, and doesn't require an API surface.) After RFC prep,
the agent can call `/compact` to discard the verbatim reads while
preserving the decisions they produced.

Codex CLI, Cursor, Gemini CLI, Antigravity, and opencode do not
currently ship an equivalent. The trigger is a no-op on those
hosts; the rest of the contract (read context, prepare RFC, walk
cascade) is unchanged.

This RFC ships **two triggers** for the same compaction primitive:

1. **Boundary fire** at the interactive-start boundary — *after* the
   agent has done its reading and either picked up an existing
   accepted RFC or stubbed a new one via `seed-rfc`, but *before*
   the first specialist dispatch. Catches the predictable heavy-read
   prep pass at session start.
2. **Threshold fire** mid-loop — whenever the conversation context
   grows past a configurable token cap (default 256k, settable via
   `.nightly/config.yml`). Catches the slow accumulation that
   long-running sessions produce as specialist dispatches and file
   reads pile up. The cap is **soft**: a few thousand tokens of
   bleed-over is tolerated, especially on 1M-context-window models
   where the cost of crossing 256k is gradual rather than
   catastrophic. Triggering aggressively *before* the model itself
   forces a compaction (Claude Code auto-compacts near the hard
   ceiling) keeps the agent in control of *when* the summary
   happens — at a turn boundary, not mid-file-read.

## Non-goals

- **Compacting between every cascade iteration.** Mid-loop
  compaction at *every* iteration boundary would discard the
  working memory the agent needs to chain plan → implementer →
  tester → reviewer → briefing. The threshold fire is *condition*-
  based (cap exceeded), not iteration-based — it only triggers
  when the buffer is actually heavy.
- **Headless `nightly run` compaction.** The headless driver spawns
  fresh host processes per task — the conversation buffer never
  accumulates the way an interactive session's does. No trigger
  needed.
- **Codex/Cursor/Gemini/Antigravity/opencode compaction.** Those
  hosts don't currently expose an equivalent primitive; the skill
  text documents the gap and explicitly no-ops there until a host
  ships compaction.
- **Precise token counting.** The agent rarely has a programmatic
  read of the exact context-window usage. The threshold check is
  *approximate* — the skill instructs the agent to estimate based
  on visible signals (number of file reads, specialist dispatches,
  embedded RFC bodies, conversation depth) and triggers when the
  estimate crosses the cap. Bleed-over of a few thousand tokens is
  expected and fine; the goal is to fire *before* the model's own
  forced-compaction kicks in, not to land at exactly N tokens.
- **Compaction-aware re-reads.** After compaction the agent may
  need to re-read a specific file (the plan it's about to dispatch
  against, for example). v1 trusts the agent's normal Read tool
  use for this — no special "re-hydrate" primitive.

## Proposed direction

Three approaches; **Approach C** ships as v1.

---

### A — Auto-compact at `nightly session start` time

Have `nightly session start` print a `compact_hint` line that the
skill picks up and forwards to the host. The CLI knows nothing
about which host it's running under, so this is structural — we
add a `--print-compact-hint` flag and the skill includes the
output.

**Pros:**
- Centralizes the trigger in `nightly-core` — one place to update
  when Claude Code's compact primitive changes.
- Makes the trigger uniform across hosts that *do* support
  compaction.

**Cons:**
- `nightly session start` is the *first* CLI call in a session,
  before any reading has happened. Compacting then is pointless —
  the buffer is empty.
- Pushes host-specific primitives (the literal `/compact` string)
  into core, violating the existing layering where host
  particulars live in host-package skill.md files.

---

### B — CLI helper `nightly compact`

A new command that prints the host's compact incantation, the same
way `nightly specialist <role>` prints a sub-agent system prompt.
The skill calls `nightly compact` at the right moment and forwards
the output to the host.

**Pros:**
- Keeps the trigger discoverable (a real CLI verb the operator can
  also run manually).
- Centralizes the per-host incantation table in core, so
  Claude/Codex/etc. can evolve in lockstep.

**Cons:**
- "Print a slash command for the agent to then execute" is a
  layering inversion — the agent has to forward CLI output as a
  user-style command back to itself. Workable but awkward.
- The empty-string output for unsupported hosts is hard to
  test against from the agent side; the skill ends up checking
  for emptiness explicitly.
- Adds a CLI surface for a feature that is fundamentally skill-side
  (the agent is the one who decides when to compact, not a
  programmatic caller).

---

### C — Skill-only trigger with per-host gating

Update each host's `skill.md` to instruct the agent: after
arming the keep-alive, reading context, and either picking up an
accepted RFC or seeding a new one via `seed-rfc`, *if the host
supports session compaction*, invoke it. Claude Code's skill spells
out `/compact`; the other five hosts say "skip — your host doesn't
support compaction yet."

**Pros:**
- Layering stays clean: host-specific incantations live in the
  host package's `skill.md`, where every other host-specific bit
  already lives (Stop-hook command, dispatch primitive, etc).
- Zero new CLI surface, zero new core code. Touches only the six
  skill files.
- Easiest to test: the trigger paragraph either contains
  `/compact` (Claude) or doesn't (others). RFC 004 §D4 already
  paid the cost of per-host text drift, and the doctor's
  content-drift check (RFC 005 §B4) can be extended to require
  the compact reference on Claude.

**Cons:**
- Six skill files to update in lockstep — same per-host drift
  hazard the existing seed-rfc paragraph and `depends_on_pr`
  paragraph live with. Mitigation: the doctor extension catches
  drift.
- The "after RFC prep" boundary has to be described in prose; the
  agent decides what counts as "done with prep." For the standard
  flow (context-read → cascade walk → first pick) this is crisp;
  for unusual paths (no cascade work, fall through to keepalive
  strategies) the boundary is fuzzier. Approach C documents the
  common path and accepts the agent's judgment on edge cases.

---

## Resolved technical decisions

**1. Approach C ships as v1.** Approach A was rejected because the
compact-at-session-start timing is structurally wrong (buffer is
empty). Approach B was rejected because the CLI-helper indirection
doesn't earn its complexity for a feature the agent is the natural
caller of. Approach C keeps host-specific incantations in host
packages and adds zero core code — matching the layering established
by the dispatch primitives and Stop-hook commands.

**2. Boundary trigger: after RFC prep, before first specialist
dispatch.** The skill paragraph names this boundary explicitly:
*"After you have armed the keep-alive, read the cascade rationale,
and either confirmed an existing accepted RFC or seeded a new one
via `nightly seed-rfc`, if your host supports session compaction,
invoke it now — your context is at its heaviest right before the
implementation loop starts. The compact preserves the planning
artifacts on disk; only the verbatim conversation buffer gets
summarized."*

**2a. Threshold trigger: mid-loop when context exceeds the cap.**
A second skill paragraph (Claude only for v1) instructs the agent
to estimate its own context usage at every cascade-walk boundary
(every `nightly next` call) and invoke `/compact` whenever the
estimate exceeds `compact.context_token_cap` from `.nightly/
config.yml`. Default cap is **256,000 tokens** — large enough that
ordinary multi-task sessions don't fire it, small enough that the
1M-context Opus 4.7 / Sonnet 4.6 hosts stay well inside their
cache-efficient envelope. Estimation is rough and operator-
tunable; the bias is "compact a little early" rather than "wait
for the model's hard limit."

**3. Claude Code: `/compact` slash command, not the API.** The
slash command is the user-stable, documented surface. The
underlying API (`microcompact`, `compactArgsHint`) is hostable but
its shape is less stable across Claude Code releases. v1 leans on
`/compact`; if/when the API stabilizes for programmatic use, v2 can
flip Claude's skill text to point at it.

**4. Five-host no-op.** The Codex / Cursor / Gemini / Antigravity /
opencode skills get a one-line statement that their host doesn't
support compaction today, with a TODO link back to this RFC so a
later host upgrade can flip them on. No CLI no-op stub — silence is
fine.

**5. Boundary fire is unconditional; threshold fire is conditional.**
The boundary trigger (Resolved #2) fires every time on Claude
without measuring buffer size — it's at a structural moment where
heavy reads have just happened. The threshold trigger (#2a) is
conditional on the agent's estimated context size exceeding the
configured cap. Both can fire in the same session.

**5a. Config schema: `compact.context_token_cap` (int, default
256000) and `compact.enabled` (bool, default true).** Lives under
`.nightly/config.yml`'s top-level `compact:` block. The cap is in
tokens (not bytes, not characters) because models reason about
tokens; the agent's estimator works at token granularity even when
imprecise. Setting `enabled: false` disables *both* triggers; the
operator can also use a high cap (e.g. `999999999`) to effectively
disable the threshold fire while keeping the boundary fire.

```yaml
# .nightly/config.yml
compact:
  enabled: true
  context_token_cap: 256000   # threshold fire; bleed-over fine
```

The config is read via a new `load_compact_config(root)` helper in
`nightly_core.config`, surfaced through `nightly status` and
`nightly doctor` for visibility.

**6. Compaction-aware re-reads via existing Read tool.** After
compaction, if the agent needs the verbatim text of a file again
(e.g. to apply Edit), it Re-reads via the normal Read tool. No
special "rehydrate from disk" primitive. The CLAUDE.md /
brainstorm.html / RFC contents are all on disk and re-readable.

**7. Doctor extension: require `/compact` token in Claude's
skill.md.** RFC 005 §B4's `_REQUIRED_SKILL_TOKENS` mechanism gains
an entry for `/compact` scoped to the Claude host only (the other
five hosts get nothing — the token check there is no-op). A stale
Claude skill that's missing the compact trigger surfaces in
`nightly doctor` as drift and re-installs.

**8. Briefing surfaces compaction.** The briefing's "Session
narrative" section gets a one-line note when compaction fired,
just so the operator's morning review can see that the
context-handling worked. Optional; if the compact happens but the
narrative doesn't mention it, no harm done.

## Risks

- **Compaction discards a fact the agent needs later.** Claude Code's
  `/compact` summarizes aggressively — facts the agent has not yet
  acted on may be dropped. Mitigation: the trigger fires *after*
  the RFC is on disk, so the load-bearing decisions are already
  persisted as planning artifacts. The agent re-reads them on
  cascade walk anyway.

- **Operator's seed gets lost in the summary.** If the operator
  typed `/nightly add a dashboard for vault stats` and the agent
  compacted before transitioning to the implementation loop, the
  literal seed text might not survive the summary. Mitigation: the
  seed-rfc CLI persisted the title to disk (as `title:` in the
  RFC frontmatter) before compaction, so the seed text is
  recoverable.

- **Per-host drift.** If Claude Code renames `/compact` or
  deprecates it, the skill text breaks silently — the agent will
  invoke a dead command and the compaction won't happen.
  Mitigation: the doctor's content check (Resolved #7) catches a
  stale Claude skill; the user re-runs `nightly doctor` which
  re-installs from the package's current skill.md. A version
  upgrade path is the canonical fix.

- **Compaction loop.** If the trigger fires unconditionally and
  the agent gets re-invoked into the prep stage (e.g. after a
  keep-alive force-continue), it could compact twice in a row.
  Mitigation: `/compact` on an already-compact buffer is cheap and
  idempotent — the second invocation does nothing useful but
  doesn't break anything. v1 doesn't add a sentinel; if double-fire
  proves real-world annoying, v2 can stamp a `.nightly/runs/<id>/
  COMPACTED` marker the skill checks.

- **Threshold-fire token estimate is imprecise.** Without
  programmatic access to the exact context-window count, the agent
  estimates based on visible signals (file reads, dispatches,
  conversation depth). Estimate skew can mean the trigger fires
  too early (cheap but interrupts a planning chain) or too late
  (the model's own auto-compact may kick in first). Mitigation:
  the default cap (256k) is conservative against a 1M ceiling, so
  "too late" mostly produces tolerable bleed-over rather than a
  hard-truncation event. Operators with cost-sensitive accounts
  can lower the cap; operators with reasoning-heavy workloads can
  raise it. The bias is operator-tunable.

- **Aggressive threshold cap on small workloads.** Setting the
  cap too low (e.g. 32k) would compact every few turns and lose
  recent working memory. Mitigation: config doc explains the cap
  should be set against the *model's* effective context window,
  not the *task's* needs. The 256k default is a sane floor.

## Implementation phases

Three phases, ~5h total.

### Phase A — Boundary fire: Claude Code skill + doctor check (~2h)

- **A1.** Update `packages/nightly-host-claude/src/nightly_host_claude/
  skill.md` to add the boundary trigger paragraph (Resolved #2's
  text) in the Invocation section, between the "Arm the keep-alive"
  block and "## Check for updates."
- **A2.** Extend `nightly_core.doctor._REQUIRED_SKILL_TOKENS` with
  a per-host entry: Claude's main skill must contain the literal
  string `/compact`. Drift triggers `nightly doctor` repair.
- **A3.** Tests: skill text contains the trigger (read the packaged
  `skill_md` constant); doctor flags a Claude install whose skill
  lacks `/compact`; doctor leaves non-Claude hosts unaffected.

**Merge gate for Phase A:** trigger paragraph + token-check both
land; existing 23 update tests + 46 doctor tests still pass.

### Phase B — Five-host no-op documentation + briefing note (~1h)

- **B1.** Each of `nightly-host-codex` / `-cursor` / `-gemini` /
  `-antigravity` / `-opencode` `skill.md` gains a one-paragraph
  note under its Invocation section: "This host does not support
  session compaction yet — skip the compact step Claude Code's
  skill describes."
- **B2.** `briefing.py` adds a "Compacted: yes/no" slot to the
  Session narrative section. The agent fills it in if compaction
  fired; default omitted.
- **B3.** Tests: briefing renders correctly with and without the
  compact slot; five hosts' skill text contains the no-op note.

**Merge gate for Phase B:** Phase A merged; per-host skill drift
caught by doctor on Claude; briefing renders without regression.

### Phase C — Threshold fire + config schema (~2h)

- **C1.** New `compact:` block in `.nightly/config.yml` (default
  written by `nightly init` / `nightly doctor`). Fields: `enabled:
  true`, `context_token_cap: 256000`.
- **C2.** `nightly_core.config.load_compact_config(root) ->
  CompactConfig` (dataclass: `enabled: bool`, `context_token_cap:
  int`). Mirrors the shape of `load_worktree_config` /
  `load_pr_feedback_config`.
- **C3.** Claude skill gains the threshold-fire paragraph
  (Resolved #2a): the agent estimates its own context at every
  `nightly next` boundary and invokes `/compact` when the estimate
  exceeds the cap. Skill text includes the rough estimator
  heuristics (count of file reads + specialist dispatches + RFC
  body bytes / 4 ≈ token estimate; conservative bias).
- **C4.** `nightly status` and `nightly doctor` print the
  configured cap + enabled state alongside the existing config
  surface so the operator can see it.
- **C5.** `_REQUIRED_SKILL_TOKENS` gains a second token check on
  Claude: the skill must contain the literal `context_token_cap`
  so the threshold-fire paragraph survives drift.
- **C6.** Tests: config loads with defaults when block is absent;
  custom cap is honored; `enabled: false` short-circuits both
  triggers; doctor token check fires for stale Claude skill;
  `nightly status` surfaces the cap.

**Merge gate for Phase C:** Phases A + B merged; config helper
matches existing per-feature config patterns; doctor + status
surface the cap.

## Sized checklist

**Phase A — Boundary fire: Claude Code skill + doctor check**
- [x] A1. Boundary trigger paragraph added to `packages/nightly-host-claude/src/nightly_host_claude/skill.md`
- [x] A2. `_REQUIRED_SKILL_TOKENS` extended with `/compact` scoped to Claude
- [x] A3. Tests in `test_doctor.py` for the drift check + skill text presence

**Phase B — Five-host no-op + briefing note**
- [ ] B1. No-op paragraph added to codex / cursor / gemini / antigravity / opencode skill.md
- [ ] B2. Briefing template gains optional "Compacted" line
- [ ] B3. Tests covering both branches

**Phase C — Threshold fire + config schema**
- [ ] C1. `compact:` block (`enabled: true`, `context_token_cap: 256000`) added to default `.nightly/config.yml` template in `init` + `doctor`
- [ ] C2. `load_compact_config(root) -> CompactConfig` helper in `nightly_core.config`
- [ ] C3. Threshold-fire paragraph added to Claude skill (estimator heuristic + cap reference)
- [ ] C4. `nightly status` + `nightly doctor` surface the cap
- [ ] C5. `_REQUIRED_SKILL_TOKENS` gains `context_token_cap` check on Claude
- [ ] C6. Tests covering config defaults, override, `enabled: false`, doctor drift, status output
