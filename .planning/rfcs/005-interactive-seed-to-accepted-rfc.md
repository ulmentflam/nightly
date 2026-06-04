---
status: accepted
sized: true
title: Interactive seed → priority accepted RFC (planning-first seed pathway)
created: 2026-06-04
sized_on: 2026-06-04
accepted_on: 2026-06-04
author: ulmentflam
estimated_effort: ~5h across 3 phases
---

# RFC 005 — Interactive seed → priority accepted RFC

## Status

`accepted` — direction agreed in the 2026-06-04 interactive design
session; sizing closed in the same pass. The selected shape is
**Approach C** (CLI helper + agent judgment from seed shape +
standard `accepted_rfc` cascade slot). Approaches A (skill-only) and
B (always-RFC for any interactive invocation) are documented below
for the record. Phase A lands the CLI primitive and the frontmatter
contract; Phase B threads the trigger into each host's skill;
Phase C characterizes against a representative feature seed and
captures the first dogfood confirmation.

## Context

When the operator invokes `/nightly` interactively — either with an
explicit seed (`/nightly add a dashboard for vault stats`) or after a
back-and-forth conversation that has surfaced a feature need — that
seed currently becomes a single task at `tasks/0001-<slug>/plan.md`
with `status: ready`. The agent's first move is `ready → in_progress`
and the cascade takes over after the seed task lands.

This worked when seeds were one-line bugfixes ("fix the login bug").
It's a poor fit when the seed describes a *feature*: a feature seed
fans out into multiple sub-tasks (design, implement, test, document)
that the current single-task seed cannot represent. The cascade has
no mechanism to plan-then-execute on the seed; it can only pick up
the single task, and the agent has to either cram everything into
one task (which violates file-scope refusal-policy guarantees) or
improvise mid-stream (which produces uneven artifacts the morning
briefing struggles to summarize).

Meanwhile, the repo already has a planning surface that *is* well
suited to multi-step feature work: `.planning/rfcs/`. RFCs 001–004
each describe a feature, list resolved decisions, and end with a
sized checklist of unchecked `- [ ]` items. The `accepted_rfc`
cascade step (priority 3, ahead of GitHub issues, PR rescue, and
ideation) picks unchecked items one at a time — exactly the shape
needed for fanning out a feature seed into durable, reviewable work.

This RFC closes the gap: when `/nightly` is invoked with
feature-shape input, the seed lands as a new accepted RFC in
`.planning/rfcs/` rather than a single throwaway task. The cascade
then runs against the RFC's checklist exactly as it does today
against RFCs 001–004 — no cascade ordering changes, no new
statuses, no behavioral surprises.

## Non-goals

- **Replacing `nightly start <seed>` for one-line bugfixes.**
  Single-task seeds remain the right shape for "fix the typo." The
  new pathway is opt-in based on seed shape, not a wholesale
  replacement.
- **Auto-classifying seeds via heuristics or ML.** The trigger is
  *agent judgment* — the host skill's prompt describes the
  seed-shape test, and the agent decides. No regex match on the
  seed string, no token-count threshold in the CLI, no classifier
  model.
- **Pulling conversation transcripts via API.** The interactive
  session's conversation context lives in the host's chat buffer;
  only the agent has access to it. The CLI helper accepts a seed
  string the agent supplies; it never tries to read the host's chat
  state.
- **Changing the cascade order.** RFCs from this pathway land at
  the existing `accepted_rfc` cascade slot (step 3, after
  `resume_in_flight` and `unblocked_approval`). Earlier slots —
  including any older RFC with still-unchecked items — continue to
  outrank the new RFC, which matches the "finish what's started"
  invariant the cascade was built around.
- **A `nightly conclude-rfc` companion command.** Marking an RFC
  done is done by checking off its task-list items via normal
  editing as the cascade processes each item. No new lifecycle verb.
- **Cross-host coordination of RFC numbering.** Only one operator
  is running interactively in any given moment; the CLI numbers the
  new RFC by scanning `.planning/rfcs/` for the highest existing
  NNN and adding 1. No locking, no concurrent-invocation handling.
  Matches the existing single-process contract of `nightly run`.

