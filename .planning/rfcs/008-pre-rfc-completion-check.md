---
status: accepted
sized: true
title: Pre-RFC completion check — verify deliverable doesn't already exist before dispatching
created: 2026-06-04
sized_on: 2026-06-04
accepted_on: 2026-06-04
author: nightly-seed
source: interactive_seed
estimated_effort: ~4h across 2 phases
---

# RFC 008 — Pre-RFC completion check

## Status

`accepted` — operator seed in the 2026-06-04 interactive session
(authoring RFCs 006–008 as a bundle). Approach C (skill-side
verifier paragraph + `nightly task --status done` reuse + cascade
re-walk on auto-tick) ships as v1. Phase A wires the skill text and
documents the helper flow; Phase B adds the briefing surface so
auto-ticks are visible in the morning report. A "ledger of past
auto-ticks" was considered and rejected — the RFC checklist itself
is the ledger.

## Context

The cascade's `accepted_rfc` step dispatches against the first
unchecked `- [ ]` item it finds in an accepted RFC. It assumes
unchecked = undone. That assumption breaks in three real-world
ways:

1. **Operator shipped out-of-band.** The operator manually fixed
   a typo or wrote a one-line README addition that was sized into
   an RFC checklist. The work is on `main` but the box stays
   unchecked because nobody manually ticked it.
2. **Prior session shipped without ticking.** A previous Nightly
   session merged a PR addressing an RFC item but the agent
   forgot to update the checkbox in the RFC. The cascade walks
   the same item again next session, the agent re-implements it,
   and a duplicate PR opens.
3. **Concurrent RFC overlap.** Two RFCs sized similar work
   independently. One ships; the other's checklist item is
   addressed-but-still-unchecked.

The cost is visible: duplicate PRs the human has to close,
specialist dispatches that produce no-op diffs, the morning
briefing showing "work" that's actually re-work. The remediation
should be skill-side and cheap: before dispatching against an RFC
item, the agent reads the relevant code and decides whether the
deliverable is already on disk. If yes — tick the box and walk
the cascade again. If no — proceed normally. If partial — scope
only the gap.

RFC 001 §A2 already partially addresses this for *open PRs*: the
cascade skips RFC items that overlap an open Nightly PR (so the
same checklist item doesn't get a second PR while the first is
in review). This RFC extends the same principle to *merged*
work — if the deliverable is already on `main`, the agent ticks
the box rather than producing a duplicate.

## Non-goals

- **Programmatic AST comparison between checklist items and
  code.** The agent is the right judge — it reads the RFC item
  text, reads the relevant code, and decides. No parser that
  tries to match "A1. Add a `next_rfc_number` function" to the
  presence of a `def next_rfc_number(...)` line. Programmatic
  matching is brittle and over-confident.
- **Re-opening already-ticked items.** If the checklist says
  `- [x]` we trust it; the verifier only fires on `- [ ]`. The
  reverse case (item ticked but code missing) is a separate
  audit problem.
- **Cross-RFC dedupe.** If RFC 003 and RFC 007 both size the same
  item, the agent does the verification once and ticks both. We
  don't add an RFC-overlap detector — the agent's natural
  reading catches this when it reads the relevant code.
