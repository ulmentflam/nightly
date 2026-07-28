---
status: accepted
sized: true
title: Model-tier routing for cost-aware specialist dispatch
created: 2026-06-04
sized_on: 2026-06-04
accepted_on: 2026-06-04
amended_on: 2026-07-28
author: nightly-seed
source: interactive_seed
estimated_effort: ~7h across 3 phases
phase_a: implemented
---

# RFC 007 — Model-tier routing for cost-aware specialist dispatch

## Status

`accepted` — operator seed in the 2026-06-04 interactive session.
Three tiers (`lite` / `coding` / `reasoning`) with per-host model
maps in `.nightly/config.yml` and a `model_tier` field that
specialist roles default-set and plan frontmatter can override.
Phase A lands the config schema + the tier-resolution helper;
Phase B threads the tier through the dispatch + Task-tool surfaces
on each host; Phase C wires defaults into the specialist registry
and adds the auto-tag heuristics for doc-only / briefing-only
tasks.

## Context

Nightly currently dispatches every specialist (implementer, tester,
reviewer, researcher) with whatever model the host's CLI is
configured to use. On Claude Code that's Opus 4.7 by default —
$15 per million input tokens, $75 per million output, the
top-tier reasoning model. For a documentation pass that's editing
README bullets, that's massive overpayment. For a tester run that's
asserting `assert result.exit_code == 0`, it's also overpayment.
For a complex multi-file refactor, Opus earns its rate.

Three of Anthropic's currently shipping models bracket the cost /
capability tradeoff cleanly:

| Tier | Claude | Notes |
|------|--------|-------|
| Lite | `claude-haiku-4-5` (200K ctx) | file search, summarization, docs, narrative |
| Coding | `claude-sonnet-5` (1M ctx) | implementation + test authoring |
| Reasoning | `claude-opus-5` (1M ctx) | orchestration, result validation, merge adjudication |

*(Amended 2026-07-28. The original table listed Opus 4.7 / Sonnet 4.6 /
Haiku 4.5 alongside speculative OpenAI and Google rows. Only the Claude
column ships as a default: those are ids Nightly can state
authoritatively. Other vendors' ids are operator-supplied — the schema
accepts any string, and an unbound host falls through to its CLI's own
default model rather than hard-failing on a guessed id.)*

The tier mapping is host-specific because each provider's lineup is
different — Claude has 1M-context variants that bump Opus into the
reasoning tier even at older sub-versions; OpenAI's reasoning model
is a distinct slot from its chat model; Google has Pro variants
versus Flash variants. The config carries the mapping.

The cascade and the specialist registry already know enough about
task shape to choose a tier:

- `nightly specialist implementer` — coding tier
- `nightly specialist tester` — coding tier
- `nightly specialist reviewer` — coding tier (could be lite for
  trivial diffs; conservative default is coding)
- `nightly specialist researcher` — reasoning tier
- Plan body says "audit-only" / "doc-only" / "briefing-only" →
  lite tier
- Plan frontmatter declares `model_tier: reasoning` → override

Routing by tier is opt-in at the per-task level (operator or agent
can override) and centralized at the per-host level (one config
block per repo, not duplicated across hosts).

## Non-goals

- **Per-model fine-grained routing.** The tier abstraction stops
  at 3 levels. We don't expose "use Sonnet 4.6 specifically for
  Python edits but Opus for Rust" — that's the kind of policy
  that drifts as models evolve.
- **Cross-host model fallback.** If Claude Opus 4.7 is unavailable
  the dispatch fails; we don't transparently fall over to Gemini
  3.5 Pro. Cross-vendor failover is a v3 concern at best.
- **Cost telemetry / budgeting.** This RFC routes by tier; it
  doesn't track $-spent or enforce a budget. Budget caps are a
  separate concern (could be a future RFC) that benefits from
  this tier substrate but doesn't require it.
