---
status: accepted
sized: true
title: Worktree policy — prevent stacked-PR geometry at branch creation time
created: 2026-05-31
sized_on: 2026-06-01
accepted_on: 2026-06-01
author: ulmentflam
estimated_effort: ~9.5h across 4 phases
---

# RFC 004 — Worktree policy: stacked-PR geometry prevention

## Status

`accepted` — sized into four phases around Approach C (default-prevent
with explicit `depends_on_pr` opt-in). Phase A lands the plan-frontmatter
field and the driver-side enforcement; Phase B threads the declaration
into PR bodies; Phase C refines RFC 001 §B's briefing panel to
distinguish declared from accidental stacks; Phase D characterizes
against the 2026-05-24 stacked-paperwork bundle and documents the
declaration heuristic for the host task-scoping skill.

## Context

The 2026-05-24 stacked-paperwork incident produced a five-level PR chain:

```text
main
└── nightly/unblock-20260523            (PR #54)
    └── nightly/phase-e-reconcile-…     (PR #55, base = #54's branch)
        └── nightly/phase-j-reconcile-… (PR #56, base = #55's branch)
            └── nightly/phase-k-…       (PR #57, base = #56's branch)
                └── nightly/plan-recon… (PR #58, base = #57's branch)
```

RFC 001 addressed the detection half of this failure: `detect_stacked_geometry`
(in `cascade.py`) identifies when the current HEAD is the head ref of an
open Nightly PR, and the briefing renderer adds a rose-bordered "stacked PR
geometry" panel when the chain is non-empty (RFC 001 §B). Resolved design
decision #1 in RFC 001 reads explicitly:

> Prevention (forced branch-from-`main`) is deferred — needs a
> worktree-policy RFC of its own.

This RFC is that follow-up. Prevention is a distinct concern from detection
because it touches `worktree.py`'s `create_worktree` call path rather than
the cascade's briefing layer. The right insertion point is before or inside
`create_worktree`, not inside `next_task` — and the tradeoffs of the three
prevention strategies differ enough from "report-and-allow" to warrant
separate design time.

The host-level Stop-hook backpressure cap (`MAX_OPEN_PRS=5`) remains the
last-resort safety net for both the stacked-geometry failure mode and the
PR-backlog saturation failure mode described in RFC 001's context section.
This RFC does not replace that cap — it addresses the upstream cause (the
agent stacking branches without declaring the dependency) rather than the
downstream symptom (too many open PRs for the operator to review).

## Non-goals

- **Auto-merging the parent PR before cutting the child branch.** That is
  a destructive git operation against the remote and falls squarely in the
  refusal-policy category "destructive git / production state." It also
  requires human review of the parent PR's diff — which is the exact
  bottleneck this RFC is trying to relieve, not worsen.
- **Modifying GitHub's auto-retarget behavior.** GitHub auto-retargets
  downstream PRs to `main` when the base merges. That behavior is correct
  and useful; Nightly should not fight it.
- **Cross-repo worktree policy.** The `create_worktree` function resolves
  the repo root via `_main_worktree_root`; this RFC's scope is limited to
  a single repo's `nightly/*` branch namespace.
- **Retroactive retargeting of already-opened PRs.** Once a stacked PR is
  open, re-pointing its base is a `gh pr edit --base` call that changes
  the diff the reviewer sees. Out of scope; too surprising.
- **Changing the refusal-policy categories or the Stop-hook cap.**
  RFC 001 §Non-goals and the CLAUDE.md contract cover these.

## Proposed direction

Three named approaches are described below. None is selected yet; sizing
resolves which one (or hybrid) ships as v1.

---

### A — Forced branch-from-main

At worktree creation time, the driver calls `create_worktree` with
`base_branch="main"` unconditionally, regardless of the current HEAD.
`create_worktree` already accepts `base_branch` as a parameter and passes
it to `git worktree add <path> -b <branch> <base_branch>`. Today the
driver passes `base_branch="main"` in most paths; the stacked-geometry
failure occurs when the driver is called from inside a `nightly/` branch
context (e.g. when the agent resumes an in-flight plan that lives on
`nightly/unblock-…` and then tries to start a second task).

