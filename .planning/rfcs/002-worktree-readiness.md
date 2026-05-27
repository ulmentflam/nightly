---
status: draft
title: Worktree readiness — detect and remediate broken pre-commit hooks before dispatch
created: 2026-05-27
author: ulmentflam
---

# RFC 002 — Worktree readiness

## Status

`draft` — not yet `accepted`, so Nightly's cascade will not auto-pick
items from this RFC. Promote to `accepted` after a human author has
read it, sized it, and broken it into checkbox items.

## Context

Issue [#2](https://github.com/ulmentflam/nightly/issues/2) (May 2026,
corpus-forge): Nightly completed 21 tasks across 3 P0 RFCs +
7 autonomous proposals, every one verified pyrefly-clean against
its worktree, and **landed every one as a local proposal branch**
rather than a real PR. The reason was upstream of Nightly entirely —
the host repo's pre-commit `pyrefly` hook ran project-wide and
failed on optional-extra imports missing from a fresh worktree venv:

```
ModuleNotFoundError: No module named 'sentence_transformers'
ModuleNotFoundError: No module named 'transformers'
```

`pyrefly` then refused to type-check, the pre-commit hook exited
non-zero, every `git commit` failed. Nightly correctly refused
`--no-verify` (refusal policy: bypassing test or type safety) and
had no other path forward. Across two sessions, 51+ task attempts,
zero merged commits — Nightly's value chain broke at the worktree
boundary.

Two related observations:

1. **Fresh worktree venvs are reliably under-equipped.** `git worktree
   add` creates a working tree but does not re-install Python
   dependencies. Nightly's loop spawns a worktree per task and then
   invokes its host (`claude -p`, `codex exec`, etc.) inside it.
   Whatever the host's `uv sync` / `pip install` story was on the
   primary branch, the worktree starts blank.

2. **Hosts increasingly run heavyweight pre-commit hooks.** `pyrefly`
   project-wide, `mypy` cross-module, `tsc --noEmit`, `cargo check`
   — these all require a fully-resolved dependency graph and a
   working venv. The kind of project that has Nightly worth running
   on it is the kind that's most likely to have these.

The cascade-dedupe fix that landed in `43f5ac9` (issue #2) is the
right *symptom* fix — it stops the proposer from re-dispatching the
same blocked work — but doesn't change Nightly's actual throughput
in repos where the worktree is hostile. The real problem is that
Nightly has no notion of "is this worktree ready to commit?" and no
remediation when the answer is no.

## Non-goals

- **Bypassing failed hooks.** Refusal-policy category "Bypassing test
  or type safety" remains firm. `--no-verify` is never an option.
- **Modifying the operator's `.pre-commit-config.yaml`, `Makefile`,
  or hook scripts.** Those belong to the operator's repo. Mutating
  them would cross "scope creep" — Nightly's job is to be *runnable*
  in the operator's repo, not to redesign it.
- **Auto-merging or auto-fixing of hook code.** That's a separate
  feature with its own refusal-policy review.
- **Solving every possible hook failure.** Network egress, missing
  external services (databases, cloud APIs), corrupted git state,
  broken pyproject.toml — out of scope. The probe is best-effort
  and reports unremediable failures to the operator rather than
  trying to be clever.

## Proposed direction

Take this as a starting frame, not a commitment — concrete design
should happen when this RFC is sized.

### A new lifecycle phase: `READY`

Before any task dispatch on a fresh worktree, the driver runs a
**readiness probe**:

```
nightly worktree doctor [--remediate]
```

The probe:
1. Detects the host repo's pre-commit configuration (looks for
   `.pre-commit-config.yaml`, `package.json` scripts, `Makefile`
   targets like `check`/`lint`, `pyproject.toml [tool.pytest]`
   sections — same detection vocabulary as `nightly verify`).
2. Runs each detected check against a minimal touch-nothing input
   (e.g. `pre-commit run --files /dev/null`, or `make lint` from
   an empty diff). The goal is to surface *infrastructure* failures
   (missing deps, missing tools, broken hook config) without
   exercising the project's real test suite.