- **Plan-time auto-classification via heuristics.** The agent
  picks the tier when scoping a task, the same way it picks
  `depends_on_pr` (RFC 004) and the seed-rfc vs single-task
  pathway (RFC 005). We don't add a model-classifier that
  inspects the plan body programmatically.
- **Headless `nightly run` overrides.** `nightly run` can pass
  `--host` today; we don't add `--tier` to override per
  invocation. The per-plan frontmatter is the override surface.
- **Tier 0 / "freebie" tier.** Some providers expose a free or
  very-cheap tier (Haiku 4.5 Free, Gemini Flash Free). The
  config block tolerates whatever model id the operator points
  at, so they can wire it manually; we don't pre-package a
  zero-cost preset.

## Proposed direction

Three approaches; **Approach C** ships as v1.

---

### A — Per-task tier field, no defaults

Every plan must declare `model_tier:` in frontmatter; specialists
read it and pass the right model to the host. No defaults — if
absent, the dispatch fails with a clear error.

**Pros:**
- Explicit. Every dispatch knows exactly which tier it's on.
- Forces the operator (or seed-rfc author) to think about cost
  per task.

**Cons:**
- Onerous. Every existing plan would need to be retrofitted.
- Defeats the agent's natural judgment — the implementer specialist
  is *always* coding-tier; making the plan declare it is paperwork.
- Headless `nightly run` against a fresh seed couldn't dispatch
  until the agent's first edit pass added the tier.

---

### B — Specialist-role-only routing

Each specialist role has a hardcoded tier (implementer = coding,
researcher = reasoning, etc). Plan frontmatter can't override.
Config maps tier → host-specific model id.

**Pros:**
- Simple. Every dispatch resolves through one lookup table.
- Zero new fields in plan frontmatter.

**Cons:**
- Inflexible. A "fix the README typo" task dispatched through the
  implementer specialist would still hit the coding tier; we'd be
  paying Sonnet rates for a one-line doc fix.
- The reverse case (a "refactor the entire pipeline" task that
  needs reasoning-tier even though it's dispatched through
  implementer) has no escape hatch.
- Conflates *role* (what specialist sub-agent) with *complexity*
  (how much model horsepower the task needs).

---

### C — Specialist defaults + plan-frontmatter override

The specialist registry sets a default tier per role (implementer
→ coding, researcher → reasoning, etc.). Plan frontmatter can
override via `model_tier: lite | coding | reasoning`. The agent
sets the override when scoping the plan — same pattern as
`depends_on_pr`, `proposer_fingerprint`, and `source` on existing
RFCs.

**Pros:**
- Reasonable default for every existing plan: implementer → coding,
  no field needed.
- Explicit escape hatch when judgment differs: an implementer task
  that's "fix one typo in README" gets `model_tier: lite`.
- Mirrors the existing plan-frontmatter / specialist-default
  layering. New plan field, no new specialist role.
- Plays well with seed-rfc: the agent's first Edit pass on a
  seed-stub can add `model_tier:` if the title makes the tier
  obvious ("Polish the README" → lite; "Refactor the cascade" →
  reasoning).

**Cons:**
- Agent judgment is fuzzy at the borderline. A "Refactor the test
  suite" task may sit between coding and reasoning; the agent
  picks one and we accept the occasional mis-pick.
- Two layers (specialist default + plan override) means the
  resolution logic has to be documented carefully. Tested below.

---

## Resolved technical decisions

**1. Approach C ships as v1.** Approach A was rejected because the
mandatory field defeats the agent's natural judgment for the
common case. Approach B was rejected because the role-only
mapping can't handle role/complexity divergence (the trivial
implementer task; the reasoning-needing audit). Approach C
preserves defaults for the common case and lets the agent override
when it matters.

**2. Three tiers: `lite`, `coding`, `reasoning`.** Named after the
*task complexity*, not the provider. Maps to whatever the
operator's host vendor sells in that complexity band. Stable
abstraction even when individual model ids churn (Sonnet 4.6 →
Sonnet 4.7 doesn't break the config; only the model id under
`coding:` changes).

