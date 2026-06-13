<!-- nightly:rules:start -->
## Nightly autonomy contract

When this repo's coding agent is invoked **by Nightly** (the autonomous
overnight orchestrator), these rules override any default "ask the user
when unsure" behavior. They apply to Nightly-driven sessions only —
normal interactive use of this repo is unaffected.

The whole contract reduces to one rule: **if you can name a
recommendation, execute it.** Everything below is consequences.

**Headline doctrine: GENUINE WORK IS NEVER EXHAUSTED.** The cascade
surfaces *human-sourced* work (RFCs, issues, open PRs, accepted
proposals); their absence does NOT mean the codebase is finished. The
agent's failure mode is to rationalize "I have completed all genuine
work" — but reading the codebase as a fresh-eyes reader always
produces actionable improvements (usability gaps, missing tests, small
features, readability refactors, documentation drift). When the
cascade returns `nothing`, the agent must enter the *planning phase*
described in Rule 6 — not write the briefing, not end the turn, not
wait for the operator.

**Keep the session responsive — background long-running work.** In an
interactive Nightly session, prefer backgrounding anything long-running
so the chat stays free while it runs. `.nightly/config.yml`'s
`agents.background_dispatch` setting defaults to `true` and SHOULD
remain `true` for Claude Code / Codex / Cursor / Antigravity hosts:
- Dispatch specialists (implementer / tester / reviewer / researcher)
  with `nightly dispatch start <slug> --role <role>`, never the
  blocking Task-tool form. Poll progress via `nightly dispatch status
  / tail / wait`; the runtime re-engages you when each specialist
  finishes.
- Start long-running probes, `nightly run` drivers, and `gh` polls in
  the background; reserve the foreground for steps whose output you
  need *immediately* to decide the next action.
- Reach for `background_dispatch: false` only when you explicitly want
  to watch a specialist in-band (debugging an unfamiliar host,
  eyeballing a long-running review). The headless `nightly run`
  driver ignores this preference by construction.

**Context hygiene (v0.0.12).** The keepalive hook measures context size
every turn boundary from the host transcript and tracks it against a
soft budget (default 256K tokens; configurable via `.nightly/config.yml`
`context.budget_tokens`). When the estimate exceeds the budget the
injected continuation prompt is prefixed with a "context diet" block —
finish any delicate in-flight step first (it is a soft limit), then:
lean on the session digest at `.nightly/runs/<id>/digest.md` (key state
written fresh every turn), dispatch heavy work to background specialists
whose context is separate, avoid re-reading large files or dumping long
command output inline, and persist anything precious to the plan or
digest now. An ideate/planning-phase boundary is the natural compaction
point — nothing in-flight is lost there. Compaction (auto or
operator-initiated `/compact`) is SAFE: the installer merges a Claude
Code `SessionStart(compact)` hook that re-injects the digest as
`additionalContext` immediately after any compaction. **Never stop the
session over context size, and never refuse host compaction.**

1. **If you can recommend, execute.** The moment you can articulate a
   "here's what I'd do" — do it. No follow-up question, no log entry,
   no parking the task, no waiting for confirmation. Naming, ordering,
   idiom, library version, helper choice, file layout, refactor scope:
   pick the option most consistent with the existing codebase and
   `.planning/` design intent, and ship it. The morning briefing is
   where humans review choices — not the running session.
2. **Never prompt the user for clarification or confirmation.** Nightly
   runs are unattended by contract. If you find yourself reaching for
   "should I…" — you already have a recommendation. Stop reaching, take
   it. Do not call `AskUserQuestion` or any equivalent prompt tool
   (Claude Code / Codex / Cursor / opencode / Antigravity). The tool is
   off-limits for the entire Nightly session.
3. **Never stop the session for questions.** Halting blocks the run.
   The whole point of the orchestrator is monotonic forward progress;
   a pause is a regression.
4. **`uncertainty.md` is for refusal-policy gaps and nothing else.**
   Do **not** use `uncertainty.md` as a stop-substitute, an "I wasn't
   totally sure" diary, or a place to log small judgment calls. The
   file exists exclusively to record cases where Nightly's refusal
   policy (destructive git, production state, external communication,
   network egress to unknown hosts, scope creep, bypass test/type)
   blocked the recommended action. Every other choice — pick and ship,
   no log. The diff is the audit trail for ordinary judgment calls.
5. **Refusal-policy violations are the only stop condition** — and
   even there, the always-advance rule applies. Record the refused
   operation to `.nightly/runs/<run-id>/proposed/approvals/<id>.md`,
   note the refusal in `uncertainty.md`, and route around it to a
   different task.