The enforcement point would be a guard in `create_worktree` or its caller:
before calling `git worktree add`, resolve whether `base_branch` is a
`nightly/` branch head that maps to an open PR. If so, force `base_branch`
to `origin/main` and log the substitution.

**Pros:**
- Simple to implement: one check in `create_worktree` or the driver, no
  new frontmatter fields, no cascade plumbing.
- Completely prevents the stacked geometry at the source — no detection
  required after the fact, no human intervention needed.
- Consistent with the existing `WorktreeHandle.base_branch` field, which
  already records the base for later reference.

**Cons:**
- The new branch may diverge from the in-flight parent PR. If PR #54
  renames a symbol used by PR #55's planned changes, the agent's new
  worktree (cut from `main`) won't have that rename. The agent will write
  code against the old symbol, produce a conflicted diff, and CI will fail
  on the open PR.
- The agent is not equipped to resolve merge conflicts well (the refusal
  policy's "bypass test or type safety" carveout doesn't cover this, but
  conflict resolution is a known weak spot). Forcing branch-from-main
  silently creates a class of failures the agent can't recover from
  without a human.
- `detect_stacked_geometry` in `cascade.py` will still fire during
  briefing (it checks the *current* branch, not the worktree base) — the
  detection panel and the prevention logic become independent code paths
  that could diverge.

---

### B — Structured `depends_on_pr` plan frontmatter

Rather than preventing stacking at the filesystem level, the agent declares
when a new task genuinely depends on an open PR by adding a
`depends_on_pr: <number>` field to the plan's frontmatter when it is
materialized. The cascade then reads this field and takes one of two actions:

