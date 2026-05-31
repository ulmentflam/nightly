---
status: draft
title: Worktree policy — prevent stacked-PR geometry at branch creation time
created: 2026-05-31
author: ulmentflam
---

# RFC 004 — Worktree policy: stacked-PR geometry prevention

## Status

`draft` — not yet sized or accepted. Promote to `accepted` only after a
human author sizes the checklist into phases, resolves the open questions
below, and picks among the three named approaches. Until then the cascade
will not auto-pick any checkbox from this RFC.

## Context

The 2026-05-24 stacked-paperwork incident produced a five-level PR chain:

```
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

_None yet — left for the sizing pass to fill in._

Placeholders for the sizing author:

- **Which approach ships as v1 (A, B, or C)?** TBD.
- **Enforcement point** (inside `create_worktree`, in the driver, or as a
  new `worktree_policy` cascade step)? TBD.
- **`depends_on_pr` as plan frontmatter field vs. cascade-only metadata?**
  TBD — relevant only if B or C wins.
- **`blocked: dependency` as a new `PlanStatus`?** TBD — relevant only if
  the wait sub-variant of B or C ships.

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

## Open questions

1. **Which approach (A, B, or C) wins for v1?** The sizing pass must
   pick one. Approach C is the most complete but also the most surface
   area. Approach A is the simplest but has the highest conflict risk for
   tasks with genuine code dependencies. Approach B (declare-and-surface
   sub-variant, not the wait variant) may be the right starting point:
   low enforcement, high visibility.

2. **Does the cascade need a new source `awaiting_dependency`, or is
   dropping back to `nothing` enough for the wait sub-variant?** RFC 001's
   Resolved design decision #3 chose "no new cascade source" for the
   RFC-overlap case. The dependency-wait case is different: `nothing` is
   misleading when there are `blocked: dependency` plans sitting on disk.
   An `awaiting_dependency` source (inserted after `unblocked_approval` in
   `CASCADE_SOURCES`) would let `nightly next` surface the blockage
   explicitly rather than silently falling through to ideation.

3. **How does the driver detect "this task depends on the in-flight PR"
   without a structured declaration?** Automatic detection requires
   reasoning about diff overlap between the in-flight PR's changes and the
   new task's planned scope. The most practical v1 approach is to skip
   automatic detection entirely: if the agent does not write
   `depends_on_pr` into the plan frontmatter, the driver treats the task
   as having no dependency and cuts from `main`. The question for sizing
   is whether to document a prompt heuristic in the task-scoping agent
   instructions (e.g. "if the plan references a function or module
   introduced by an open PR, add `depends_on_pr`") or to leave detection
   entirely to the agent's own judgment.

4. **Should the Stop-hook backpressure cap (`MAX_OPEN_PRS`) interact with
   this RFC's prevention logic?** Specifically: should stacked PRs (those
   with a declared `depends_on_pr` ancestor) count toward the cap, or
   should only root-level PRs (those targeting `main` directly) count?
   The current `count_open_nightly_prs` function in `cascade.py` counts
   all open `nightly/*` PRs, stacked or not. Excluding stacked PRs from
   the cap could allow the agent to build deep chains while staying under
   the cap nominally — a loophole. Including them treats a 5-level chain
   as 5 cap-slots, which may be too conservative. This is a policy
   decision for the sizing author.

5. **What is the operator escape hatch when a declared dependency no
   longer matters and they want the branch rebased to `main`?** If the
   operator reviews the stacked PR and decides the parent PR's changes
   are irrelevant to the child, they should be able to signal to Nightly
   "drop the `depends_on_pr` and rebase to `main`." The current
   `nightly approve` path (which clears `blocked: approval` via
   `approval_granted: true`) could be extended, or a separate
   `nightly rebase <task-slug>` command added. Sizing must decide whether
   this is in scope for v1 or deferred.

6. **How does the prevention check interact with the READY marker from
   RFC 002 §D2?** The `.nightly/worktrees/<branch-slug>/READY` marker
   caches the worktree's pre-commit readiness. The stacked-geometry check
   operates at branch *creation* time, before a READY marker exists.
   These are independent — the READY marker is a post-creation cache for
   the probe; the geometry check is a pre-creation policy gate. They share
   the branch-slug derivation logic in `_branch_slug_for` in `cascade.py`,
   and any v1 implementation should reuse that helper rather than
   duplicating the slug logic.

## Checklist (for promotion to `accepted`)

Complete all items before changing `status` to `accepted`. The cascade
will not pick these items until the status flips.

- [ ] Resolve open question 1: pick Approach A, B, or C for v1
- [ ] Resolve open question 2: decide whether to add `awaiting_dependency`
      to `CASCADE_SOURCES` or fall through to `nothing`
- [ ] Resolve open question 3: document the agent-level heuristic (or
      explicit non-heuristic) for declaring `depends_on_pr`
- [ ] Resolve open question 4: decide whether stacked PRs count toward
      `MAX_OPEN_PRS` in the Stop-hook cap
- [ ] Resolve open question 5: decide on operator escape hatch (in-scope
      for v1 or deferred)
- [ ] Resolve open question 6: confirm that the geometry check and the
      RFC 002 READY marker are implementation-independent and document the
      boundary explicitly
- [ ] Size the chosen approach into implementation phases (with
      effort estimates, hard-dependency arrows, and per-phase merge gates)
- [ ] Write characterization tests against the 2026-05-24 stacked-paperwork
      bundle (5 nested `nightly/` branches, HEAD in the chain) that lock
      the prevention behavior selected in question 1
- [ ] Update `detect_stacked_geometry` documentation in `cascade.py` to
      reference this RFC's prevention decision once it is made
- [ ] Update `.planning/brainstorm.html` §03 (cascade sources) if a new
      `awaiting_dependency` source is added