## Proposed direction

Three approaches; **Approach C** ships as v1.

---

### A — Skill-only (no CLI change)

Update each host's `skill.md` to instruct the agent to draft an
`accepted` RFC file directly via the Write/Edit tool whenever a
feature-shape seed arrives. Zero Python changes; the agent computes
the next RFC number itself, picks a slug, writes the frontmatter,
and fills the body in one editor action.

**Pros:**
- Smallest diff. Touches only six markdown files (one per host) plus
  the canonical skill copy under `.claude/skills/nightly/SKILL.md`.
- No new public surface; the existing skill text fully captures the
  behavior.
- Each host can adapt the trigger language to its own idiom.

**Cons:**
- Computing the next RFC number from raw filesystem access is
  fragile. Six hosts implementing it independently will drift over
  time (RFC 004 §D4 already documented this drift hazard for the
  declared-dependency heuristic paragraph).
- The "write frontmatter exactly so the cascade can parse it"
  contract belongs in `nightly-core` next to `parse_frontmatter` /
  `render_frontmatter`, not in skill prose. A frontmatter typo in
  the skill becomes a silent cascade failure that surfaces as "the
  RFC sits unprocessed in `.planning/rfcs/` and nobody knows why."
- No `nightly run` (headless) parity. The headless driver has no
  conversation context today, but if a future caller (e.g. a
  triage-driven RFC seeder) wanted to materialize an RFC
  programmatically, there's no API surface to call.

---

### B — Always-RFC for every interactive invocation

Every `/nightly <seed>` and every `/nightly` (with prior context)
goes through the seed-RFC pathway. The current `nightly start
<seed>` task seeding is reserved for the headless `nightly run`
driver.

**Pros:**
- Simple rule for the host skill to encode: interactive → RFC,
  headless → task. No agent judgment required at invocation time.
- Every interactive invocation produces a durable `.planning/rfcs/`
  artifact, biasing toward auditable planning.

**Cons:**
- Heavy-weight for one-line bugfix seeds. "Fix the typo" doesn't
  need an RFC; the current single-task path is correct and
  produces less ceremony.
- Creates RFC churn — `.planning/rfcs/` accumulates many tiny RFCs
  whose only checklist item is "fix the bug," dragging the
  signal-to-noise ratio of the planning surface down.
- The operator's mental model of "RFCs describe features and design
  decisions, not bugfixes" gets diluted, which weakens the
  `accepted_rfc` cascade slot's signal that human-blessed scope
  lives here.

---

### C — CLI helper + agent judgment from seed shape

A new `nightly seed-rfc <title>` CLI command stubs an `accepted` RFC
file with the correct frontmatter and a skeleton body. The host's
`skill.md` instructs the agent to invoke this command on `/nightly`
when the seed (or the prior conversation context) describes a
feature or multi-step change, and to keep using `nightly start
<seed>` for one-line bugfix seeds. The agent fills the RFC body in
place after the stub lands.

**Pros:**
- Frontmatter contract lives in `nightly-core` where it's adjacent
  to `parse_frontmatter` and the cascade reader — one source of
  truth, no per-host drift.
- Agent judgment preserves the right shape for the right seed:
  heavy feature seeds → RFC; trivial bugfix seeds → task. Neither
  over- nor under-formalizes.
- Headless parity: `nightly run` and future programmatic callers
  (e.g. a triage-driven RFC seeder, or a `nightly verify` failure
  that wants to file a "fix the test suite" RFC) can invoke
  `seed-rfc` directly.
- The cascade's `accepted_rfc` step needs no changes — the new RFC
  appears as a higher-numbered file in `.planning/rfcs/` and the
  existing alphabetical/numeric walk picks it up after any
  unchecked items in older RFCs.

**Cons:**
- Agent judgment is fuzzy; the line between "feature seed" and
  "bugfix seed" is not crisp, so some seeds will land in the wrong
  shape. Mitigation: document the heuristic prominently in each
  host's `skill.md` (the same per-host discipline RFC 004 §D4 used
  for `depends_on_pr`).
- Adds one new CLI command. The Toolkit table in each `skill.md`
  grows by a row.