**3. Per-host config block.** `.nightly/config.yml` gains a
`model_tiers:` map keyed by host id, with each host's three tiers
pointing at concrete model ids:

```yaml
model_tiers:
  claude:
    lite:       claude-haiku-4-5
    coding:     claude-sonnet-4-6
    reasoning:  claude-opus-4-7
  codex:
    lite:       gpt-5-mini
    coding:     gpt-5
    reasoning:  gpt-5-reasoning
  cursor:
    lite:       claude-haiku-4-5
    coding:     claude-sonnet-4-6
    reasoning:  claude-opus-4-7
  gemini:
    lite:       gemini-2.5-flash
    coding:     gemini-2.5-pro
    reasoning:  gemini-3.5-pro
  antigravity:
    lite:       gemini-2.5-flash
    coding:     gemini-2.5-pro
    reasoning:  gemini-3.5-pro
  opencode:
    lite:       claude-haiku-4-5
    coding:     claude-sonnet-4-6
    reasoning:  claude-opus-4-7
```

`nightly init` and `nightly doctor` write this default block.
Operators override per-host without changing the schema.

**4. Specialist defaults — table (AMENDED 2026-07-28):** Existing
`SpecialistRole` literal in `nightly_core.contract` gains a parallel
`SPECIALIST_TIER_DEFAULTS: dict[SpecialistRole, ModelTier]` table:

| Role | Default tier | Why |
|------|--------------|-----|
| `implementer` | `coding` | output is gated by `nightly verify` |
| `tester` | `coding` | same gate |
| `reviewer` | `reasoning` | result validation — nothing downstream re-checks it |
| `researcher` | `lite` | file search + summarization over code already on disk |

The original draft paired role with *seniority*: reviewer sat at
`coding` because it was "part of the implementation cycle," and
researcher at `reasoning` because research sounded hard. Both were
wrong, and for the same reason — the axis that matters is not how
senior the role sounds, it is **how expensive it is to be wrong and
not notice**.

- The reviewer is the last judgment call before a diff becomes a PR.
  A lite-tier mis-approval is caught by nothing: lint, types, and
  tests all already passed by the time review runs, which is exactly
  why review exists. Original Risk item "lite-tier reviewer
  mis-approves a bad diff" identified this hazard and then mitigated
  it with `coding` — half a step. `reasoning` is the whole step.
- The researcher, despite the name, does file search and
  summarization against a codebase already on disk. It is
  high-volume reading and low-stakes synthesis: the role that
  benefits *least* from deliberation and is cheapest to run wide.
- Implementer and tester stay at `coding`. Their output passes
  through `nightly verify` before it can reach a PR, so a cheap
  model's mistakes surface mechanically rather than silently.

Future roles add an entry to the table; `tier_for_role` falls back
to `coding` for any role without one, so a new role degrades to
today's behavior rather than silently routing to lite.

**5. Plan frontmatter field: `model_tier`.** New constant
`MODEL_TIER_KEY = "model_tier"` in `nightly_core.plans` alongside
`PROPOSER_FINGERPRINT_KEY` and `DEPENDS_ON_PR_KEY`. New
`PlanRecord.model_tier: ModelTier | None` property: returns the
parsed tier or `None` when absent / malformed. `None` means "use
the specialist default."

**6. Resolution order at dispatch time:**
1. If the plan declares `model_tier: <tier>`, use that.
2. Otherwise, look up the specialist role in
   `SPECIALIST_TIER_DEFAULTS`.
3. The host integration's tier resolver maps tier → model id via
   the per-host config block.
4. If the host has no entry for the tier (older config without
   `model_tiers`), fall back to the host's default model (today's
   behavior) with a warning surfaced in the briefing's "Friction
   caught" section.