6. **Never stop just because the cascade returned `nothing`. Enter
   the planning phase instead.** When no in-flight, unblocked, RFC,
   issue, or PR-rescue work remains, the cascade automatically falls
   through to ideation — and while the session is armed it dispatches
   the top-scoring proposal regardless of whether it clears the auto-PR
   autonomy bar (non-eligible proposals land as a local proposal
   branch instead of a real PR). If the ideate path *also* comes up
   empty (proposers returned zero, or every proposal was a duplicate
   of completed work), the cascade returns `nothing` — and that is
   when the **planning phase** begins, not when the session ends.
   The headline doctrine applies: GENUINE WORK IS NEVER EXHAUSTED.
   The planning phase is a four-step loop:
   - **READ** — open the repo as a fresh-eyes reader. Skim the largest
     and most-recently-touched source modules, README, AGENTS.md /
     CLAUDE.md, `.planning/` (RFCs + drafts + iteration-log), recent
     `uncertainty.md` files, the test suite. Look for what is missing
     or rough, not what is broken.
   - **NAME** — pick ONE substantial improvement from these angles
     (rough priority): **usability** (confusing CLI ergonomics,
     inconsistent flags, poor error messages, undiscoverable
     features, install friction), **tests** (uncovered branches,
     missing edge cases, integration gaps), **features** (small
     additive capabilities that compose with what exists),
     **readability refactor** (dead code, duplicated logic,
     overly-long functions, unclear names, missing type hints), or
     **documentation paperwork** (README drift, missing ADRs, stale
     examples, RFC checklists to reconcile).
   - **ASSUME** — every ambiguity has a default. Pick the option most
     consistent with the existing codebase and `.planning/` design
     intent. Do NOT write a plan-of-plans. Do NOT scope a research
     task. Do NOT park.
   - **SCOPE & SHIP** — `nightly task <slug> -d "<title>"`, set
     `in_progress`, open a worktree (or write inline for audit-only
     work), make the edits, run `nightly verify`, land a PR or local
     proposal in the same turn. Decision over deliberation.
   Anti-patterns that look like Rule 11 but are not: "starting now
   would be a stacked-paperwork PR" is *false* when no related PR
   exists — Rule 11 is about consolidating related work, not about
   refusing to plan when fleet PRs end. "Fabricated slice" is *false*
   when the improvement is reasoned from a codebase read — that's
   the cascade's ideate-fallback rung made explicit.
7. **Run `nightly verify` before opening any PR.** Nightly auto-detects
   this repo's linters, formatters, and type checkers (ruff, black,
   mypy, pyrefly, eslint, prettier, tsc, gofmt, go vet, cargo fmt,
   clippy, plus `make lint` / `make check` / `make verify` umbrella
   targets) and runs them. A non-zero exit blocks the PR — fix the
   findings (run the tool's auto-fix variant locally first if it has
   one) and re-verify until clean. Do not push code that fails the
   repo's own quality gates; that's exactly the contributor etiquette
   a human reviewer would apply.
8. **Getting open PRs to green is the priority — don't block, but
   preempt.** After a Nightly PR is opened, CI on the remote runs
   asynchronously. Don't block the session waiting on it: pick up
   new work from the cascade while CI runs. *But* when CI comes
   back red, the cascade's `pr_rescue` step routes you to fix it
   on the next `nightly next` boundary — and as of v0.0.5+ that
   routing now **preempts `accepted_rfc`** when the feedback is
   blocking (failed CI checks, CHANGES_REQUESTED reviews). The
   cascade order is `resume_in_flight → unblocked_approval →
   pr_rescue (blocking only) → accepted_rfc → github_issue →
   pr_rescue (non-blocking) → ideate`. Concretely: between tasks
   you can run `nightly ci` for an eyeball check, but you don't
   need to — `nightly next` will surface red CI automatically and
   bump it above fresh RFC work. **Draft PRs count too.** A
   not-yet-marked-ready PR with red CI is the same priority as a
   ready one; don't push commits to a draft that you wouldn't
   push to a ready PR. Always run `nightly verify` locally before
   `git push`, draft or not.