- Six host skill files need to be updated in lockstep. RFC 004 §D4
  already paid this cost; we pay it again here.

---

## Resolved technical decisions

**1. Approach C ships as v1.** Approach A was rejected because the
frontmatter contract belongs in `nightly-core`, not duplicated
across six skill files where it will drift. Approach B was rejected
because it over-formalizes trivial seeds and drags the
`.planning/rfcs/` signal-to-noise ratio down. Approach C preserves
seed-shape flexibility while keeping the contract in one place.

**2. Command name is `nightly seed-rfc`.** Reads as "seed an RFC."
Alternatives considered and rejected: `nightly rfc new` (verb-noun
ordering inconsistent with the rest of the CLI, which is mostly
verb-only); `nightly start --as-rfc` (overloads `start`'s existing
run-creation semantics, surprising); `nightly plan` (too generic —
`.planning/` already conflates with the per-task `plan.md`).

**3. Signature: `nightly seed-rfc <title> [--slug <slug>] [--source <verb>]`.**
- `<title>` is the human-readable RFC title (required, positional).
- `--slug` overrides the auto-derived kebab-case slug. Without it,
  the slug derives from `slugify(title)` (reused from `runs.py`).
- `--source` records which trigger fired (`interactive_seed`,
  `interactive_context`, or `headless`) for retro analytics. Defaults
  to `interactive_seed`; the agent supplies the right value based
  on whether the operator passed a seed string or the trigger came
  from prior conversation context.

**4. RFC numbering: scan `.planning/rfcs/`, take max NNN + 1.** No
locking, no reservation. Two concurrent operators is a pathological
case the CLI does not attempt to handle (matches the existing
`nightly run` single-process contract). The next RFC number is
derived once at stub-creation time and baked into the filename.
Defaults to 1 when the directory is empty (first-ever RFC).

**5. RFC stub shape mirrors RFCs 001–004.** Frontmatter:
- `status: accepted`
- `sized: false` — the agent fills in the sizing pass as it
  expands the body; flipping to `true` is a manual edit once the
  Sized checklist is committed
- `title: <title>`
- `created: <today>` and `accepted_on: <today>` — same date for
  interactive seeds, because the operator's invocation *is* the
  acceptance signal
- `author: nightly-seed` — distinguishes from human-authored RFCs
  so retro audits can filter on the origin
- `source: <verb>` — carries the `--source` value

Body skeleton: `## Status`, `## Context`, `## Non-goals`,
`## Proposed direction`, `## Resolved technical decisions`,
`## Risks`, `## Implementation phases`, `## Sized checklist` —
each with a `_TODO_` placeholder the agent overwrites in its first
Edit pass. The skeleton matches the eight sections that 001–004
converged on, so the cascade's `_RFC_UNCHECKED_RE` regex (which
matches top-level `- [ ]` items only) sees a well-formed file.

**6. Cascade-side: no changes.** `_find_accepted_rfc` already
returns the first unchecked `- [ ]` item from any accepted RFC,
processed in `sorted(rfcs.iterdir())` order — which for `NNN-slug.md`
filenames is numeric. New seed-RFCs land at the next NNN, so they
are picked *after* any older RFC with remaining unchecked items.
This preserves the "finish what you started" invariant the cascade
is built around. Top-of-cascade override was rejected because it
would break the invariant; created-date-desc sort was rejected
because it would re-prioritize RFCs every time a new seed landed,
complicating reasoning about cascade order. Standard slot — the
choice committed in the design session — is the cleanest fit.

**7. Trigger heuristic lives in `skill.md`, not the CLI.** The CLI
is unconditional — given a title, it writes the stub. The host's
`skill.md` gains a paragraph in the Invocation section:

> *"When the seed (or the prior interactive conversation) describes
> a feature, design change, or multi-step initiative, run
> `nightly seed-rfc "<distilled title>"` and immediately Edit the
> resulting RFC file to fill in Context, Resolved decisions, and
> Sized checklist. The cascade picks up the unchecked items on
> subsequent `nightly next` calls. For one-line bugfix seeds, keep
> using `nightly start <seed>` — the single-task pathway is still
> the right shape for one-shot work. When in doubt, prefer the
> lighter path (task) over the heavier path (RFC); a borderline
> seed that turns out to need RFC scope can be upgraded post-hoc
> by manually authoring the RFC and marking the stranded task
> done."*