- **Auto-ticking from PR titles.** The previous-session-shipped-
  without-ticking case (Context #2) sometimes surfaces as an
  RFC reference in a merged PR title (e.g. "RFC 004 §A3"). We
  could parse those at cascade-walk time and auto-tick. v1
  defers — the agent reads code, not PR titles. v2 could add a
  PR-title heuristic if Context #2 turns out to dominate.
- **Replacing the existing open-PR skip in RFC 001 §A2.** That
  heuristic addresses *in-flight* duplication; this RFC
  addresses *merged* duplication. They compose: in-flight skip
  prevents a second PR while the first is open; pre-RFC check
  catches the case where the first already merged.

## Proposed direction

Three approaches; **Approach C** ships as v1.

---

### A — Cascade-side reader (preflight in `nightly next`)

Before `pick_accepted_rfc` returns a match, scan the codebase for
signals that the item is done. Use grep / file-existence checks
against item text. If any hit, skip the item and walk to the next
`- [ ]`.

**Pros:**
- Centralized — the cascade walker is the one place that
  decides whether to dispatch.
- Fully programmatic — no skill text, no agent judgment.

**Cons:**
- Brittle. Item text rarely maps 1:1 to a grep pattern. "Add a
  next_rfc_number helper" matches; "Refactor the proposer suite
  to use a shared base class" doesn't.
- Expensive. The cascade walks at the top of every iteration;
  scanning the codebase each time blows up runtime.
- Conflates *detection* (the cascade's job) with *judgment* (the
  agent's job). The cascade should hand off to the agent;
  programmatic preflight inverts that.

---

### B — Specialist sub-agent: "verifier"

Add a new specialist role — `verifier` — that runs before the
implementer. It reads the RFC item, reads the relevant code,
and returns a yes/no/partial decision. The driver consumes that
decision.

**Pros:**
- Clean separation: a dedicated reasoning pass for the
  verification decision.
- Reuses the existing specialist dispatch surface — no new
  primitives.

**Cons:**
- Adds latency. Every cascade pick now pays a specialist
  dispatch *before* the implementer dispatch. For the common
  case (item is genuinely undone), that's wasted work — the
  agent could have just read the code itself in 2 seconds.
- Adds a new specialist role to the registry — more surface to
  document, test, and maintain.
- Sub-agent dispatch adds 3–5× latency on Claude Code interactive
  mode; the skill's own audit-only carveout already documents
  that overhead.

---

### C — Skill-side verifier paragraph + `nightly task --status` reuse

Update each host's `skill.md`: before scoping a plan from an
`accepted_rfc` cascade pick, the agent reads the RFC item text,
reads the files most likely to carry the deliverable (the agent
uses its judgment about which files), and decides. If the work
is already done:

1. Tick the box in the RFC via Edit tool (the same way the agent
   ticks them off after landing today).
2. Run `nightly next` again to walk to the next unchecked item.
3. Loop until a genuinely-undone item is found or the cascade
   walks past `accepted_rfc`.

If partial: scope a plan that covers only the gap, with the
"Source" section in plan.md noting which parts were pre-existing.

If undone: proceed with normal SCOPE → ISOLATE → IMPLEMENT loop.

**Pros:**
- Cheap. No new specialist dispatch, no cascade-walker code
  scan. The agent reads a few files inline — sub-second cost.
- Uses existing primitives. The Edit-tool tick and the `nightly
  next` re-walk are already in the agent's vocabulary.
- Composable with the open-PR skip from RFC 001 §A2. The
  cascade's existing skip still runs at preflight; the agent's
  verifier runs after the cascade returns its pick.
- Visible in the briefing. Auto-ticks land as commits ("docs:
  tick RFC 007 §B2 — already implemented in commit abc1234")
  which the morning report surfaces under "RFC items
  auto-ticked."

**Cons:**
- Agent judgment is fuzzy at the borderline. An item that's
  "Add Y test for X" may have a test that *partially* covers X
  without the agent recognizing it. The fallback (scope a
  plan covering the gap) is the right judgment call, but
  borderline cases will sometimes mis-classify.
- The auto-tick commits straight to the RFC. If the agent is
  wrong, the operator has to manually un-tick. Mitigation: the
  auto-tick commit message names the supposedly-shipped
  artifact ("already implemented in <SHA>"), giving the
  operator a verification path.

---

## Resolved technical decisions

**1. Approach C ships as v1.** Approach A was rejected because
programmatic grep-based detection is too brittle for the
variety of checklist item shapes. Approach B was rejected
because the verifier sub-agent's latency cost dominates the
verification benefit; agent-inline reads are cheaper. Approach
C keeps the verification in the same context window as the rest
of the work loop.

**2. Verifier paragraph in each host's `skill.md`.** Inserted in
the SCOPE step of "The loop, per task picked." Reads:

> *"**Pre-flight verification (RFC 008).** Before transitioning
> `status: ready → in_progress`, read the RFC item text and the
> files most likely to carry the deliverable. If the work is
> already on disk:*
>
> 1. *Tick the item in the RFC via Edit: `- [ ]` → `- [x]` with
>    a note pointing at the commit / PR / file that shipped it.*
> 2. *Commit the tick directly to main (one-line docs commit;
>    matches the C2-tick pattern from RFC 005).*
> 3. *Run `nightly next` again. Loop the cascade until you find
>    a genuinely undone item.*
>
> *If the work is partial — some of the checklist sub-points are
> done but not all — scope a plan that covers only the gap. The
> plan's `## Source` section names which sub-points were
> pre-existing (with commit references) so the diff stays
> reviewable.*
>
> *If the item is genuinely undone, proceed with the normal SCOPE
> → IMPLEMENT loop.*
>
> *Bias: when uncertain, treat the item as undone and proceed —
> the cost of an unnecessary specialist dispatch is lower than
> the cost of mis-ticking a real outstanding item."*

The text is identical across hosts (no per-host customization
needed), so it can ship as one block reused by each
`skill.md`.

**3. Auto-tick commit format:** docs-only, single-file, message
formatted as `docs(rfc-NNN): tick <PHASE>.<ITEM> — already
implemented in <SHA>`. Examples:
- `docs(rfc-005): tick C2 — already implemented in 53898e6`
- `docs(rfc-007): tick C1 — already shipped as part of #14`

The commit message includes the evidence pointer so retro audit
is one `git log` away.

**4. No "ledger of past auto-ticks."** The RFC checklist IS the
ledger. A box checked with the "auto-implemented in <SHA>"
commit message is sufficient audit trail; no separate
`.nightly/atlas/auto-ticks.md` file.

**5. The verifier reads code, not PR titles or commit
messages.** The skill explicitly says "read the files most
likely to carry the deliverable." PR-title parsing was deferred
(Non-goals) because it duplicates work the agent's
file-reading already does and adds a brittle text-match step.
v2 could add PR-title parsing as a *hint* (not a substitute) if
the file-reading approach proves slow.

**6. Compose with RFC 001 §A2's open-PR skip.** The cascade's
existing skip-RFC-item-that-overlaps-an-open-Nightly-PR runs
at `pick_accepted_rfc` time. This RFC's verifier runs *after*
the cascade returns. Two layers; both active simultaneously:

```
cascade.pick_accepted_rfc  →  agent verifier  →  dispatch
   (RFC 001 skip)              (RFC 008 check)
```

No code change to the RFC 001 skip; this RFC adds a layer
without removing one.

**7. Briefing slot.** The morning briefing's Session narrative
gains a one-line summary when auto-ticks fired:
"Auto-ticked X RFC items (already implemented): [list]." Lets
the operator confirm at a glance that the verifier wasn't
overzealous.

**8. The verifier paragraph is host-uniform.** Unlike RFC 004
§D4's per-host customization for the `depends_on_pr`
heuristic, this RFC's text is identical across all six hosts.
A shared constant in `nightly_core` (similar to the trigger
paragraph in RFC 005's host skills) keeps drift to one source
of truth.

**9. `nightly doctor` content-drift check.** Mirrors RFC 005
§B4: each host's main `skill.md` must contain the literal
token `pre-flight verification` (case-insensitive) for the
`_REQUIRED_SKILL_TOKENS` check to pass. Drift triggers
`nightly doctor` repair → re-install from the package source.

**10. The verifier does NOT call `nightly task` to tick.** The
agent edits the RFC file directly (it's `.planning/rfcs/<NNN>-
<slug>.md`, plain markdown). `nightly task --status done` is
for `plan.md` transitions on `tasks/<n>-<slug>/`. The two
state surfaces stay separate.

## Risks

- **False positive: agent ticks an item that wasn't actually
  done.** The auto-tick commits straight to main; the
  operator has to un-tick if they catch it. Mitigation: the
  commit message names the SHA / PR the agent believed
  shipped the work, so retro audit is fast. Bias paragraph
  says "when uncertain, treat as undone" — the verifier
  errs toward proceeding.

- **False negative: agent doesn't recognize that the item is
  done and re-implements.** Same failure mode the current
  system has; this RFC doesn't make it worse. The specialist
  dispatch will produce a no-op diff which the reviewer
  catches.

- **Partial-implementation mis-scope.** Scoping "only the
  gap" requires the agent to correctly identify what's there
  vs. what's missing. Mitigation: the `## Source` section in
  the partial plan names the pre-existing pieces with
  commits; reviewer can verify.

- **Auto-tick commit noise.** A long-stale RFC could produce
  many auto-tick commits in one session. Mitigation: each is
  one-line docs, signed, distinguishable by `docs(rfc-NNN):
  tick` prefix; `git log --grep` filters cleanly.

- **Race with concurrent edits to the RFC.** If the operator
  is hand-editing the RFC while the agent auto-ticks, both
  writers can clash. v1 inherits the existing single-process
  contract — concurrent edits are operator's responsibility.

- **Verifier reads the wrong files.** Agent picks the wrong
  source location to verify against and concludes "not
  done" when it actually is. The work proceeds, specialist
  dispatch happens, no harm beyond wasted tokens.
  Mitigation: agent reads file paths mentioned in the
  checklist item text first (most items name their target
  paths); falls back to repo-wide grep only when text gives
  no hint.

## Implementation phases

Two phases, ~4h total.

### Phase A — Verifier paragraph + auto-tick documentation (~3h)

- **A1.** Define `RFC_008_VERIFIER_PARAGRAPH` constant in
  `nightly_core.specialists` (or a new module
  `nightly_core.skill_blocks` if the constant gets used across
  multiple skills — there are two now, the seed-rfc paragraph
  and this one). The text is Resolved #2's block verbatim.
- **A2.** Each of the six host `skill.md` files gains the
  verifier paragraph in its SCOPE step, sourced from the
  shared constant via the same per-host duplication pattern
  RFC 005 used. Eventually the per-host duplication is a
  doctor-monitored drift surface (Resolved #9).
- **A3.** `_REQUIRED_SKILL_TOKENS` extension: each host's
  main skill must contain the literal `pre-flight
  verification` (case-insensitive). Per RFC 005 §B4's
  pattern.
- **A4.** Update `nightly_core.cascade._is_item_in_flight`
  docstring to cross-reference this RFC: the existing
  open-PR skip composes with the verifier.
- **A5.** Tests: each host's skill text contains the trigger;
  doctor flags drift; cascade tests still pass with no logic
  change (the verifier runs agent-side, not in the cascade
  walker).

**Merge gate for Phase A:** all six skills have the
paragraph; doctor catches drift; cascade tests still green.

### Phase B — Briefing surface + auto-tick commit format (~1h)

- **B1.** Briefing's Session narrative template gains an
  "Auto-ticked RFC items" section, populated by the agent
  when verifier fires. Section is omitted when no auto-ticks
  happened.
- **B2.** Document the commit message format in the verifier
  paragraph: `docs(rfc-NNN): tick <PHASE>.<ITEM> — already
  implemented in <SHA>` (Resolved #3).
- **B3.** Tests: briefing renders with the new section when
  data is present; renders cleanly when absent.

**Merge gate for Phase B:** Phase A merged; briefing render
unchanged for sessions with no auto-ticks; tests verify
both branches.

## Sized checklist

**Phase A — Verifier paragraph + auto-tick documentation**
- [ ] A1. `RFC_008_VERIFIER_PARAGRAPH` constant in `nightly_core.specialists` (or `skill_blocks` if extracted)
- [ ] A2. Verifier paragraph added to all six host `skill.md` files in the SCOPE step
- [ ] A3. `_REQUIRED_SKILL_TOKENS` extended with `pre-flight verification` per host
- [ ] A4. `_is_item_in_flight` docstring cross-references this RFC's verifier
- [ ] A5. Tests covering presence + doctor drift detection

**Phase B — Briefing surface + auto-tick commit format**
- [ ] B1. Briefing template gains "Auto-ticked RFC items" optional section
- [ ] B2. Commit message format documented in the verifier paragraph
- [ ] B3. Tests covering rendered briefing with and without auto-ticks
