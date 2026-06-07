"""Repo-level rules Nightly seeds into AGENTS.md / CLAUDE.md.

The single rule that matters most: **never prompt the user**. Nightly
runs are unattended by design — anything that pauses the agent and
waits for input defeats the whole product. The block below is what
`nightly init` writes (idempotently) into the repo's rules files so
the host agent sees it on every cold load.

Marker-delimited so re-running `nightly init` rewrites just the block,
leaving the rest of the file alone. Removing the markers (or the whole
block) opts out — Nightly never re-adds it once gone, only refreshes
when it's already present.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_RULES_FILES",
    "MARKER_END",
    "MARKER_START",
    "NIGHTLY_RULES_BODY",
    "RulesSeedResult",
    "seed_rules",
]


MARKER_START = "<!-- nightly:rules:start -->"
MARKER_END = "<!-- nightly:rules:end -->"

# Files Nightly seeds by default. AGENTS.md is the cross-tool standard
# (Codex / Cursor / opencode / Antigravity all read it); CLAUDE.md is
# Claude Code's holdout location. We seed both because most users have
# one or the other and we don't know which until they install a host.
DEFAULT_RULES_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")


NIGHTLY_RULES_BODY = """\
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
- `cascade_loop` — repeated-pick guard, removed. The history file
  is still written for post-mortem diagnostics but doesn't release.

Two structural preconditions remain (these are "nothing to keep
alive" not "voluntarily released"): `no_run` (no active run) and
`inactive` (`SESSION_ACTIVE` marker absent — non-Nightly sessions
are untouched). One host-level override also remains and cannot be
fought from Python: `host_cap`, where Claude Code's own
9-consecutive-block safety overrides us regardless.

**Partial mitigation in v0.0.8+ (bug reports #13 / #16).** When the
hook yields to `host_cap`, it now drops a `RESPAWN_REQUESTED` marker
under the current run dir before bowing out. `nightly status` and
`nightly session start` both surface the marker prominently the
next time someone enters the repo; the Claude skill reads
`nightly session start`'s output and, on seeing the marker, skips
its fresh-session prelude (seed parsing, briefing checks) and walks
the cascade immediately. The operator's "re-invoke `/nightly`"
becomes a clean continuation, not a restart. RFC 010 (planned) is
the daemon-driven follow-up: a supervisor that re-invokes the host
itself on `host_cap` so the operator never has to. The marker is
the disk-state half of RFC 010 shipping early.

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
"""


@dataclass(frozen=True)
class RulesSeedResult:
    """One file's outcome from a `seed_rules` invocation."""

    path: Path
    """Absolute path to the rules file."""

    action: str
    """Literal: `created` · `updated` · `unchanged` · `skipped`."""

    @property
    def changed(self) -> bool:
        return self.action in {"created", "updated"}


def _render_block() -> str:
    """Return the marker-delimited rules block (no leading/trailing newline)."""
    return f"{MARKER_START}\n{NIGHTLY_RULES_BODY.rstrip()}\n{MARKER_END}"


def _seed_one(path: Path, *, create_if_absent: bool) -> RulesSeedResult:
    new_block = _render_block()
    if not path.exists():
        if not create_if_absent:
            return RulesSeedResult(path=path, action="skipped")
        path.write_text(new_block + "\n", encoding="utf-8")
        return RulesSeedResult(path=path, action="created")

    current = path.read_text(encoding="utf-8")
    if MARKER_START in current and MARKER_END in current:
        # Block already present — replace it in place. We re-scan markers
        # rather than using regex so weird user edits around the block
        # can't trick the replacement.
        start = current.index(MARKER_START)
        end = current.index(MARKER_END) + len(MARKER_END)
        replaced = current[:start] + new_block + current[end:]
        if replaced == current:
            return RulesSeedResult(path=path, action="unchanged")
        path.write_text(replaced, encoding="utf-8")
        return RulesSeedResult(path=path, action="updated")

    # Marker absent — append the block at the bottom with a blank line
    # separator. Preserves whatever the user already had.
    separator = "" if current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
    path.write_text(current + separator + new_block + "\n", encoding="utf-8")
    return RulesSeedResult(path=path, action="updated")


def seed_rules(
    root: Path,
    *,
    files: tuple[str, ...] = DEFAULT_RULES_FILES,
    create_if_absent: bool = True,
) -> list[RulesSeedResult]:
    """Idempotently seed Nightly's autonomy rules into `files` under `root`.

    For each filename in `files`:

    - **File exists, marker present** → block is replaced in place
      (returns `updated` if content changed, `unchanged` if identical).
    - **File exists, marker absent** → block is appended at the bottom
      with a blank-line separator (returns `updated`).
    - **File absent** → file is created with just the block when
      `create_if_absent=True` (returns `created`), else `skipped`.

    Removing the marker comments from the file opts out — `seed_rules`
    won't re-add them once the user has explicitly cleared the block
    only if they also remove the markers (the marker detection treats
    "no markers" as "append at end" by default; pass
    `create_if_absent=False` to make the absence permanent).
    """
    return [_seed_one(root / filename, create_if_absent=create_if_absent) for filename in files]