This biases toward false negatives (a borderline seed lands as a
task rather than as an RFC) over false positives (a one-line
bugfix bloats `.planning/rfcs/` with a tiny RFC), consistent with
RFC 001 §Resolved-decisions-#2 / RFC 004 §Resolved-decisions-#6.

**8. Conversation-context distillation is the agent's job.** The
CLI never reads the host's chat buffer. When `/nightly` is invoked
without a seed but with prior conversation context, the agent
distills that context into a one-line title and passes it to
`nightly seed-rfc`. The agent is the only consumer with access to
the chat state; pushing the distillation into the CLI would require
a host-specific bridge for each of the six hosts and would couple
the CLI to host-vendor APIs that are not stable.

**9. `nightly start` runs unchanged.** The seed-RFC pathway does
not create a Nightly run. The current run (created by `nightly
start` or inherited from `runs/CURRENT`) holds tasks materialized
off the RFC's checklist via the existing `accepted_rfc` cascade
dispatch. RFC artifacts live in `.planning/`, run artifacts live
in `.nightly/runs/`; the two are intentionally disjoint, and
`seed-rfc` does not bridge them. If no run exists when `seed-rfc`
is called, the agent's next move is to invoke `nightly start`
itself (no seed argument) — the skill instructions cover this
ordering.

**10. No new `nightly task` flag.** Tasks materialized from a
seed-RFC's checklist use the same `nightly task <slug>` path as
tasks from any other accepted RFC. The plan body's "Source"
section quotes the RFC item text (the agent already does this
when scoping), and the cascade records the RFC path in its
rationale (existing behavior). No new `--from-rfc <NNN>` flag —
the link is captured via natural-language reference, not
structured frontmatter, matching how today's RFC-driven tasks
work.