**7. Auto-tag for trivial roles.** Two task-shape patterns get an
*automatic* lite-tier override at scoping time, baked into the
skill text:
- The task's deliverable is a markdown file only (briefing,
  README edit, RFC body fill-in, lessons doc) → `model_tier: lite`.
- The plan body's "File scope" lists only files ending in `.md`,
  `.txt`, `.html` → `model_tier: lite`.

The agent applies this rule when scoping. It's documented in
each host's skill, mirroring RFC 005's seed-vs-task heuristic.

**8. Cost note in the morning briefing.** The briefing's Session
narrative section gains a one-line tier breakdown:
"Dispatches by tier: lite × 3, coding × 5, reasoning × 1." Lets
the operator see at a glance whether routing is working.

**9. Tier-routing applies to both `nightly dispatch start` (the
default for interactive sessions) and the Task tool fallback.**
The agent's Task-tool sub-agent invocation also reads the tier
and selects the right model id. Hosts that don't expose a
model-selection knob on their Task-tool surface (Codex, opencode)
fall back to their default model and surface a friction note.

**10. CLI surface: `nightly specialist <role> --tier <tier>`** lets
the agent see the system prompt scoped to a tier (today the
command takes only `<role>`). The `--tier` flag is optional; with
no flag, the role's default tier is used. The system prompt itself
doesn't change between tiers — same role-specific instructions —
but the dispatch invocation that follows reads the tier for model
selection.

**11. Reasoning effort is a per-tier dial (ADDED 2026-07-28).** The
`model_tiers:` block gains an `effort:` sub-map binding each tier to a
reasoning-effort level:

| Tier | Effort | Rationale |
|------|--------|-----------|
| `lite` | `low` | read and summarize; do not deliberate |
| `coding` | `low` | write the files; `nightly verify` is the check |
| `reasoning` | `xhigh` | the tier whose whole job is judgment |

Model choice alone under-delivers on the cost goal. A fast model run
at high effort spends its savings on preamble and exploratory tool
calls — the exact behavior the lite and coding tiers are meant to
avoid. Lower effort yields fewer, more-consolidated tool calls and
less preamble, which is what "more writing, less thinking" means
operationally.

Effort is injected as prompt-preamble text rather than as a
vendor-specific CLI flag. That is deliberate: the flag surface differs
per host and some hosts have none, whereas prompt text works
everywhere and degrades to a no-op on a model that ignores it. A
future phase can upgrade specific hosts to a native flag without
changing the config schema.

**12. Model controls are discovered, not declared (ADDED 2026-07-28).**
`nightly init` probes each installed host CLI's `--help` for its
model-selection flag and its advertised model vocabulary, then ranks that
vocabulary into the three tiers and writes the result to
`model_tiers.<host>.flag` and `model_tiers.<host>.<tier>`.

The original plan had Nightly carry a per-host flag table. That table is
wrong the day a vendor renames a flag, and a wrong flag is a hard spawn
failure in the middle of an unattended run — whereas the right one is
readable from the CLI in milliseconds. Discovery immediately found
`--model` on opencode and gemini, neither of which had been verified by
hand when the defaults were written.

Two rules keep discovery safe:

- **It can only add certainty.** Probe results merge over the seeded
  defaults; any probe failure degrades to the seeded template, so `init`
  can never fail because a host CLI misbehaved.
- **Pinned beats floating.** A discovered *pinned* id (`claude-opus-5`)
  overrides a seeded default, but a bare alias (`opus`) does not. Aliases
  resolve to whatever shipped most recently — convenient interactively,
  wrong for an overnight run whose model should still be identifiable in
  the morning.

Host coverage spans all seven major harnesses (Claude, Codex, Cursor,
Gemini, OpenCode, Pi, Hermes) plus Antigravity. `pi` and `hermes` are
recognized at the routing layer only — they ship no integration package,
so skill install and keep-alive hooks are unavailable for them.

## Risks