1. **Wait variant**: the cascade treats `depends_on_pr: <N>` plans as
   non-dispatchable until PR #N merges (checked via `gh pr view <N>
   --json state`). The plan's status remains `ready` but the cascade's
   `pick_in_flight` / `pick_unblocked` steps skip it. This is analogous
   to `blocked: approval` — a new status `blocked: dependency` would be
   appropriate.

2. **Declare-and-surface variant**: the plan is dispatched normally, but
   the `depends_on_pr` field is surfaced in: (a) the PR body as a
   human-visible "Depends on #N" line, (b) the briefing's geometry panel
   (already rendered by RFC 001 §B), and (c) a new `dependency_chain` key
   in `BriefingContext`. The operator sees the dependency explicitly instead
   of discovering it at review time.

The `PROPOSER_FINGERPRINT_KEY` precedent in `plans.py` establishes that
structured frontmatter fields with a named constant are the right idiom for
cascade-readable plan metadata. A new `DEPENDS_ON_PR_KEY = "depends_on_pr"`
constant in `plans.py` and a corresponding `PlanRecord.depends_on_pr`
property follow the same shape.

**Pros:**
- Preserves optionality: sometimes a task *should* depend on an in-flight
  PR (e.g. a Phase E task that builds on Phase D's new module). Forced
  branch-from-main (Approach A) silently breaks these.
- The dependency is explicit and auditable in the plan frontmatter and PR
  body — the operator knows exactly why the PR is structured the way it is.
- The "declare-and-surface" sub-variant matches the autonomy contract's
  preference for transparency over prevention: the agent names the
  situation and the human decides.
- No merge-conflict hazard, since the worktree is cut from the actual
  dependency branch.

**Cons:**
- Requires the agent to correctly declare `depends_on_pr` — if it forgets,
  the stacked geometry re-emerges silently (same failure mode as today).
- The "wait" sub-variant risks stalling the cascade: if PR #54 sits
  unreviewed for three days, every plan with `depends_on_pr: 54` blocks
  forever. The Stop-hook cap can release the session, but the operator
  wakes up to a stalled queue rather than shipped work.
- Adds a new status (`blocked: dependency`) to `PlanStatus`, touching
  `plans.py`'s `PlanStatus` literal and every test that fixtures the
  status set.
- The detection of "this task depends on an in-flight PR" is the hard
  part: the agent must reason about whether the in-flight PR's diff
  overlaps the new task's scope. That reasoning is fragile; false
  declarations (declaring a dependency that doesn't exist) block the
  cascade unnecessarily.

---

### C — Hybrid: explicit declaration + escalation

The default behavior is always branch-from-main (same as Approach A for
the common case). If the agent determines that the new task genuinely
depends on an open Nightly PR's changes, it must make an explicit
structured declaration in the plan frontmatter (`depends_on_pr: <N>`) to
opt into the stacked geometry. Without the declaration, `create_worktree`
forces `base_branch="main"`.

When `depends_on_pr` is declared:
- The worktree is cut from the named PR's branch (preserving the
  dependency's context).
- The plan frontmatter carries the `depends_on_pr` field.
- The PR body includes a "Depends on #N" line automatically.
- `detect_stacked_geometry` still fires in the briefing but marks the
  chain as "declared dependency" (green, not rose) to distinguish it from
  an accidental stack.

This is a hybrid of A's "no accidental stacks" guarantee and B's "declared
dependencies are legitimate" carveout.

**Pros:**
- Accidental stacks are impossible: the driver enforces branch-from-main
  unless the plan explicitly opts out.
- Declared stacks are visible and auditable, not silently surprising to
  reviewers.
- The briefing geometry panel gains a meaningful distinction between
  "accidental" and "declared" chains, making RFC 001 §B more useful.
- Aligns with the existing pattern of explicit, auditable plan frontmatter
  fields (`PROPOSER_FINGERPRINT_KEY`, `PR_LAST_RECONCILED_KEY`).

**Cons:**
- Still requires the agent to correctly identify when `depends_on_pr` is
  needed (same detection problem as B). If the agent forgets to declare
  the dependency and cuts from `main`, it may produce merge conflicts in
  the PR — the same class of failure as Approach A.
- More moving parts than A or B alone: both the prevention path and the
  declaration path need to be correct. Testing surface is larger.
- The `detect_stacked_geometry` change (green vs. rose for declared vs.
  accidental) is a UI change to the briefing template that's otherwise
  ready and shipping under RFC 001's Phase C characterization tests.

---

## Resolved technical decisions

**1. Approach C (hybrid) ships as v1.** Default behavior is
branch-from-`main`, enforced in the driver before `create_worktree` is
called. The agent opts into a stacked geometry by writing
`depends_on_pr: <N>` into the plan's frontmatter at scoping time. With
the declaration, the worktree is cut from PR #N's head ref (preserving
the dependency's context); without it, the driver substitutes
`origin/main` regardless of the current HEAD. Approach A was rejected
because it silently breaks legitimate cross-task dependencies (e.g.
Phase E building on Phase D's new module) and produces conflicted diffs
the agent isn't equipped to resolve. Pure Approach B was rejected
because forgetting to declare reproduces the original failure
silently — the prevention-by-default in C closes that hole.

**2. Enforcement point is the driver, not `create_worktree`.** The
plan's frontmatter is read at the same layer that already constructs
the worktree request (the dispatch driver), so the substitution happens
*before* `create_worktree` is called. `create_worktree` itself stays
plan-agnostic and continues to honor the `base_branch` argument it
receives. The guard lives in a new helper —
`_resolve_base_branch(plan, repo_root) -> str` — that the driver calls
to derive the effective base. This keeps the prevention logic
co-located with the plan-reading code rather than smearing
plan-awareness into the worktree primitives.

**3. `depends_on_pr` is a structured plan frontmatter field.** Add
`DEPENDS_ON_PR_KEY = "depends_on_pr"` to `plans.py` alongside the
existing `PROPOSER_FINGERPRINT_KEY` and `PR_LAST_RECONCILED_KEY`
constants, plus a `PlanRecord.depends_on_pr: int | None` property.
Cascade-only metadata was rejected because the field needs to round-trip
through plan persistence (so subsequent cascade walks can re-read it)
and needs to be auditable in `git log`.

**4. No new `blocked: dependency` PlanStatus.** Because Approach C cuts
from the PR's branch when the declaration is present, the dependency is
*satisfied* by the geometry itself rather than gated by a status. There
is no cascade wait — `depends_on_pr` is descriptive (where to base
from), not gating (whether to dispatch). Consequently no new
`PlanStatus` literal, no changes to existing status-set fixtures.

**5. No new `awaiting_dependency` cascade source.** With no wait
semantics, the cascade has nothing new to surface. The existing sources
remain unchanged; `pick_in_flight` and `pick_unblocked` see
`depends_on_pr`-bearing plans exactly like any other ready plan.
Resolution preserves RFC 001 §Resolved-design-decisions #3's bias
toward bookkeeping minimalism.

**6. Declaration heuristic: documented prompt, not automatic
detection.** No diff-overlap reasoning, no automatic dependency
inference. The host-side task-scoping skill instructions gain a single
heuristic paragraph: *"If your planned changes touch a symbol, module,
or file introduced by an open Nightly PR, declare it as
`depends_on_pr: <N>` in the plan frontmatter. When in doubt, omit the
field — the driver will branch from `main` and any conflict will
surface at CI time rather than at base-resolution time."* This biases
toward false negatives (occasionally a missed declaration produces a
conflicted diff at CI) over false positives (a declared dependency
that isn't real, which would stack the PR unnecessarily). Consistent
with RFC 001 §Resolved-design-decisions #2's same bias.

**7. Stacked PRs count toward `MAX_OPEN_PRS`.** The Stop-hook cap is
about operator review bandwidth, not branch geometry. A 5-level
declared chain is 5 PRs the operator must read, so it occupies 5
cap-slots. Excluding stacked PRs would create a loophole where the
agent could build arbitrarily deep chains while staying under the cap
nominally. The existing `count_open_nightly_prs` in `cascade.py` keeps
its current behavior; no code change in this RFC.

**8. Operator escape hatch deferred to v2.** Removing a `depends_on_pr`
declaration to rebase a child onto `main` is a manual operation in v1:
the operator edits the plan's frontmatter, deletes the field, and
`nightly` re-bases at the next dispatch cycle. A dedicated
`nightly rebase <slug>` command is out of scope. The escape hatch is
worth revisiting if v1 produces frequent rebase asks; otherwise the
manual edit is acceptable.

**9. Briefing panel gains a `declared` flag per chain entry.**
`StackedGeometry` (from RFC 001 §B) extends to carry a `declared: bool`
per chain entry, computed by reading the plan on the chain branch and
checking whether its `depends_on_pr` matches the parent PR number.
`briefing.html.j2` renders declared chains with a green-bordered panel
and accidental chains with the existing rose border. A mixed chain
(some declared, some accidental) renders rose with per-entry color
annotations — accidental is the dominant signal because it's the
failure mode.

**10. Geometry-check and READY-marker boundary.** The geometry check
fires in the driver at branch-creation time (pre-creation policy gate);
RFC 002's `READY` marker is a post-creation cache for the pre-commit
probe. They share `_branch_slug_for` from `cascade.py` for slug
derivation but otherwise operate on disjoint state. No coupling, no
shared mutable cache. Documented as a one-paragraph note in
`worktree.py` next to the `_resolve_base_branch` helper.

## Risks

- **Over-waiting (Approach B, wait sub-variant):** if the cascade defers
  all tasks with `depends_on_pr` set and the parent PR is never merged,
  the queue can stall indefinitely. The Stop-hook cap releases the session
  after the PR-backlog threshold, but the affected plans remain in
  `blocked: dependency` until the operator either merges the parent or
  manually unblocks them. Mitigation: expose a `nightly unblock <task-slug>`
  escape hatch (or reuse `nightly approve`) and document the stall risk in
  the plan body when the dependency is declared.

- **Over-branching (Approach A, undetected conflicts):** forcing
  branch-from-main when there is a real code dependency between tasks
  produces a worktree that lacks the dependency's changes. The agent will
  write code that compiles against the old interface, produce a diff that
  conflicts with the in-flight PR, and CI will fail. The operator wakes up
  to a pair of conflicted PRs rather than a stacked chain. Mitigation:
  document the conflict risk prominently in the PR body when branch-from-main
  overrides the natural base; the briefing's geometry panel retains the
  detection logic from RFC 001 §B so the operator can see the situation.

- **False-positive dependency detection (all approaches):** any heuristic
  that tries to determine "does task Y depend on in-flight PR #N?" will
  have false positives (declaring a dependency that doesn't exist). False
  positives in Approach B's wait sub-variant block valid work; in
  Approach C they permit stacking that wasn't needed. The safest heuristic
  for v1 is "no automatic detection — the agent must declare explicitly,
  or the default (branch-from-main) applies." This is conservative but
  consistent with RFC 001 §Resolved-design-decisions #2, which biased
  toward false negatives rather than false positives for the RFC-overlap
  detection.

- **Detection / prevention divergence:** `detect_stacked_geometry` in
  `cascade.py` fires during briefing; the prevention logic fires during
  `create_worktree` in `worktree.py`. These two code paths must stay
  consistent in their definition of "a stacked branch." If prevention
  succeeds but detection still fires (e.g. because the READY marker is
  stale), the operator sees a false alarm in the briefing. Mitigation:
  share a single `_nightly_open_pr_branches` call between both paths.

## Implementation phases

Four phases, ~9.5h total. Phase A is the load-bearing piece (plan
field + driver enforcement); Phases B–D layer transparency and
characterization on top. Each phase is independently mergeable — the
prevention semantics work after Phase A even if B–D never land.

### Phase A — plan field + driver enforcement (~4h)

- **A1.** `DEPENDS_ON_PR_KEY = "depends_on_pr"` constant in `plans.py`,
  alongside `PROPOSER_FINGERPRINT_KEY` and `PR_LAST_RECONCILED_KEY`.
- **A2.** `PlanRecord.depends_on_pr: int | None` property — parses the
  frontmatter value, accepting `int` or `str` (with `"#"` prefix
  tolerated) and returning `None` when absent/unparseable.
- **A3.** `_resolve_base_branch(plan, repo_root) -> str` helper in
  `worktree.py` — when `plan.depends_on_pr` is set, resolves the PR's
  head ref via `gh pr view <N> --json headRefName,state`; falls back to
  `origin/main` with a warning if the PR is closed/merged or `gh`
  fails. When unset, returns `origin/main` unconditionally.
- **A4.** Dispatch driver call site swapped from passing `base_branch`
  literally to calling `_resolve_base_branch(plan, repo_root)`.
- **A5.** Unit tests (stubbed `gh pr view`): no-declaration → main;
  declared + open PR → PR's head ref; declared + merged PR → main with
  warning; declared + closed PR → main with warning; declared + no-`gh`
  → main with warning; malformed `depends_on_pr` → main + log.

**Merge gate for Phase A:** all unit tests pass; characterization test
deferred to Phase D so this phase ships independently.

### Phase B — PR-body declaration line (~1.5h)

- **B1.** PR-body builder (in the dispatch driver's commit-and-push
  path) reads `plan.depends_on_pr` and, when present, prepends a
  `Depends on #<N>` line before the existing PR body.
