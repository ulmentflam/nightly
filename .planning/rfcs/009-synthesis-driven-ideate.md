---
status: accepted
sized: true
title: Synthesis-driven ideate — codebase-wide proposal generation across five categories
created: 2026-06-05
sized_on: 2026-06-05
accepted_on: 2026-06-05
author: nightly-seed
source: interactive_seed
estimated_effort: ~10h across 3 phases
---

# RFC 009 — Synthesis-driven ideate

## Status

`accepted` — operator-reported regression on 2026-06-05: "Ideate is
not running again. Ideate should read the codebase again and make
recommendations, starting with cleaning, refactoring and house
keeping, and ending with new convenience features or new capabilities
that would improve user experience, performance speed, etc... based
on the objectives of the project." Approach C (hybrid: keep the three
narrow programmatic proposers, add one LLM-driven synthesis proposer)
ships as v1. Phase A wires the synthesis proposer + a new
`SynthesisProposal` shape; Phase B threads the five-category ordering
into the cascade pick and the morning briefing; Phase C adds a
cache/throttle layer so synthesis doesn't run on every cascade walk.

## Context

The Phase 5 proposer suite ships three proposers:

- `TodoFixmeProposer` — greps `TODO:` / `FIXME:` markers.
- `LintDebtProposer` — runs `ruff` and surfaces auto-fixable findings.
- `TypeHoleProposer` — greps for `Any` at module boundaries.