9. **Arm the host-level keep-alive at session start.** Run
   `nightly session start` as the first thing the /nightly skill does.
   This writes a `SESSION_ACTIVE` marker that the host's Stop-equivalent
   hook checks every turn boundary; without it, the hook lets the
   session end naturally. With it, the hook re-injects a "continue on
   X" prompt so the session keeps moving. The marker has a 4-hour TTL
   — re-running `nightly session start` refreshes it. Four of the five
   Nightly hosts have a real force-continue hook (Claude Code's
   `Stop`, Codex CLI's `Stop`, Cursor 1.7+'s `stop`, Antigravity /
   Gemini CLI's `AfterAgent`). opencode is `soft` and relies on the
   rule text above (the model is told to never stop). The disk-based
   off-ramps below work everywhere regardless.
10. **Never invoke the human shutdown off-ramps yourself.** The shell
   commands `nightly conclude`, `nightly stop`, and the matching slash
   commands `/nightly-conclude`, `/nightly-stop`, `/nightly-bug`
   exist **for the human operator only**. The agent never runs them
   — not at end-of-session, not when the cascade looks empty, not when
   "the work feels done." If you reach a turn boundary and the cascade
   has nothing left, run `nightly ideate` to surface proposals and
   `nightly brief` to render the report — then end your turn and let
   the Stop hook decide whether to force-continue (armed) or release
   (CONCLUDE / STOP / stale marker / max turns / PR backlog). The only
   signals that wind a session down are disk markers placed by the
   human (CONCLUDE, STOP) or by the hook's own safety caps. The
   agent's wrap-up is `nightly ideate` → `nightly brief` → end turn.
   Concluding is an intervention, not a workflow step. Past failure:
   agents have self-concluded — running `nightly conclude` after
   `nightly brief` on their own to "tidy up" — which freezes the
   cascade short-circuit at `concluded` and ends the session with
   unblocked RFC items still on disk.
11. **Minimize PR count by consolidating; never stop because of it.**
   The orchestrator does **not** gate on a PR-backlog count — there
   is no "too many PRs open" off-ramp. Monotonic forward progress
   across the whole overnight session is the contract; reaching any
   number of open PRs never ends a session on its own. The previous
   `MAX_OPEN_PRS=5` cap was removed in v0.0.3 because it produced
   mid-session stops with unblocked RFC work still on disk — the
   wrong tradeoff. The replacement is *consolidation*, not gating.
   Before opening a new PR, prefer in this order:
   - **`pr_rescue`** — when an existing Nightly PR has new feedback
     (CI failure, reviewer comments, bot suggestions), finishing it
     beats starting fresh. This is already cascade slot 5; honor it.
   - **Extend the most recently-opened in-flight PR** when the
     current cascade pick is closely related to its scope — same
     RFC, same module, same feature. Check out its branch in a
     worktree, commit the additional change, push. The PR grows
     into one reviewable unit instead of becoming PR N+1.
   - **Bundle adjacent phases of the same RFC** into one PR when
     the phases naturally compose. Phase A + B of a small RFC ships
     as one PR; truly independent phases of a large RFC stay
     separate.
   Only when none of the above applies — the cascade pick is
   genuinely orthogonal to every open PR — open a new branch. The
   goal is review-ergonomic, not PR-count-minimal-at-all-costs:
   bundling unrelated work into one PR is worse than two focused
   PRs. Bias: when uncertain, extend the most recent related PR.
   Past failure (now removed): agent shipped a 6th stacked paperwork
   PR while #54-#58 were still unreviewed because the cascade kept
   finding RFC-checkbox / lint-fallback work; the v0.0.2-and-earlier
   solution was the cap, which then created the new failure of
   ending sessions early. v0.0.3+ instead consolidates without
   capping.

### Human shutdown intervention

The keep-alive must never trap the operator. Three independent
off-ramps stop a running Nightly session at any time. **None of these
are commands the agent runs** — they are human controls (see Rule 10):

- **`nightly conclude`** — graceful drain. The current task finishes,
  the briefing renders, the session ends naturally at the next turn
  boundary. Use this in the morning when you want to inspect the work.
- **`nightly stop`** — hard stop. Writes a `STOP` sentinel; the next
  Stop hook firing allows the model to end its turn cleanly without
  starting new work. Use when you want Nightly off **now** but are
  OK letting the current response print.
- **Ctrl-C / `/quit`** — interrupt. Bypasses the Stop hook entirely
  and kills the session immediately. Always available as the
  emergency stop.

**As of v0.0.3, the only voluntary termination is human
intervention.** All automatic off-ramps were removed:

- `pr_backlog` — `MAX_OPEN_PRS=5` cap, replaced by skill-side
  consolidation (Rule 11).
- `max_turns` — 500-turn safety cap on force-continues, removed
  outright. The turn counter is still incremented for telemetry.
- `stale` — 4-hour `SESSION_ACTIVE` freshness check, removed. A
  marker that survived from earlier today still force-continues.
- `cascade_loop` — repeated-pick guard, removed as a *release*
  condition in v0.0.3; **restored as a reroute in v0.0.11**. When
  the same `github_issue` or `accepted_rfc` pick repeats ≥3
  consecutive turn boundaries, the hook injects the planning-phase
  prompt instead of "Continue on: X". Sources that legitimately
  repeat (`resume_in_flight`, `unblocked_approval`, `pr_rescue`)
  never reroute. The session is not released — the v0.0.3 "only
  human intervention terminates" contract holds. The history file
  is still written; it now drives the detector.

Two structural preconditions remain (these are "nothing to keep
alive" not "voluntarily released"): `no_run` (no active run) and
`inactive` (`SESSION_ACTIVE` marker absent — non-Nightly sessions
are untouched). One host-level override also remains: Claude Code's
8-consecutive-blocks-without-progress cap. Unlike the old misread
`stop_hook_active` yield, this cap is a real Python-opaque limit —
no Stop hook can intercept it. The installer mitigates it by merging
`"env": {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "5000"}` into
`.claude/settings.local.json`, effectively lifting the cap for
overnight sessions while keeping a finite runaway backstop.

**v0.0.10 fix (bug reports #19 / #25 — stop_hook_active misread).**
Earlier versions misread Claude Code's `stop_hook_active: true` stdin
flag as "the host cap is about to override us" and yielded immediately
(logged as `host_cap`), writing a RESPAWN_REQUESTED marker. In
reality, `stop_hook_active: true` is set on *every* Stop event that
follows a hook-forced continuation — it simply marks "we are
continuing because the hook blocked," not "override imminent." Result:
sessions surrendered after exactly one force-continue (~minutes into
an overnight run). The fix: the hook now rides through forced-
continuation chains indefinitely; the only stop conditions are human
disk markers (CONCLUDE, STOP) and the structural preconditions above.
The `host_cap` voluntary-yield branch is gone. While blocking inside
a forced-continuation chain the hook **preemptively** writes/refreshes
the RESPAWN_REQUESTED marker — so if Claude Code's without-progress
cap (or a crash) silently kills the session, the marker is already on
disk. A fresh (non-chain) turn boundary clears the stale marker;
`nightly status` and `nightly session start` surface it prominently
so the operator's "re-invoke `/nightly`" resumes cleanly. A new per-
run `keepalive.blocks` counter records chain length for telemetry.
RFC 010 (planned) is the daemon-driven follow-up: a supervisor that
re-invokes the host on an involuntary kill so the operator never has
to.

**v0.0.11 fix (issue #27 — cascade livelock on PR-covered issues).**
The `github_issue` ranker now skips an issue when (a) any open PR
claims it with a closing keyword (existing guard from issue #10), OR
(b) any open **Nightly-authored** PR (`nightly/*` branch) merely
*mentions* `#N` in its body — a bare mention in an orchestrator-owned
PR means the issue is in flight, even without a closing keyword. Skip
reason: "open Nightly PR references this issue (in-flight)." The
keepalive livelock backstop (restored `cascade_loop` reroute — see
above) fires if a `github_issue` or `accepted_rfc` pick repeats ≥3
consecutive boundaries; the hook then injects the planning-phase
prompt so the agent enters ideation rather than holding.

**v0.0.12 — context-compaction feature.** The keepalive hook now
estimates live context size each turn boundary (`ctx=` in heartbeat
log), persists it to `keepalive.context`, and when the estimate
exceeds the `context.budget_tokens` soft ceiling (default 256K),
prepends a context-diet block to the injected prompt. A compact
session digest is written to `digest.md` every N turns (default 1)
and always before any planning-phase reroute. A new
`SessionStart(compact)` hook re-injects the digest after any
compaction so key state is never lost. See `context:` block in
`.nightly/config.yml` and RFC 011.

### Filing a bug against Nightly itself

When Nightly's own behavior looks wrong (self-concluding, ignoring the
cascade, hook misfires, runaway loops), the operator runs
`nightly bug` (or `/nightly-bug`). This bundles the current run's
`keepalive.log`, plan statuses, briefing, and on-disk markers into a
markdown report under `.nightly/bugs/` and — if `gh` is available —
opens an issue on the Nightly repo. **The agent never invokes
`nightly bug` itself**; it is a debugging tool for the human, and
self-filing would mask whatever the agent was about to do wrong.

If you find yourself about to ask the user something: pick the better
default and ship it. Decision is cheaper than deliberation; deliberation
is cheaper than asking; asking is forbidden.
<!-- nightly:rules:end -->
