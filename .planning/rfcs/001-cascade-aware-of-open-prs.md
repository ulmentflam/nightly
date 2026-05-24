---
status: draft
title: Make the cascade aware of open Nightly PRs (branch geometry + RFC overlap)
created: 2026-05-24
author: ulmentflam
---

# RFC 001 — Cascade awareness of open Nightly PRs

## Status

`draft` — not yet `accepted`, so Nightly's cascade will not auto-pick
items from this RFC. Promote to `accepted` after a human author has
read it, sized it, and broken it into checkbox items.

## Context

The 2026-05 corpus-forge incident, then the 2026-05-24 stacked-paperwork
incident, both shipped the same root pattern: **the cascade can keep
finding plausible work even when the operator's review queue is already
saturated.** The host-level Stop-hook backpressure landed in Phase 9p
(`MAX_OPEN_PRS=5`) addresses the *symptom* (the agent producing PR N+1
on top of N unreviewed PRs), but the cascade itself remains uninformed
about open PRs. Two specific failure modes survive:

### A. Stacked-on-unblock branch geometry

When PR #54 introduces an unblock (e.g. a refactor that subsequent
plan items depend on) and isn't merged yet, the agent's next worktree
is created from `nightly/unblock-<ts>` rather than `main`. Subsequent
PRs target that branch as their base, producing a chain:

```
main
└── nightly/unblock-20260523            (PR #54)
    └── nightly/phase-e-reconcile-…     (PR #55, base = #54's branch)
        └── nightly/phase-j-reconcile-… (PR #56, base = #55's branch)
            └── nightly/phase-k-…       (PR #57, base = #56's branch)
                └── nightly/plan-recon… (PR #58, base = #57's branch)
```

GitHub auto-retargets downstream PRs to `main` when the base PR merges,
so this isn't *broken* per se — but it's surprising review geometry,
makes rebases more painful if PR #54 is amended, and tempts reviewers
into "I'll wait for #54 before looking at #55" deferrals that snowball.

The correct behavior is one of:

1. **Wait** — when the cascade would pick work that conceptually
   depends on an open Nightly PR, defer until the dependency merges.
   Risks: stalls. The Stop-hook backpressure cap is the safety net.
2. **Branch from `main` anyway** — accept that the new branch may have
   merge conflicts with the in-flight PR, and resolve them post-hoc.
   Risks: conflicts the agent isn't equipped to resolve well.
3. **Detect and report** — let the cascade pick the work, but log the
   stacked geometry as a `pr_geometry_warning` in the briefing so the
   operator can see what's stacked on what at a glance.

### B. Cascade picking RFC items already addressed by an open PR

`pick_accepted_rfc` scans `.planning/rfcs/*.md` for unchecked top-level
checkbox items and returns the first match. It has no notion of "this
item is being addressed by an open PR right now." So when PR #58 ticked
43 checkboxes against an RFC but the PR is still open, the next cascade
walk could still pick checkbox #44 from the same RFC and produce PR #59
— even though the operator is one merge away from the desired state.

The correct behavior: when an open Nightly PR's body / title / linked
plan claims to address checkboxes in an accepted RFC, the cascade
should treat those items as `in-flight` and skip past them. The
matching can be approximate (PR title contains the RFC filename, plan
metadata links the PR to the RFC, the PR body has a `Closes RFC-001
items 1-43` style line) — false negatives are fine (worst case: we
re-pick an item that's already being addressed, exactly today's
behavior), false positives are the danger (we skip an item that
isn't actually addressed and silently lose progress).

## Non-goals

- Changing the host-level Stop-hook backpressure (already landed in
  Phase 9p — that's the safety net for both A and B).
- Changing the refusal-policy categories.
- Auto-merging or auto-rebasing of stacked PRs. That's a separate
  feature (and a separate refusal-policy review).

## Proposed direction

Take these as a starting frame, not a commitment — concrete design
should happen when this RFC is sized.

### For (A) — stacked-on-unblock geometry

- Detect: when starting a new worktree, the agent's current branch
  HEAD is not `main` and there's an open Nightly PR for the current
  branch.
- React: prefer branching from `main` and recording a
  `depends_on_pr: <number>` field in the plan's frontmatter. The
  driver can refuse to push if the worktree's HEAD diverges from
  `main` in a way that suggests stacking, and instead create the
  new branch from `origin/main`.
- Failure mode: agent silently produces stacks anyway because the
  detection is fragile. Mitigation: log the geometry to the briefing
  unconditionally so the operator sees stacking even when prevention
  fails.

### For (B) — RFC-checkbox overlap

- Detect: in `pick_accepted_rfc`, after finding an unchecked item,
  consult open Nightly PRs for ones whose title / body / linked plan
  reference the same RFC filename or item text.
- React: skip RFC items judged to be "in flight" and fall through to
  the next cascade source. If *every* unchecked item in *every*
  accepted RFC is in flight, the cascade should return `nothing` (or
  fall through to `ideate_fallback`), letting the Stop hook's
  backpressure cap take over.
- Implementation hint: store the RFC→PR association on the plan when
  the agent first scopes the task. That removes the matching ambiguity
  on later iterations.

## Risks

- **Over-skipping** (B): if the matching is wrong, the cascade skips
  items it shouldn't, and shipped work doesn't get picked up. The
  Stop-hook cap catches the worst case (no infinite no-op spin) but
  silent skipping is still a regression.
- **Over-waiting** (A): if the agent defers on an open PR, and the
  operator forgets to merge it, the cascade stalls. The Stop-hook cap
  releases the session, so the next morning's `/nightly` invocation
  picks up wherever it left off — but the queue isn't draining either.
- **Author drift**: the heuristics for matching PR ↔ RFC items will
  drift as PR-title / commit-message conventions evolve. Tests are
  essential; characterization tests against the corpus-forge incident
  bundle are the cheapest version.

## Open questions

- Should the cascade expose a new source (`awaiting_dependency`?) for
  visibility, distinct from `nothing` / `ideate_fallback`?
- Should plan frontmatter gain a structured `depends_on_pr` field, or
  is best-effort title/body matching enough for the first cut?
- How does this interact with the `pr_rescue` cascade source? If a
  stacked PR collects blocking feedback, do we rescue it before
  merging the parent?

## Checklist (for promotion to `accepted`)

Leave unchecked until a human author has decided to ship this.

- [ ] Decide between "wait" / "branch-from-main" / "report-and-allow"
      for stacked geometry (section A).
- [ ] Decide on matching strategy for RFC-overlap (title scan vs
      structured plan metadata).
- [ ] Spec the `awaiting_dependency` cascade source (or rule it out).
- [ ] Write characterization tests against the corpus-forge bundle
      and the 2026-05-24 stacked-paperwork bundle.
- [ ] Land the changes behind `MAX_OPEN_PRS` so the safety net stays
      regardless of detection accuracy.