All three are **programmatic and narrow**. They're correctness
checkers, not strategists. Their proposals are uniform-shaped ("tighten
N `Any` usages in `<file>`"), they re-detect the same signal every
cascade pass (mitigated by fingerprint dedupe from issue #2), and they
have nothing to say about *the actual shape of the project* — they
don't read the README, they don't read the RFCs, they don't know that
this is a host-native autonomous coding agent.

Concretely, when ideate runs today on this very repo, it returns:

```
 1.60   skip  type_holes      Tighten 3 `Any` usage(s) in packages/nightly-core/src/nightly_core/config.py
 1.60   skip  type_holes      Tighten 3 `Any` usage(s) in packages/nightly-core/src/nightly_core/ideation.py
 1.25   skip  todo_fixme      Audit 1 TODO/FIXME marker(s) across 1 file(s)
 1.20   skip  type_holes      Tighten 1 `Any` usage(s) in packages/nightly-core/src/nightly_core/...
 1.20   skip  type_holes      Tighten 1 `Any` usage(s) in packages/nightly-core/src/nightly_core/...
```

Five proposals; zero clear the auto-PR autonomy bar; all are
"clean up a `Any` in a config file" or "audit a TODO." None of them
are the cleaning, refactoring, housekeeping, convenience, or
capability work that would actually move the project forward.

The operator's directive frames the missing tier of ideation as five
ordered categories:

1. **Cleaning** — dead code, unused imports beyond linter scope,
   redundant tests, stale comments, abandoned scaffolding.
2. **Refactoring** — long functions that should split, repeated
   patterns to extract, modules that have outgrown their boundary,
   classes that should merge or pull apart.
3. **Housekeeping** — naming inconsistency, file layout drift, doc
   gaps, missing or stale type hints (beyond `Any`-at-boundary),
   missing tests for non-trivial code.
4. **Convenience features** — CLI shortcuts, better error messages,
   auto-completion, friendlier output formats, missing-but-obvious
   verbs, configuration ergonomics.
5. **New capabilities** — performance / speed improvements, new
   cascade sources, new specialist roles, new proposers,
   integrations the project's objectives would clearly benefit from.

The first three are *cleanup*; the last two are *forward motion*.
The ordering matters because the user wants the ideate output to read
like a real product-strategy review — fix what's broken before
inventing new things.

The synthesis pass that produces this ordering is fundamentally an
LLM job. No grep / lint / type-checker can read README + RFCs +
code together and propose "the CLI's error messages don't mention
the recovery command — that's a convenience gap" or "the cascade
walks 50ms of grep per pick which compounds at MAX_TURNS; cache it."
This RFC adds that capability.

## Non-goals

- **Replacing the three existing proposers.** They still ship.
  `TodoFixmeProposer` / `LintDebtProposer` / `TypeHoleProposer` are
  cheap, programmatic, deterministic, and good at what they do.
  Synthesis runs alongside them, not instead of them.
- **Cross-host synthesis equivalence.** The synthesis proposer
  spawns the *current* host's headless CLI (`claude -p`, `codex
  exec`, etc.) — there's no attempt to normalize output quality
  across vendors. Each host gets the synthesis its model produces.
- **Multi-pass refinement / interactive synthesis.** v1 is
  one-shot: write a prompt, parse the output, materialize the
  proposals. No "ask the LLM to critique its own proposals" pass.
  Future work if v1 quality is too low.
- **Proposer-suite plug-in API for external authors.** The
  synthesis proposer ships in-tree with the rest. Third-party
  proposers as a public API is a v3+ concern.
- **Cost telemetry / budgeting.** Synthesis costs more than the
  three narrow proposers because it spawns a host process. v1
  doesn't add a `$-spent` counter; the throttle (Resolved #6)
  caps frequency, which is the cost lever that matters here.
- **Synthesis during `nightly run` headless.** Same throttle
  applies; we don't disable synthesis for headless runs, but the
  throttle prevents it from firing on every iteration.

## Proposed direction

Three approaches; **Approach C** ships as v1.

---

### A — Add more narrow programmatic proposers

Ship `DeadCodeProposer` (find unimported public symbols),
`FunctionLengthProposer` (flag functions over N lines),
`RepeatedPatternProposer` (find near-duplicate blocks),
`TestCoverageGapProposer` (find code paths without tests),
`CLIConsistencyProposer` (find inconsistent help text / arg
patterns). Pure grep / AST / coverage-tool work; no LLM
involvement.

**Pros:**
- Programmatic, deterministic, cheap. Same shape as the existing
  three proposers — easy to extend the test suite, easy to
  fingerprint-dedupe.
- Predictable output. The operator knows what each proposer does
  and can opt one out per-repo via config.

**Cons:**
- Still doesn't address the operator's actual ask. The five new
  proposers would produce more nits, not strategic suggestions.
  "Function `foo` is 87 lines long" is a refactoring hint, but it
  doesn't read like "the `cascade.next_task` function has nine
  branches that should probably be a table-dispatch lookup" — that's
  the synthesis voice.
- No "new capability" proposals possible from pure code analysis.
  Nothing in the file tree says "the CLI should grow a `nightly
  watch` verb"; that requires reading the README to understand the
  loop and then noticing the gap.

---

### B — Single LLM synthesis proposer replacing the three narrow proposers

Drop the three Phase-5 proposers entirely. The synthesis proposer
reads the README, RFCs, AGENTS.md, CLAUDE.md, and a code summary,
and produces all proposals — cleaning through capabilities.

**Pros:**
- Cleaner mental model. One proposer; one output stream.
- LLM can synthesize across categories — a "function `foo` is too
  long AND the abstraction `Bar` could pull two of its branches
  out" beats two separate narrow proposers each seeing half.

**Cons:**
- LLM proposals are non-deterministic across runs. The
  fingerprint-dedupe filter (issue #2's fix) assumes stable
  proposal fingerprints; two synthesis runs may produce two
  near-identical-but-not-identical proposals that the dedupe
  misses.
- LLM cost on every cascade walk. Without a throttle this gets
  expensive fast — synthesis runs as a sub-process spawn, parses
  output, and may run dozens of times per session.
- Loses the cheap, predictable static-analysis hits. Today
  `lint_debt` reliably surfaces auto-fixable nits that the
  synthesis proposer might overlook.

---

### C — Hybrid: keep the three narrow proposers, add a synthesis proposer

Synthesis runs alongside the existing three. The three narrow
proposers continue to produce their predictable static-analysis
output; synthesis adds the strategic-review layer. The cascade
picks across the combined set by score; the briefing groups output
by category.

**Pros:**
- Preserves what works (the three narrow proposers).
- Adds what's missing (codebase-wide synthesis).
- Five-category ordering (cleaning → refactoring → housekeeping →
  convenience → capability) becomes a *score-modifier* the
  cascade respects: a "cleaning" proposal at score 1.2 outranks a
  "capability" proposal at score 1.2 because cleaning comes first.
- Throttle (Resolved #6) limits LLM cost to once-per-session by
  default.

**Cons:**
- Two output styles in the proposer suite — programmatic and
  LLM. The briefing renderer has to handle both. Test surface
  grows for two parallel shapes.
- Synthesis proposals are non-deterministic; we need a more
  permissive fingerprint to match "same proposal, different
  wording." Resolved #5 addresses this.

---

## Resolved technical decisions

**1. Approach C ships as v1.** Approach A was rejected because it
doesn't address the operator's ask — more nits aren't synthesis.
Approach B was rejected because losing the cheap programmatic
proposers (which catch real, fingerprintable nits) for an LLM
single-track is a regression on what works. Approach C preserves
the existing surface and adds the strategic layer.

**2. The synthesis proposer is `SynthesisProposer`, registered in
`proposers/registry.py`.** Lives at `packages/nightly-core/src/
nightly_core/proposers/synthesis.py` alongside the existing three.
Implements the same `Proposer` protocol from `proposers/base.py`
(returns `list[Proposal]`). The cascade and the morning briefing
require no special-casing — synthesis proposals are just
proposals with a different `proposer` field value.

**3. Five categories tagged on the proposal.** New `Proposal`
field: `category: ProposalCategory` (Literal type). Values:
- `"cleaning"` — dead code, redundant tests, abandoned scaffolding.
- `"refactoring"` — long functions, repeated patterns, boundary
  drift.
- `"housekeeping"` — naming, layout, doc gaps, type-hint gaps.
- `"convenience"` — CLI ergonomics, error messages,
  auto-completion.
- `"capability"` — new cascade sources / specialists / proposers /
  performance.

The three Phase-5 proposers backfill the field: `LintDebtProposer`
→ `housekeeping`, `TodoFixmeProposer` → `housekeeping`,
`TypeHoleProposer` → `housekeeping` (each is a different shade of
housekeeping work — they don't earn `cleaning` or `refactoring`
status because they're individual-line nits, not structural
review).

**4. Category outranks raw score in the cascade.** Today's
`pick_ideated_fallback` returns `proposals[0]` (already score-sorted
desc). v0.0.6+ sort is **(category_rank, -score)** where
`category_rank` is the five-tuple index above. A `cleaning`
proposal at score 1.2 outranks a `capability` proposal at score
1.8 because cleaning ships first in the operator's stated
ordering. Operators who want score-only ordering can opt out via
`ideate.category_ordering: false` in `.nightly/config.yml`
(Resolved #8).

**5. Synthesis proposals carry a longer, content-hashed
fingerprint.** Today's `Proposal.fingerprint` is
`f"{proposer}:{category}:{file_scope[0]}"` which is fine for
deterministic narrow proposers but too coarse for synthesis (two
runs may produce two different "refactor `cascade.next_task`"
proposals that should dedupe). The synthesis proposer composes
the fingerprint from:

```python
f"synthesis:{category}:{sha256(title + sorted_file_scope)[:12]}"
```

The title-hash means two synthesis runs that propose "refactor
`cascade.next_task` to use table dispatch" with identical wording
dedupe; two runs that propose the same conceptual refactor with
different wording fingerprint differently and both show up. The
operator decides which one is better at morning-review time.

**6. Throttle: synthesis runs at most once per session unless
`--force`.** A `.nightly/runs/<id>/synthesis.json` cache holds the
last run's output. Subsequent cascade walks read the cache instead
of re-spawning the host CLI. Cache invalidates when:
- `--force` flag is passed to `nightly propose` / `nightly ideate`.
- A new commit lands on `main` between cascade walks (cache stamps
  the SHA at write-time; mismatch invalidates).

Default throttle keeps cost bounded for long overnight runs. The
narrow proposers keep running on every cascade walk (they're cheap
enough to not need throttling).

**7. Synthesis spawns the host's headless CLI.** Implementation
uses the same `host.run_headless(prompt, cwd=root, timeout_s=N)`
surface the `nightly headless` command already exposes (see
`packages/nightly-host-claude/src/nightly_host_claude/integration.py`).
Prompt template lives at `packages/nightly-core/src/nightly_core/
proposers/synthesis_prompt.md`; it instructs the spawned model to:
- Read `README.md`, `CLAUDE.md`, `.planning/rfcs/*.md` (objectives).
- Read the codebase summary (auto-generated `ls -R packages/` +
  file-size pass; LLM doesn't need every line).
- Emit a structured JSON array of proposals, each with `category`,
  `title`, `description`, `file_scope` (list), `estimated_loc`,
  and `rationale` (linking the proposal back to a project
  objective from README / RFC text).
- Order categories in the five-category sequence; emit at least
  one proposal per category when applicable; cap total proposals
  at 25 to keep the morning briefing readable.

Output is parsed with `json.loads`; parse failures are logged and
return an empty list (synthesis degrades silently to the narrow
proposers).

**8. Config schema additions in `.nightly/config.yml`:**

```yaml
ideate:
  category_ordering:  true            # Resolved #4 opt-out
  synthesis:
    enabled:          true            # disable to skip the LLM spawn entirely
    timeout_seconds:  120             # cap host CLI wall-clock
    max_proposals:    25              # cap total synthesis output
```

`load_ideate_config(root) -> IdeateConfig` helper in
`nightly_core.config`, mirroring the existing per-feature config
patterns.

**9. Synthesis defaults to the host's primary model in v0.0.6;
RFC 007's model-tier routing wires it to `reasoning` tier in a
follow-up.** RFC 007 (model-tier routing) is accepted but not yet
implemented. When it lands, this proposer's specialist
registration adds `tier="reasoning"` to its default config so
synthesis runs on the operator's reasoning-tier model (Opus 4.7 /
GPT-5-reasoning / Gemini 3.5 Pro). Until then, synthesis runs on
the host's default model — operators can still gate cost via the
config `enabled: false` flag or by lowering the per-host default
model.

**10. Morning briefing surfaces the synthesis pass distinctly.**
The briefing's "Proposed issues" section gains a category-grouped
sub-section: synthesis proposals appear under
`Cleaning (3) · Refactoring (2) · Housekeeping (5) · Convenience (1)
· Capability (2)` headers, with the narrow programmatic proposals
under a "Static-analysis hits" sub-section below. Lets the operator
read the strategic review separately from the linter nits.

**11. The synthesis prompt explicitly anchors proposals to the
project's stated objectives.** From the prompt template:

> *"Read README.md to extract the project's stated objectives.
> For each proposal you generate, the `rationale` field must
> reference one or more of those objectives explicitly — e.g.
> 'this convenience proposal makes the
> "cross-host suspend/resume" objective more accessible to first-
> time operators.' Proposals whose rationale doesn't connect back
> to an objective should be dropped."*

This is the load-bearing piece that keeps synthesis honest. Without
it the LLM tends to propose generic best-practice suggestions
("add type hints to all functions") that don't earn their slot.

## Risks

- **LLM cost.** Even with the throttle, a long overnight run that
  hits the once-per-session synthesis pass costs an extra
  `~timeout_seconds × tokens-per-second × model-cost` per session.
  Mitigation: `enabled: false` opt-out at the config level;
  `synthesis.timeout_seconds` caps wall-clock; `max_proposals`
  caps output. RFC 007's tier routing eventually means
  cost-sensitive operators can route synthesis to a cheaper
  reasoning-tier model.

- **Non-determinism in proposal wording.** Two synthesis runs at
  different times may produce two near-identical-but-not-identical
  proposals for the same underlying issue. The content-hashed
  fingerprint (Resolved #5) reduces but doesn't eliminate this.
  Mitigation: the operator's morning-review pass naturally dedupes
  via human judgment; the briefing's category grouping (Resolved
  #10) clusters related proposals visibly.

- **LLM proposes work the refusal policy bars.** A synthesis
  proposal that suggests "add a `--no-verify` flag to bypass
  pre-commit" or "push directly to main" would violate the
  refusal-policy categories (destructive git / bypass test). The
  prompt template (Resolved #7) includes the refusal-policy
  constraints so the LLM knows the boundaries. Proposals that
  violate them anyway are caught at materialization time — the
  cascade's auto-PR autonomy bar already requires single-file
  scope and < 80 LOC, so refusal-bait proposals fall through to
  the operator's morning review.

- **Synthesis stalls a cascade walk.** A 120-second host CLI
  spawn blocks `nightly next` while it runs. The cascade caller
  expects sub-second latency. Mitigation: synthesis runs *only*
  inside `nightly ideate` (not during cascade walks); the cascade
  reads the cached `synthesis.json` and the propose-suite returns
  cached results to the cascade's `pick_ideated*` calls.

- **Cache invalidates too eagerly.** A new commit on `main`
  invalidates the synthesis cache (Resolved #6), but most cascade
  walks don't follow a fresh `main` commit. The cache should
  survive across walks within the same session. Mitigation:
  cache lives under `.nightly/runs/<id>/`, so a new run gets a
  fresh cache by default; within a run, cache survives unless
  `--force` is passed.

- **Prompt-template drift.** The prompt template lives in the
  package; tweaking it changes ideate output across all installs
  at upgrade time. Mitigation: tests assert the prompt contains
  the load-bearing constraint strings (objectives, refusal
  policy, five-category ordering); `nightly doctor` extends to
  flag drift when the operator's installed prompt differs from
  the package version.

## Implementation phases

Three phases, ~10h total.

### Phase A — SynthesisProposer + category tagging (~5h)

- **A1.** New `ProposalCategory` Literal in `nightly_core.proposers.base`:
  `"cleaning" | "refactoring" | "housekeeping" | "convenience" |
  "capability"`. `Proposal` gains a `category: ProposalCategory`
  field (default `"housekeeping"` for backward compat).
- **A2.** Backfill the field on the three existing proposers
  (`TodoFixmeProposer`, `LintDebtProposer`, `TypeHoleProposer`) →
  all emit `category="housekeeping"`.
- **A3.** New `packages/nightly-core/src/nightly_core/proposers/
  synthesis.py` with `SynthesisProposer` that:
  - Spawns the current host via `run_headless` with the prompt
    template + a `--cwd` pointing at repo root.
  - Parses JSON output into `list[Proposal]`.
  - Computes content-hashed fingerprints per Resolved #5.
  - Returns empty list on any failure (parse, timeout, host
    missing) — logged at WARN.
- **A4.** New `packages/nightly-core/src/nightly_core/proposers/
  synthesis_prompt.md` — the literal prompt template the proposer
  feeds the host. Contents per Resolved #7 + #11.
- **A5.** Register `SynthesisProposer()` in
  `proposers/registry.py::default_proposers()`.
- **A6.** Unit tests: prompt template contains all load-bearing
  constraint strings (objectives, refusal policy, five-category
  ordering, JSON output schema); synthesis-output parser handles
  well-formed JSON, malformed JSON, empty array, and truncated
  output; backfilled categories on the three existing proposers
  remain stable.

**Merge gate for Phase A:** synthesis proposer + prompt land; the
three existing proposers still emit deterministic output; existing
ideation tests still pass.

### Phase B — Five-category ordering in cascade + briefing (~3h)

- **B1.** `cascade.pick_ideated` / `pick_ideated_fallback` sort
  proposals by `(category_rank, -score)` per Resolved #4.
- **B2.** New `_category_rank` constant in `cascade.py`:
  `{"cleaning": 0, "refactoring": 1, "housekeeping": 2,
  "convenience": 3, "capability": 4}`.
- **B3.** `load_ideate_config` helper in `nightly_core.config`
  with the schema from Resolved #8. `category_ordering: false`
  bypasses the sort and falls back to score-only.
- **B4.** Briefing renderer (`briefing.py` + Jinja template)
  groups "Proposed issues" by category with the five sub-section
  headers; static-analysis hits get their own sub-section below.
- **B5.** Tests: category ordering changes the cascade pick when
  enabled; falls back to score-only when disabled; briefing
  renders correctly with mixed-category synthesis output.

**Merge gate for Phase B:** Phase A merged; cascade routing changes
documented in CASCADE_SOURCES rationale text; briefing tests
green.

### Phase C — Throttle / cache + `nightly doctor` integration (~2h)

- **C1.** `.nightly/runs/<id>/synthesis.json` cache shape:
  `{"head_sha": "<short SHA>", "ran_at": "<ISO>", "proposals":
  [...]}`. `SynthesisProposer` reads this before spawning; if
  present + SHA matches + not `--force`, returns the cached
  proposals.
- **C2.** `nightly propose --force` / `nightly ideate --force`
  flags that bypass the cache.
- **C3.** `nightly doctor` extends with a prompt-template-drift
  check: the installed `synthesis_prompt.md` must contain the
  Resolved-#11 anchor text. Drift triggers re-install from the
  package source via the same `_REQUIRED_SKILL_TOKENS` pattern
  RFC 005 §B4 established for host skills.
- **C4.** README documentation: a new "Synthesis-driven ideate"
  paragraph in the proposer-suite section.
- **C5.** Tests: throttle survives within-session repeats; `--force`
  bypasses; SHA mismatch invalidates; doctor flags stale prompt
  template.

**Merge gate for Phase C:** Phases A + B merged; throttle measured
to keep synthesis to ≤ 1 spawn per session under default config;
doctor surfaces prompt drift.

## Sized checklist

**Phase A — SynthesisProposer + category tagging**
- [ ] A1. `ProposalCategory` Literal + `Proposal.category` field
- [ ] A2. Three existing proposers backfilled to `category="housekeeping"`
- [ ] A3. `SynthesisProposer` in `proposers/synthesis.py` with `run_headless` spawn + JSON parsing + content-hashed fingerprints
- [ ] A4. `synthesis_prompt.md` template with objectives anchor + refusal-policy constraints + five-category schema
- [ ] A5. `SynthesisProposer()` registered in `default_proposers()`
- [ ] A6. Unit tests covering prompt content, output parsing, fingerprint stability, backfill correctness

**Phase B — Five-category ordering + briefing**
- [ ] B1. Cascade sort by `(category_rank, -score)` in `pick_ideated*`
- [ ] B2. `_category_rank` constant in `cascade.py`
- [ ] B3. `load_ideate_config` helper + opt-out flag
- [ ] B4. Briefing grouping by category + static-analysis sub-section
- [ ] B5. Tests covering ordering, opt-out, briefing render

**Phase C — Throttle + cache + doctor integration**
- [ ] C1. `synthesis.json` cache shape + read path
- [ ] C2. `--force` flag on `nightly propose` / `nightly ideate`
- [ ] C3. `nightly doctor` prompt-template-drift check
- [ ] C4. README documentation paragraph
- [ ] C5. Tests covering throttle, `--force`, SHA invalidation, doctor drift