- **Tier mis-pick at the borderline.** Agent judgment will
  sometimes route a coding task to lite or a lite task to
  reasoning. Mitigation: the briefing's tier breakdown (Resolved
  #8) surfaces unusual patterns; the operator can review and
  flip the plan's frontmatter for a re-dispatch. Bias remains
  "default to the specialist's tier" — overrides only when the
  agent has high confidence.

- **Stale config after a model deprecation.** If Anthropic
  deprecates Haiku 4.5 in favor of Haiku 5.0, configs still
  pointing at the old id will fail at dispatch time. Mitigation:
  `nightly doctor` gains a future check that pings each
  configured model id; the immediate failure mode is "dispatch
  raises" which surfaces in the briefing.

- **Host-side rate limits / billing caps.** Switching to lite tier
  for the bulk of doc work could trip the host's rate limit if
  it's per-model rather than per-account. Mitigation: out of
  scope for this RFC; documented as a known gap with a pointer
  to the host's billing dashboard.

- **Conflict with future budget feature.** A separate budget RFC
  will need to read the tier for cost estimation. We name the
  config block `model_tiers:` (not `models:`) to keep the noun
  composable with a `budget:` block later.

- **Lite-tier reviewer mis-approves a bad diff.** *(Resolved by the
  2026-07-28 amendment.)* A reviewer dispatched on a lite model may
  LGTM a diff a reasoning model would have rejected, and nothing
  downstream catches it. Mitigation is now structural: the reviewer
  default is `reasoning` (Resolved #4), and the auto-tag rule
  (Resolved #7) does not apply to reviewer dispatches.

- **Reasoning-tier cost concentration.** Moving reviewer to
  `reasoning` raises per-review cost. Mitigation: RFC 012's
  `parallelism.per_tier.reasoning` cap (default 2) bounds how many
  reasoning dispatches can run at once, so the wide fleet stays wide
  in the cheap tiers and scarce in the expensive one. The tier
  breakdown in the briefing (Resolved #8) surfaces the mix.

- **`nightly verify` runs across all tiers.** The lint / type /
  test gates don't care which tier produced the code, but if a
  lite-tier implementer ships sloppier code, `nightly verify`
  catches it before PR. The cost saving + the existing gate make
  the routing safe by construction.

## Implementation phases

Three phases, ~7h total.

### Phase A — Config schema + tier resolver (~3h)

- **A1.** New `ModelTier` literal in `nightly_core.contract`:
  `Literal["lite", "coding", "reasoning"]`.
- **A2.** New `MODEL_TIER_KEY = "model_tier"` constant +
  `PlanRecord.model_tier: ModelTier | None` property in
  `nightly_core.plans`. Same shape as the existing
  `proposer_fingerprint` / `depends_on_pr` accessors.
- **A3.** New `model_tiers:` block in the default config template
  (in `cli._DEFAULT_CONFIG_YML` and `doctor._DEFAULT_CONFIG_YML`)
  per Resolved #3.
- **A4.** New `load_model_tier_config(root) -> ModelTierConfig`
  helper in `nightly_core.config` (dataclass with per-host
  `dict[HostId, dict[ModelTier, str]]`). Mirrors
  `load_worktree_config` shape.
- **A5.** New `SPECIALIST_TIER_DEFAULTS` table in
  `nightly_core.specialists`.
- **A6.** New `resolve_model_for_task(plan, host_id, role, config)
  -> str | None` helper that implements Resolved #6's resolution
  order. Returns `None` when no model can be resolved (host
  missing from config; falls back to host default).
- **A7.** Unit tests: every resolution branch (plan override,
  specialist default, host miss, malformed plan field).

**Merge gate for Phase A:** all unit tests pass; existing 23
update / 46 doctor / etc tests still green; config schema parses.

### Phase B — Dispatch integration (~3h)

- **B1.** `nightly dispatch start --role <role>` reads
  `resolve_model_for_task` and passes the resolved model id to the
  host's headless CLI (Claude Code's `--model` flag, Codex CLI's
  `--model`, etc.).
- **B2.** Task-tool fallback (interactive `/nightly` mode) — the
  skill text on each host gains instructions for picking the model
  id when dispatching via the Task tool. Per-host syntax differs
  (Claude Code accepts `model: <id>` in the Task tool's args).
- **B3.** Briefing's Session narrative gains the tier breakdown
  line (Resolved #8). Computed from `dispatch.json` per-task
  records.
- **B4.** `nightly specialist <role> --tier <tier>` CLI surface
  (Resolved #10).
- **B5.** Tests: end-to-end dispatch with a plan declaring
  `model_tier: lite` routes to the lite model id; default plan
  routes to the role's default tier's model id; host with no
  tier config falls back to default with a logged warning.

**Merge gate for Phase B:** Phase A merged; dispatch + briefing
integration tested; six host skills updated for Task-tool
fallback.

### Phase C — Auto-tag heuristic + doctor + docs (~1h)

- **C1.** Skill paragraph on each host: "When scoping a plan
  whose deliverable is markdown-only (briefing, RFC body, README
  edits, lessons), set `model_tier: lite` in the plan
  frontmatter. When scoping a multi-file refactor / architecture
  change / long-running investigation, set `model_tier:
  reasoning`. Otherwise rely on the specialist default (typically
  `coding`)."
- **C2.** `nightly doctor` checks that each installed host's
  config has a `model_tiers` block; flags missing block as drift.
- **C3.** README "Cost-aware dispatch" section: one paragraph
  explaining the tiers, the config knob, and the auto-tag rule.

**Merge gate for Phase C:** Phases A + B merged; doctor surfaces
missing config; README updated.

## Sized checklist

**Phase A — Config schema + tier resolver** — *implemented 2026-07-28*
- [x] A1. `ModelTier` literal in `nightly_core.contract` (+ `MODEL_TIERS`
      tuple and the `ReasoningEffort` literal from Resolved #11)
- [x] A2. `MODEL_TIER_KEY` + `PlanRecord.model_tier` accessor
- [x] A3. `model_tiers:` default config block — written to the single
      consolidated `DEFAULT_CONFIG_YML` in `nightly_core.config`
- [x] A4. `load_model_tier_config` helper (merge-over-defaults semantics)
- [x] A5. `SPECIALIST_TIER_DEFAULTS` table + `tier_for_role`
- [x] A6. `resolve_model_for_task` helper — lives in the new
      `nightly_core.routing` module alongside the RFC 012 threshold
      resolver, since both answer "what budget does this dispatch get?"
- [x] A7. Unit tests covering all resolution branches
      (`tests/test_routing.py`, 49 cases)
- [x] A8. *(unplanned)* `nightly doctor` advisory check for hosts with no
      tier binding — pulled forward from Phase C's C2 because the
      reviewer-tier change makes a silently-inert routing config more
      consequential than it was when C2 was scheduled.

**Phase B — Dispatch integration** — *core landed 2026-07-28; B2/B3 open*
- [x] B1. `nightly dispatch start` reads resolved model id and passes it
      with the **discovered** model flag (see B6)
- [ ] B2. Task-tool fallback documented across six host skill.md
- [ ] B3. Briefing tier-breakdown line
- [x] B4. `nightly specialist --tier <tier>` flag
- [x] B5. Dispatch resolution tests across tiers + host-miss fallback
      (`tests/test_routing.py`); end-to-end argv assertions still open
- [x] B6. *(unplanned, supersedes part of B1)* `nightly init` **discovers**
      each host's model-selection flag and model vocabulary from the host
      CLI's own `--help`, rather than Nightly carrying a vendor table.
      See `nightly_core.model_probe` and RFC 007 Resolved #12.

**Phase C — Auto-tag heuristic + doctor + docs**
- [ ] C1. Auto-tag scoping paragraph on six host skills
- [ ] C2. Doctor flags missing `model_tiers` block
- [ ] C3. README "Cost-aware dispatch" section
