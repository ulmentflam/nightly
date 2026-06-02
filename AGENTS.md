<!-- nightly:rules:start -->
## Nightly autonomy contract

When this repo's coding agent is invoked **by Nightly** (the autonomous
overnight orchestrator), these rules override any default "ask the user
when unsure" behavior. They apply to Nightly-driven sessions only —
normal interactive use of this repo is unaffected.

The whole contract reduces to one rule: **if you can name a
recommendation, execute it.** Everything below is consequences.

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
6. **Never stop just because the cascade returned `nothing`.** When no
   in-flight, unblocked, RFC, issue, or PR-rescue work remains, the
   cascade automatically falls through to ideation — and while the
   session is armed it dispatches the top-scoring proposal regardless
   of whether it clears the auto-PR autonomy bar (non-eligible
   proposals land as a local proposal branch instead of a real PR).
   You should never see the cascade return `nothing` during an active
   run. If you somehow do, immediately run `nightly ideate` and start
   the highest-scoring proposal — do not deliberate, do not render
   the briefing, do not exit.
7. **Run `nightly verify` before opening any PR.** Nightly auto-detects
   this repo's linters, formatters, and type checkers (ruff, black,
   mypy, pyrefly, eslint, prettier, tsc, gofmt, go vet, cargo fmt,
   clippy, plus `make lint` / `make check` / `make verify` umbrella
   targets) and runs them. A non-zero exit blocks the PR — fix the
   findings (run the tool's auto-fix variant locally first if it has
   one) and re-verify until clean. Do not push code that fails the
   repo's own quality gates; that's exactly the contributor etiquette
   a human reviewer would apply.
8. **Watch CI between tasks with `nightly ci`.** After a Nightly PR is
   opened, CI on the remote runs asynchronously. Between tasks (or
   after committing the current one), run `nightly ci` to see whether
   any open Nightly PR has failed checks. **Do not block waiting on
   CI** — keep picking up new work from the cascade. When CI fails,
   the failure is already a `PRFeedback` kind and the cascade's
   `pr_rescue` step will surface it on the next `nightly next`. The
   `nightly ci` glance just lets you confirm there's nothing in-flight
   that needs your attention before starting a brand-new investigation.
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
11. **PR-backlog backpressure is a host-level concern, not yours.**
   The Stop hook independently watches the count of open Nightly PRs
   and allows the session to end when the operator already has a
   queue waiting for review — unless the cascade has resume-priority
   work (in-flight task, unblocked approval, PR rescue with blocking
   feedback). You don't read this count, don't measure it, and don't
   change behavior because of it. Keep running the cascade and ending
   the turn after `nightly brief`; the hook decides whether to
   release or force-continue. Past failure: agent shipped a 6th
   stacked paperwork PR while PRs #54-#58 were still unreviewed
   because the cascade kept finding RFC-checkbox / lint-fallback work
   and the hook had no signal for operator saturation.

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
- **Open-PR backlog cap** — when the count of open Nightly-authored
  PRs reaches the cap (default 5), the Stop hook treats human review
  as the bottleneck and allows the next turn boundary to end the
  session (`reason_code=pr_backlog` in `keepalive.log`). Resume-
  priority cascade picks (in-flight task, unblocked approval, PR
  rescue with blocking feedback) override the cap and keep the
  session running, because that's *finishing* shipped work rather
  than adding more to the queue. No operator action needed — this
  fires automatically when the queue saturates.

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