**11. `accepted` for an auto-generated RFC is a deliberate
deviation from the human-acceptance precedent.** Historically
`.planning/rfcs/` carries human-authored RFCs that pass a sizing
and review pass before flipping to `accepted`. Auto-accepting RFCs
from the agent skips that explicit review. The justification: the
operator *just* authored the seed in the same interactive session
— they are the reviewer of record, and the act of invoking
`/nightly` with a seed is the acceptance signal. The
`author: nightly-seed` frontmatter field distinguishes these RFCs
from hand-authored ones so retro audits can filter on the origin.
If this proves problematic in practice (auto-acceptance produces
RFCs the operator wouldn't have approved on review), the v2
escape is a `--status proposed` flag on `seed-rfc` that lands the
RFC at `proposed` rather than `accepted`, deferring cascade pickup
until the operator flips the status manually.

## Risks

- **Borderline seeds land in the wrong shape.** A seed that hovers
  between "small feature" and "big bugfix" could go either way;
  the agent's judgment will sometimes pick the heavier path (RFC)
  for a seed that didn't need it, or the lighter path (task) for
  a seed that did. The skill text biases toward false negatives,
  so the common miss is "should have been an RFC, was a task" —
  the operator can upgrade post-hoc by manually authoring an RFC
  and marking the stranded task done. Acceptable in v1.

- **The auto-numbered RFC collides with a hand-authored one.** If
  the operator is mid-draft on `006-…` in a worktree and the agent
  invokes `nightly seed-rfc` on `main`, both end up at NNN=006.
  Mitigation: the convention documented in §11 is that the operator
  should not draft RFCs outside of an interactive `/nightly`
  session unless they have paused the agent. The CLI does not
  enforce this; collision-handling is a v2 concern.

- **`accepted` for an auto-generated RFC sets a precedent.** See
  Resolved decision #11. The frontmatter `author: nightly-seed`
  field is the audit trail; if auto-acceptance proves wrong, the
  v2 `--status proposed` flag is the escape hatch.

- **Skill drift across six hosts.** RFC 004 §D4 already documented
  that the six host skills can drift. The same risk applies here:
  if one host's `skill.md` gets out of step, that host will not
  invoke `seed-rfc` correctly. Mitigation: a single
  source-of-truth paragraph in this RFC (Resolved decision #7)
  that each host's update step copies verbatim, plus a
  `nightly doctor` check that the toolkit row is present in each
  host's skill file.

- **The dogfood pass cannot be characterized purely in unit
  tests.** Phase C's dogfood confirmation depends on the agent's
  actual judgment in a real interactive session, which a unit test
  cannot reproduce. The fallback is the morning briefing's
  narrative slot: the agent records whether the seed pathway
  triggered, and the operator reads it. Acceptable — RFC 004 §D
  used the same pattern (briefing-based confirmation rather than
  inline assertion).

## Implementation phases

Three phases, ~5h total. Phase A is the load-bearing piece (CLI
primitive + frontmatter contract); Phase B threads the trigger
into each host's skill; Phase C characterizes against a
representative feature seed and captures the first dogfood
confirmation. Each phase is independently mergeable — the seed-RFC
primitive works after Phase A even if Phases B–C never land
(operator can invoke it manually).

### Phase A — CLI helper + frontmatter contract (~2h)

- **A1.** New module `nightly_core/seed_rfc.py` with:
  - `next_rfc_number(root) -> int` — scans
    `.planning/rfcs/<NNN>-*.md`, parses leading NNN, returns
    `max + 1`. Defaults to 1 if the directory is empty or absent.
  - `RFC_FRONTMATTER_TEMPLATE: dict[str, str]` — the frontmatter
    shape used at stub time (status / sized / title / created /
    accepted_on / author / source).
  - `RFC_BODY_SKELETON: str` — the eight-section skeleton string
    with `_TODO_` placeholders. Matches the section ordering
    converged on in RFCs 001–004.
  - `write_seed_rfc(root, title, *, slug=None,
    source="interactive_seed", today=None) -> Path` — creates
    `.planning/rfcs/<NNN>-<slug>.md`, returns the absolute path.
    Uses `render_frontmatter` from `plans.py` so the frontmatter
    shape matches the cascade reader exactly. `today` is exposed
    for tests; production calls let it default to UTC now.
- **A2.** `nightly seed-rfc` Typer command in `cli.py` — positional
  `title`, `--slug`, `--source`. Prints the created path and a
  "→ next: edit the body to flesh out the Context and Sized
  checklist, then `nightly next` will pick the first unchecked
  item" hint.
- **A3.** Unit tests in
  `packages/nightly-core/tests/test_seed_rfc.py`:
  - empty `.planning/rfcs/` → NNN=001
  - existing 001–004 → NNN=005
  - non-NNN filenames in `.planning/rfcs/` are ignored
  - explicit `--slug` honored verbatim
  - auto-slug derives from title via `slugify` from `runs.py`
    (reuse, do not duplicate)
  - default `source` is `interactive_seed`
  - explicit `--source headless` round-trips into the frontmatter
  - frontmatter parses cleanly through `parse_frontmatter`
  - `status: accepted` round-trips
  - `created` and `accepted_on` are today's date (frozen clock
    via the `today` parameter)
  - the body skeleton renders all eight section headings
  - the cascade's `_find_accepted_rfc` walks a directory with a
    seed-rfc file and returns its first unchecked item without
    raising

**Merge gate for Phase A:** all unit tests pass; existing cascade
tests remain green; `parse_frontmatter` parses the rendered stub
without warnings.

### Phase B — Host skill text + toolkit row (~2h)

- **B1.** Update each of the six host packages' `skill.md`
  (`nightly-host-claude` / `-codex` / `-cursor` / `-gemini` /
  `-antigravity` / `-opencode`) to:
  - Add the trigger paragraph from Resolved decision #7 to the
    Invocation section.
  - Add a row to the Toolkit table for `nightly seed-rfc`:
    `| nightly seed-rfc "<title>" | Seed an accepted RFC from an
    interactive feature seed (planning-first pathway). |`
- **B2.** Update the canonical `.claude/skills/nightly/SKILL.md`
  (which ships as the repo's own dogfood setup) to match.
- **B3.** Update `CLAUDE.md` / `AGENTS.md` Toolkit references so
  the contract surfaces in both the per-host skill and the
  cross-tool rule files.
- **B4.** Extend `nightly doctor` with a check that each host's
  `skill.md` contains the `seed-rfc` toolkit row. Drift surfaces
  as a doctor warning rather than as a silent skill miss.

**Merge gate for Phase B:** Phase A merged; all six skill files
and the canonical SKILL.md contain the trigger paragraph and
toolkit row; `nightly doctor` passes; `CLAUDE.md` and `AGENTS.md`
mention `seed-rfc` in the Toolkit table.

### Phase C — Characterization + dogfood pass (~1h)

- **C1.** `packages/nightly-core/tests/test_rfc005_characterization.py`:
  simulate a feature seed arriving via `seed-rfc`, then run the
  cascade's `_find_accepted_rfc` walk against the resulting
  `.planning/rfcs/` snapshot. Expected: the new RFC is found, its
  first unchecked item is returned, and any older RFCs with
  remaining unchecked items continue to outrank it (verified by
  fixturing an older RFC with one unchecked item and asserting
  that older item is returned first).
- **C2.** Dogfood: with this RFC merged, the next time the
  operator invokes `/nightly <feature>` from an interactive
  session, the agent should pick up Resolved decision #7's
  trigger and invoke `seed-rfc` rather than seeding a task.
  Capture the resulting RFC path and link it from the next
  morning briefing's narrative as evidence the pathway closed
  the loop.
- **C3.** README Cascade section gains a one-paragraph note that
  RFCs in `.planning/rfcs/` may now carry `author: nightly-seed`
  (interactive auto-acceptance) in addition to hand-authored
  RFCs; the cascade treats them identically.

**Merge gate for Phase C:** Phases A + B merged; characterization
test green; the next interactive session that should trigger the
pathway does so (dogfood confirmation captured in the morning
briefing's narrative).

## Sized checklist

**Phase A — CLI helper + frontmatter contract**
- [x] A1. `nightly_core/seed_rfc.py` with `next_rfc_number`, `RFC_FRONTMATTER_TEMPLATE`, `RFC_BODY_SKELETON`, `write_seed_rfc`
- [x] A2. `nightly seed-rfc` Typer command in `cli.py` (positional title, `--slug`, `--source`; prints created path + next-step hint)
- [x] A3. Unit tests in `test_seed_rfc.py` covering numbering, slug derivation, frontmatter round-trip, body skeleton, `_find_accepted_rfc` interop

**Phase B — Host skill text + toolkit row**
- [x] B1. Trigger paragraph (Resolved decision #7) + toolkit row added to all six host `skill.md` files (claude / codex / cursor / gemini / antigravity / opencode)
- [x] B2. `.claude/skills/nightly/SKILL.md` refreshed via `nightly init` / `nightly doctor` from the package skill.md (no separately-versioned copy — the canonical content lives in the host packages)
- [x] B3. Skipped intentionally — CLAUDE.md / AGENTS.md carry the autonomy *contract*, not a toolkit table. The seed-rfc toolkit row lives in host skills only; no rules-file edit needed.
- [x] B4. `nightly doctor` extended with `_REQUIRED_SKILL_TOKENS` content-drift check that flags a stale main skill missing the `seed-rfc` token; re-install refreshes it

**Phase C — Characterization + dogfood**
- [x] C1. `test_rfc005_characterization.py` — cascade walk against seed-RFC output, with older-RFC-still-outranks assertion
- [x] C2. Dogfood confirmation captured in `.nightly/runs/2026-05-27T16-30-35Z/briefing.md` §"RFC 005 dogfood" — the 2026-06-04 interactive session that *wrote* this RFC also executed Phases A–C against it, proving the cascade picks up a fresh accepted RFC at the standard slot. A re-dogfood with the merged `nightly seed-rfc` CLI in play is still worth capturing on the next real interactive session, but the loop-closure claim is verified.
- [x] C3. README Cascade section note about `author: nightly-seed` interactive auto-acceptance
