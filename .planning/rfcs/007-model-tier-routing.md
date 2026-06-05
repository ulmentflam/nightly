---
status: accepted
sized: true
title: Model-tier routing for cost-aware specialist dispatch
created: 2026-06-04
sized_on: 2026-06-04
accepted_on: 2026-06-04
author: nightly-seed
source: interactive_seed
estimated_effort: ~7h across 3 phases
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

| Tier | Claude | OpenAI | Google | Notes |
|------|--------|--------|--------|-------|
| Lite | Haiku 4.5 | GPT-5-mini | Gemini 2.5 Flash | docs, formatting, narrative |
| Coding | Sonnet 4.6 | GPT-5 | Gemini 2.5 Pro | implementer / tester / reviewer |
| Reasoning | Opus 4.7 (1M) | GPT-5 Reasoning | Gemini 3.5 Pro | architecture, long refactors |

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

**4. Specialist defaults — table:** Existing `SpecialistRole`
literal in `nightly_core.contract` gains a parallel `SPECIALIST_TIER_DEFAULTS:
dict[SpecialistRole, ModelTier]` table:

| Role | Default tier |
|------|--------------|
| `implementer` | `coding` |
| `tester` | `coding` |
| `reviewer` | `coding` |
| `researcher` | `reasoning` |

Future roles add an entry to the table. The `coding` default for
the three implementation-cycle roles preserves today's behavior:
nothing routes to lite or reasoning automatically — the agent must
opt in via plan frontmatter.

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

- **Lite-tier reviewer mis-approves a bad diff.** A reviewer
  dispatched on Haiku may LGTM a diff Opus would have rejected.
  Mitigation: reviewer default is `coding`, not `lite` (Resolved
  #4). The auto-tag rule (Resolved #7) doesn't apply to reviewer
  outputs.

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

**Phase A — Config schema + tier resolver**
- [ ] A1. `ModelTier` literal in `nightly_core.contract`
- [ ] A2. `MODEL_TIER_KEY` + `PlanRecord.model_tier` accessor
- [ ] A3. `model_tiers:` default config block
- [ ] A4. `load_model_tier_config` helper
- [ ] A5. `SPECIALIST_TIER_DEFAULTS` table
- [ ] A6. `resolve_model_for_task` helper
- [ ] A7. Unit tests covering all resolution branches

**Phase B — Dispatch integration**
- [ ] B1. `nightly dispatch start` reads resolved model id
- [ ] B2. Task-tool fallback documented across six host skill.md
- [ ] B3. Briefing tier-breakdown line
- [ ] B4. `nightly specialist --tier <tier>` flag
- [ ] B5. End-to-end dispatch tests across tiers + fallback

**Phase C — Auto-tag heuristic + doctor + docs**
- [ ] C1. Auto-tag scoping paragraph on six host skills
- [ ] C2. Doctor flags missing `model_tiers` block
- [ ] C3. README "Cost-aware dispatch" section