3. Classifies each failure into a small known set of remediation
   patterns:
   - `missing_python_dep` — `ModuleNotFoundError: No module named
     '<name>'` → try `uv sync --all-extras` (or equivalent for the
     repo's package manager).
   - `missing_binary` — `command not found` or `[Errno 2] No such
     file or directory: '<tool>'` → check `which <tool>`; if absent
     and the repo declares it (via `pre-commit-config.yaml`'s
     `repos: -repo: ...`), let pre-commit's own bootstrap install it
     (`pre-commit install --install-hooks`).
   - `hook_config_error` — yaml/toml parse errors, malformed hook
     entries → cannot remediate; surface to operator.
   - `unknown` — failure pattern doesn't match a known signature
     → cannot remediate; surface to operator.

### Remediation, scoped tightly

Only `missing_python_dep` and `missing_binary` are auto-remediated.
Both expand the *availability* of project-declared tools without
changing project files. The legal moves:

- `uv sync --all-packages --all-extras` (or `--frozen` if there's
  a uv.lock).
- `pip install -r requirements*.txt` if uv isn't in use.
- `pre-commit install --install-hooks` (the pre-commit framework's
  own bootstrap path).
- `npm ci` / `pnpm install` / `bun install` for JS hooks.

The probe **never** runs `pip install <somepackage>` based on parsing
an error message — only invokes the repo's declared installer. If the
declared installer fails to provide the missing dep, that's a
`hook_config_error` (the repo says it should have the dep but doesn't)
and gets surfaced to the operator.

### Cascade integration

A new cascade source: `worktree_blocked`. When `worktree doctor`
returns an unremediable failure, the next `nightly next` returns
`worktree_blocked` with the failure details, and the agent's loop
treats it as a proposal to scope a meta-task: "the worktree
infrastructure is broken; fix the hook config so Nightly can
commit." This task targets the host repo's hook config files
directly — a proposal-class task the operator reviews, not an
auto-PR.

The Stop hook's backpressure already exists to limit damage if this
ever fails — the cascade can't loop on `worktree_blocked` because
the loop guard from `d592974` catches it.

### `auto_pr_categories` extension

Currently only `lint_debt` and `dep_upgrade` clear the autonomy bar.
A new category `worktree_remediation` would name dependency-installation
PRs that the operator should review (not auto-merge). The
`worktree_blocked` cascade source produces tasks in this category.

## Open design questions

- **Per-worktree state caching.** Re-running the probe on every
  task dispatch is wasteful (every task in a clean repo would pay
  the cost). Where does "this worktree is ready" cache? A
  `.nightly/worktrees/<branch>/READY` marker? Time-bounded?
  Invalidated by what?
- **Detection scope creep.** "Run pre-commit with no files" is
  cheap, but doesn't catch every infrastructure failure (e.g.
  hooks that need network egress, hooks that fail only against
  real diff content). A second probe phase that exercises a
  minimal real change (`git commit --allow-empty -m test`) would
  catch more but pollutes git history. Punted to a follow-up.
- **Operator-controlled override.** Should there be a `.nightly/config.yml`
  setting to disable the probe per-repo? Probably yes for repos where
  the probe's false positives are noisier than its catches. Same
  shape as `pr_feedback.enabled`.
- **Cross-host portability.** The probe ships in nightly-core (host-
  agnostic), but the remediation commands are language-stack-specific.
  Where does the language detection live? Reuse `nightly verify`'s
  detection vocabulary, or roll a separate `worktree_doctor` module?
- **Interaction with `nightly verify`.** `nightly verify` already
  runs the repo's linters/formatters. Is `worktree doctor` just
  `nightly verify --infra-only`? Or does it have a wider scope (e.g.
  detecting that the worktree's venv doesn't exist at all)?
- **Refusal-policy expansion.** Adding `worktree_remediation` to the
  autonomy bar requires updating the brainstorm §06 documentation
  and the test that locks `AUTO_PR_CATEGORIES`.

## Risks

- **Over-remediation.** If `uv sync --all-extras` pulls in heavy GPU
  deps just to satisfy a hook, every fresh worktree pays a multi-minute
  cost. Mitigation: detect which extras the failing imports actually
  need, install only those. Per-extra remediation is more code.
- **False positives.** A hook fails on the empty input due to a
  real bug in the hook itself, not infrastructure. Mitigation: only
  remediate when the failure matches a known signature; surface
  everything else.
- **Operator surprise.** The probe runs `uv sync` or `pre-commit
  install --install-hooks` without asking. Mitigation: log the
  remediation steps taken in `keepalive.log` + the morning briefing
  so the operator sees every invocation.
- **Reaching past the worktree.** If the operator's primary `main`
  has the broken hook, fixing only the worktree means the next PR
  Nightly opens against `main` will get blocked by the same hook in
  CI. The worktree-remediation task (`worktree_blocked` cascade
  source) should target the host repo's hook config so the operator
  can merge the fix and unblock the queue.

## Implementation sketch

1. **`packages/nightly-core/src/nightly_core/worktree.py`** — add a
   `probe_worktree_readiness(root) -> WorktreeReadiness` function that
   returns a typed result (`ok`, `remediable`, `blocked`).
2. **CLI**: `nightly worktree doctor` (the user-visible probe) +
   `nightly worktree doctor --remediate` (try the fixes).
3. **`cascade.py`**: new `pick_worktree_blocked()` step + a new
   `worktree_blocked` source in `CascadeSource`. Slot it before
   `pick_in_flight` (it's an infrastructure prerequisite).
4. **`autonomy.py`**: add `worktree_remediation` to `AUTO_PR_CATEGORIES`
   if we decide it's eligible. Tests lock it.
5. **`driver.py`**: before `worktree.create_worktree`, invoke the
   probe; on `remediable`, run the remediation; on `blocked`, scope
   a `worktree_remediation` proposal and skip the original task.
6. **`config.yml`**: `worktree.probe_enabled: true` (default on),
   `worktree.remediate_enabled: true` (default on). Hook surface for
   operator opt-out.

## Checklist (for promotion to `accepted`)

Leave unchecked until a human author has decided to ship this.

- [ ] Decide whether `worktree doctor` is a new module or
      `nightly verify --infra-only`.
- [ ] Spec the per-worktree readiness cache (path, invalidation).
- [ ] Spec the `worktree_blocked` cascade source — does it really
      slot before `pick_in_flight`, or somewhere later?
- [ ] Decide whether `worktree_remediation` clears the autonomy bar.
      Probably no — operator should review dep changes — but the
      design depends on the answer.
- [ ] Define the remediation pattern set authoritatively; tests
      lock the recognized error signatures.
- [ ] Write characterization tests against the corpus-forge
      pre-commit failure bundle.
- [ ] Update `.planning/brainstorm.html` §06 refusal-policy section
      with the carveout for dependency-installation remediation
      (not a "bypass" — *making* the test runnable).