- **B2.** Tests: declared → line present and exactly once even when
  re-pushed; not declared → line absent; multi-line body preserved
  verbatim after the line.

**Merge gate for Phase B:** Phase A merged; PR-body tests green.

### Phase C — briefing panel: declared vs accidental (~2h)

- **C1.** `StackedGeometry` (from RFC 001 §B) extends to carry
  `declared: bool` per chain entry. Computed by reading the plan on the
  chain branch and matching `plan.depends_on_pr` against the parent
  PR number.
- **C2.** `briefing.html.j2` adds a green-bordered "declared dependency
  chain" panel variant; the existing rose-bordered panel covers
  accidental and mixed chains. Mixed chains render rose with per-entry
  color annotations.
- **C3.** Tests: pure-declared (all green); pure-accidental (all rose,
  existing behavior); mixed (rose with annotations); empty (no panel,
  existing behavior).

**Merge gate for Phase C:** Phase A merged (so plans actually carry
`depends_on_pr`); briefing render tests green.

### Phase D — characterization + heuristic docs (~2h)

- **D1.** Characterization test against the 2026-05-24 stacked-paperwork
  bundle: 5 nested `nightly/` branches, none of the plans declare
  `depends_on_pr`. Expected: each new worktree cuts from `main`,
  geometry panel renders rose, no stacking occurs.
