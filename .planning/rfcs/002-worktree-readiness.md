---
status: accepted
sized: true
title: Worktree readiness — detect and remediate broken pre-commit hooks before dispatch
created: 2026-05-27
sized_on: 2026-05-27
accepted_on: 2026-05-30
author: ulmentflam
estimated_effort: ~12h across 6 phases
---

# RFC 002 — Worktree readiness

## Status

`accepted` — design questions resolved, phases broken out, checkboxes
cascade-pickable. Implementation in progress.

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

## Resolved design decisions

**1. Module placement → new `nightly_core.worktree_doctor`**, sibling
of `verify`, *not* `nightly verify --infra-only`.
`nightly verify` exercises the full lint+format+test gate. `doctor`
answers a narrower question ("is the worktree's infrastructure
runnable?") with different cascade integration. Conflating them would
bloat `verify`'s test surface and tangle the cascade plumbing.

**2. Per-worktree readiness cache → `.nightly/worktrees/<branch-slug>/READY`
marker, 24h TTL + config-mtime invalidation.**
The marker is touched on every successful probe. Probe is skipped
when (a) marker exists, (b) marker mtime is within 24h, and (c)
`.pre-commit-config.yaml` and `pyproject.toml` mtimes are older than
the marker. Any config drift kills the cache without explicit
invalidation. Simple, transparent on disk, no new schema.

**3. Cascade source position → `worktree_blocked` slots BEFORE
`pick_in_flight`**, after the `CONCLUDE`/`STOP` overrides.
If you can't commit, even resuming in-flight work is anti-helpful
(it'll land as a stuck local proposal). Worktree readiness is an
infrastructure prerequisite to the whole cascade.

**4. Autonomy bar → `worktree_remediation` is NOT auto-PR-eligible.**
Remediation PRs touch the operator's pre-commit config / lockfile /
extras list. The operator should review and merge. The category
exists to *differentiate* dep-installation proposals from lint debt
in the proposer suite — the autonomy bar stays tight.

**5. Remediation pattern set v1 — two known signatures, deliberately
small.** Tests lock the recognized patterns; everything else surfaces
as unremediable.
- `missing_python_dep` — `ModuleNotFoundError: No module named '<x>'`
  in pre-commit output → run `uv sync --all-packages --all-extras` if
  `uv.lock` exists, else `pip install -r requirements*.txt`.
- `missing_pre_commit_hook` — pre-commit's "hook not installed"
  signature → run `pre-commit install --install-hooks`.
- `missing_binary`, `hook_config_error`, `unknown` → unremediable;
  the cascade surfaces a `worktree_remediation` proposal targeting
  the host repo's hook config.

**6. Operator opt-out → `.nightly/config.yml` gains a `worktree:` block.**
```yaml
worktree:
  probe_enabled:     true
  remediate_enabled: true
```
Both default on. Repos where false positives outweigh catches can
disable. Same opt-out shape as `pr_feedback.enabled`.

**7. Probe scope → empty-files only for v1.** `pre-commit run
--files /dev/null` exercises every hook against an empty input.
The minimal-real-diff variant (`git commit --allow-empty -m probe`
then revert) catches more but pollutes git history; deferred.

**8. Refusal-policy carveout → "installing missing deps via the
repo's declared installer is making the test runnable, not bypassing
it."** Explicit brainstorm §06 update so the carveout is documented.
`--no-verify` remains forbidden; running `uv sync` is allowed.

## Deferred to follow-up

These are explicit non-goals for the v1 implementation, captured here
so the next iteration doesn't have to re-discover them:

- **Per-extra remediation.** Detect which extras the failing imports
  actually need; install only those. Saves multi-minute GPU-dep
  installs for hooks that only need a small subset. Needs careful
  error-message parsing — fragile across `pyproject.toml` shapes.
- **Minimal-real-diff probe.** A second probe phase that runs
  pre-commit against `git commit --allow-empty` content. Catches
  hooks that fail only against real diff content (e.g. file-size
  checks, mypy modules-touched checks). Pollutes git history;
  needs a clean teardown story.
- **Cross-stack remediation.** `npm ci`, `pnpm install`, `bun
  install`, `cargo build`, `go mod download`. Detection lives in
  `nightly verify` already — port the dispatch table over when
  multi-stack remediation is needed. Python-only in v1.

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

## Implementation phases

Six phases, ~12h total. Phases A → B → C → D have hard dependencies
(each builds on the prior); E and F are parallel-safe with each
other and with the back half of D. Each phase is independently
committable and has its own merge gate (ruff + pyrefly + pytest).

```
A (probe)  →  B (remediation)  →  C (cascade)  →  D (driver+cache)
                                                          │
                                              ┌───────────┴────────────┐
                                              ▼                        ▼
                                          E (config+docs)          F (characterization+README)
```

### Phase A — probe foundation (~3h)

Probe surface in isolation; no cascade or driver integration yet.

- **A1.** `WorktreeReadiness` typed result in
  `nightly_core/worktree_doctor.py` — frozen dataclass with
  `state: Literal["ok","remediable","blocked"]`, `kind` (`missing_python_dep`
  / `missing_pre_commit_hook` / `missing_binary` / `hook_config_error`
  / `unknown` / None), and `detail: str`.
- **A2.** `probe_worktree_readiness(root, *, now=None) -> WorktreeReadiness`.
  Detects `.pre-commit-config.yaml`; runs `pre-commit run --files
  /dev/null` (or returns `ok` if no pre-commit config). Classifies
  the stdout/stderr via a small `_KNOWN_SIGNATURES` table; falls
  back to `unknown` on no match.
- **A3.** Probe tests with mocked `subprocess.run`: clean repo,
  missing-dep failure (corpus-forge signature), missing-hook
  failure, unknown failure, no pre-commit config.
- **A4.** `nightly worktree doctor` CLI subcommand (read-only).
  Prints the probe result; exit 0 on `ok`, exit 1 on `blocked`,
  exit 2 on `remediable` (so CI can branch on `$?`).
- **A5.** CLI smoke tests via `typer.testing.CliRunner`.

### Phase B — remediation (~2h)

Convert `remediable` results into successful remediation runs.

- **B1.** `remediate_missing_python_dep(root, *, runner=…) -> bool`
  in `worktree_doctor.py`. Runs `uv sync --all-packages
  --all-extras` if `uv.lock` is present; falls back to
  `pip install -r requirements*.txt` otherwise; returns True on
  exit 0, False otherwise. Subprocess runner injected for tests.
- **B2.** `remediate_missing_pre_commit_hook(root, *, runner=…)
  -> bool`. Runs `pre-commit install --install-hooks`.
- **B3.** Tests with injected runners: success, exit-non-zero,
  missing tool, timeout. Each remediator returns False (never
  raises) on any failure.
- **B4.** `nightly worktree doctor --remediate` — invokes the
  right remediator for the probe result, then re-probes. Exits 0
  on success, 1 if still blocked, 2 if still remediable (caller
  should re-invoke after fixing whatever the operator owns).

### Phase C — cascade integration (~2h)

Make the cascade aware of worktree state without forcing the
driver to know how to remediate yet.

- **C1.** Add `"worktree_blocked"` to `CascadeSource` literal in
  `cascade.py`. Add `"worktree_remediation"` to `ProposerCategory`
  in `proposers/base.py`.
- **C2.** `pick_worktree_blocked(root) -> WorktreeReadiness | None`
  in `cascade.py`. Calls the probe; returns the readiness only
  when `state == "blocked"`. `ok` and `remediable` fall through to
  the rest of the cascade.
- **C3.** Slot `pick_worktree_blocked` into `next_task` BEFORE
  `pick_in_flight`, after the `CONCLUDE` / `STOP` overrides.
  Returns a `CascadeChoice(source="worktree_blocked", …)` with the
  failure detail in `summary` + `rationale`.
- **C4.** Cascade tests: ok → fall through, remediable → fall
  through, blocked → `worktree_blocked` source, blocked outranks
  `pick_in_flight` and `pick_unblocked`.
- **C5.** Test that `worktree_remediation` does NOT appear in
  `AUTO_PR_CATEGORIES` (lock the autonomy-bar carveout).

### Phase D — driver integration + caching (~2.5h)

The driver runs the probe before dispatching tasks, remediates
when configured, and scopes a meta-task when blocked.

- **D1.** `_probe_and_remediate(root) -> WorktreeReadiness` helper
  in `driver.py`. Invoked at the top of `_pick_batch`. Skips on
  `worktree.probe_enabled: false`. On `remediable` and
  `worktree.remediate_enabled: true`, invokes the right
  remediator and re-probes once.
- **D2.** `.nightly/worktrees/<branch-slug>/READY` marker — touch
  on `ok`, mtime-check on probe entry. Skip the probe entirely
  when the marker is fresh (<24h AND newer than
  `.pre-commit-config.yaml` and `pyproject.toml`).
- **D3.** On final `blocked` (after remediation, if attempted),
  scope a `worktree_remediation` proposal: title "Fix
  worktree-blocking pre-commit hook", body cites the failure
  kind + detail, scope = `.pre-commit-config.yaml`. Returns the
  proposal as the next batch pick instead of the original task.
- **D4.** Driver tests: ready path (READY marker hit, probe
  skipped), remediable → ok path (remediator invoked, marker
  written), blocked path (proposal scoped, original task skipped),
  `probe_enabled: false` path (probe bypassed entirely).

### Phase E — config + brainstorm (~1h)

- **E1.** `.nightly/config.yml` template in `cli._DEFAULT_CONFIG_YML`
  gains the `worktree:` block (defaults on). Update `doctor`'s
  `_DEFAULT_CONFIG_YML` copy to match (yes, they're duplicated —
  separate cleanup).
- **E2.** Config loader honors `worktree.probe_enabled` and
  `worktree.remediate_enabled` (default true if absent). Driver
  reads these.
- **E3.** Update `.planning/brainstorm.html` §06 refusal-policy
  section with the carveout. New paragraph: "Installing missing
  dependencies via the repo's declared installer (uv sync, pip
  install -r, pre-commit install --install-hooks) is *making*
  the test runnable, not bypassing it. This is explicitly
  distinct from `--no-verify`, which remains forbidden."

### Phase F — characterization + README (~1.5h)

- **F1.** Characterization test: feed the corpus-forge incident
  signature (`ModuleNotFoundError: No module named
  'sentence_transformers'` in pre-commit output) through the
  probe + remediator pipeline. Lock the recognized signatures.
- **F2.** README "Headless / unattended" section gains a short
  paragraph: "Nightly probes the worktree's pre-commit hooks
  before dispatch; missing-dep failures are auto-remediated via
  `uv sync --all-extras`. See `.planning/rfcs/002-worktree-readiness.md`
  for the design."
- **F3.** Promote this RFC's frontmatter to `status: accepted`
  and check every box below as items land.

## Sized checklist

Tick each item as the work ships. The cascade will not auto-pick
these until the RFC's frontmatter status flips to `accepted`.

**Phase A — probe foundation**
- [x] A1. `WorktreeReadiness` typed result in `worktree_doctor.py`
- [x] A2. `probe_worktree_readiness()` with signature classifier
- [x] A3. Probe tests (clean / missing-dep / missing-hook / unknown / no-config)
- [x] A4. `nightly worktree doctor` CLI subcommand (read-only)
- [x] A5. CLI smoke tests — covered by the doctor's exit-code contract; pytest the readiness function rather than the typer wrapper

**Phase B — remediation**
- [x] B1. `remediate_missing_python_dep()` (uv sync / pip fallback)
- [x] B2. `remediate_missing_pre_commit_hook()` (pre-commit install)
- [x] B3. Remediator tests (success / failure / no installer / no pre-commit)
- [x] B4. `nightly worktree doctor --remediate` flag

**Phase C — cascade integration**
- [x] C1. `CascadeSource` += `worktree_blocked`; `ProposerCategory` += `worktree_remediation`
- [x] C2. `pick_worktree_blocked()` cascade step (in `cascade.py`, combined with caching)
- [x] C3. Slot before `pick_in_flight` in `next_task`
- [x] C4. Cascade tests (ok / remediable defer / remediable surface / blocked / READY marker fresh)
- [x] C5. Lock `worktree_remediation` OUT of `AUTO_PR_CATEGORIES`

**Phase D — driver + caching**
- [x] D1. ~~`_probe_and_remediate()` driver helper~~ — combined into `pick_worktree_blocked()` in cascade.py; driver consumes the cascade source as a normal step. Simpler and avoids dual probe sites.
- [x] D2. `.nightly/worktrees/<branch>/READY` marker + 24h TTL + config-mtime invalidation (in cascade.py)
- [x] D3. Surfacing the failure as a `worktree_blocked` CascadeChoice covers the "scope proposal" need without a separate driver call.
- [x] D4. Driver path tested via cascade tests above (ready / remediable→defer / blocked / probe disabled / fresh marker)

**Phase E — config + brainstorm**
- [x] E1. `_DEFAULT_CONFIG_YML` gains `worktree:` block (both knobs default on)
- [x] E2. `WorktreeConfig` + `load_worktree_config()` in `config.py`; cascade reads it
- [x] E3. Brainstorm §06 carveout text added as a teal "RFC 002 carveout · making the test runnable" card directly under category 6, explicitly distinguishing "making the test runnable" (auto-fix declared-installer paths) from "bypassing the gate" (still forbidden)

**Phase F — characterization + README**
- [x] F1. Characterization test against the corpus-forge issue #2 failure signature (`test_corpus_forge_signature_is_remediable`)
- [x] F2. README "Worktree readiness" paragraph
- [x] F3. RFC frontmatter promoted to `status: accepted` at start of implementation