- **D2.** Counterpart characterization test: same 5-branch bundle but
  each child plan declares `depends_on_pr: <parent>`. Expected: chain
  preserved, geometry panel renders green, PR bodies carry the
  `Depends on #N` line.
- **D3.** Update `detect_stacked_geometry`'s docstring in `cascade.py`
  to reference this RFC and the prevention semantics it now interacts
  with.
- **D4.** Document the declaration heuristic in the host task-scoping
  skill instructions (the paragraph in Resolved decision #6). Apply
  the same paragraph to `nightly-core`'s skill template so all five
  hosts inherit it.
- **D5.** README paragraph cross-referencing RFC 001 §B (detection)
  and RFC 004 (prevention + declared-dependency carveout).

**Merge gate for Phase D:** Phases A + B + C merged; both
characterization tests green; docs reviewed.

## Sized checklist

**Phase A — plan field + driver enforcement**
- [x] A1. `DEPENDS_ON_PR_KEY` constant in `plans.py`
- [x] A2. `PlanRecord.depends_on_pr: int | None` property (parses bare int, `#`-prefixed, returns None on malformed/zero/negative)
- [x] A3. `_resolve_base_branch` helper in `worktree.py` (decoupled from `PlanRecord` to keep `driver → {plans, worktree}` direction; split fallback bookkeeping into `_lookup_open_pr_head_ref`)
- [x] A4. `run_one_task` calls `_resolve_base_branch(depends_on_pr=plan.depends_on_pr, default_base=base_branch, root=root)` before `create_worktree`
- [x] A5. Unit tests (15 cases: PlanRecord parsing + no-decl / open PR / merged PR / closed PR / no-gh / gh-nonzero-exit / unparseable JSON / subprocess error / non-main default)

**Phase B — PR-body declaration line**
- [ ] B1. PR-body builder prepends `Depends on #<N>` when `plan.depends_on_pr` is set
- [ ] B2. Tests (line present / line absent / re-push idempotent / multi-line body preserved)

**Phase C — briefing panel: declared vs accidental**
- [ ] C1. `StackedGeometry` chain entries carry `declared: bool`
- [ ] C2. `briefing.html.j2` renders green panel for declared, rose for accidental/mixed
- [ ] C3. Tests (pure-declared / pure-accidental / mixed / empty)

**Phase D — characterization + heuristic docs**
- [ ] D1. Characterization test: 2026-05-24 bundle without declarations → all cut from main
- [ ] D2. Characterization test: 2026-05-24 bundle with declarations → chain preserved, green panel
- [ ] D3. `detect_stacked_geometry` docstring references RFC 004 prevention semantics
- [ ] D4. Declaration heuristic paragraph added to host task-scoping skill instructions across all five hosts (claude / codex / cursor / gemini / opencode) via the `nightly-core` template
- [ ] D5. README paragraph cross-references RFC 001 §B + RFC 004
