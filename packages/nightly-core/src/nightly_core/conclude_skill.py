"""Shared `/nightly-conclude` / `/nightly-update` / `/nightly-bug` skill content.

These three skills are the same across all five hosts — each just runs
a single `nightly <verb>` shell command and ends the turn. Each host
package imports the relevant constant and writes it at its
host-specific skill path
(`.claude/skills/nightly-conclude/SKILL.md`,
`.cursor/commands/nightly-conclude.md`, etc.).

`/nightly-conclude` and `/nightly-bug` are **human-invoked off-ramps**
— the agent never reaches for them (see rules.py rule 10). They live
here because each is a thin host-portable wrapper around a shell
action, same as `/nightly-update`.

The conclude skill exists because the Stop hook would otherwise
force-continue when the user wants to wind down — running `nightly
conclude` from the chat is fiddly (the agent doesn't naturally pick
up shell commands mid-conversation). A dedicated slash command lets
the user type `/nightly-conclude` and the host invokes it cleanly.

The bug skill exists for the same reason: when the operator sees
Nightly misbehave (self-conclude, ignore the cascade, runaway loop),
they need a one-keystroke way to capture state and file an issue
against the Nightly source repo — see `nightly_core.bug`.
"""

from __future__ import annotations

__all__ = ["BUG_SKILL_MD", "CONCLUDE_SKILL_MD", "UPDATE_SKILL_MD"]


CONCLUDE_SKILL_MD = """\
---
name: nightly-conclude
description: HUMAN-ONLY off-ramp invoked when the operator types `/nightly-conclude` to wind down a running Nightly session. NEVER call this skill or run `nightly conclude` yourself as part of normal Nightly work — the autonomous loop's wrap-up is `nightly ideate` then `nightly brief`, never `nightly conclude`. Self-invoking freezes the cascade short-circuit at `concluded` and ends the session with unblocked work still on disk.
---

# /nightly-conclude  *(human-invoked only)*

This skill **only** runs when the human operator explicitly types
`/nightly-conclude` to wind down a running session. The Nightly agent
itself must never invoke this skill or run `nightly conclude` in any
other context — doing so is a known failure mode (the cascade
short-circuits at `concluded` and unblocked RFC items, parked tasks,
and fresh proposals get stranded on disk until the next session).

If you are the Nightly agent and you reached the end of your work,
your wrap-up is:

1. `nightly ideate` (surface proposals into the briefing) — **not**
   `nightly conclude`.
2. `nightly brief` (render the report).
3. End your turn and let the Stop hook decide. If `SESSION_ACTIVE`
   is still armed, the hook will force-continue you onto more work;
   if the human has placed a CONCLUDE / STOP marker themselves, the
   hook will release.

**Do not** invoke this slash command, run `nightly conclude`, or
write the CONCLUDE marker yourself. Those are operator controls.

## What this skill does (when the human invokes it)

Do the following, in order, and **do not** pick up new cascade work:

1. **Run `nightly conclude`** in the shell. This writes a `CONCLUDE`
   marker under `.nightly/runs/<id>/`. The Stop hook reads that marker
   on its next firing and stops force-continuing the session.
2. **Finish only the current task.** If a task is in flight, complete
   the current step (commit + push if a PR was already opened; otherwise
   write `proposal.md` locally). Do not start anything new from the
   cascade — `CONCLUDE` is the explicit signal that the human is back
   and the autonomous loop is done.
3. **Render the morning briefing** with `nightly brief`. The output is
   `.nightly/runs/<id>/briefing.html` — the human will open it to
   review the run.
4. **End your turn cleanly.** The Stop hook will allow the stop because
   the CONCLUDE marker is present. No need to force the issue with
   Ctrl-C.

## Off-ramps if `nightly-conclude` isn't what you wanted

- For an **immediate hard stop** (don't wait for the current task to
  drain), use `/nightly-stop` instead, or run `nightly stop` in the
  shell. That writes a `STOP` sentinel; the next Stop-hook firing
  allows the model to end its turn without starting new work.
- For an **emergency stop** (kills the session immediately, bypasses
  the hook), press Ctrl-C or use the host's `/quit` command. Always
  available.
- To **file a bug** about Nightly's behavior (e.g. the agent
  self-concluded, a hook misfired, the cascade ignored a real plan),
  use `/nightly-bug` instead. That bundles the run state and opens
  an issue.

## Why this is a separate skill

The /nightly skill arms the Stop-hook keep-alive (`nightly session
start`) at invocation. Without a dedicated `/nightly-conclude` skill,
the only way to disarm cleanly was to type the shell command into the
chat — which the agent treats as conversation rather than execution.
A slash command makes the wind-down deterministic *for the human* —
the agent itself still must never reach for it.
"""


UPDATE_SKILL_MD = """\
---
name: nightly-update
description: Pull the latest Nightly release and refresh this repo's installed skills, hooks, and rules block. Idempotent — safe to re-run anytime.
---

# /nightly-update

You were invoked because the human wants to update Nightly. Do the
following, in order:

1. **Run `nightly update`** in the shell. This:
   - Locates the Nightly source checkout (`~/.local/share/nightly`
     when installed via `install.sh`).
   - Fetches the latest from the configured remote and checks out
     `main` (override with `--version <tag|sha>`).
   - Re-runs `uv sync` to update Python dependencies.
   - Walks the current repo and re-runs `nightly init` for every
     host already installed — refreshing SKILL.md, the Stop-hook
     entry, the `/nightly-conclude` and `/nightly-update` skills,
     and the AGENTS.md / CLAUDE.md rules block.
2. **Report what changed.** The command prints a structured summary:
   - Source commit before / after (so the human sees the version
     bump).
   - Hosts whose skills got refreshed in this repo.
   - Whether the rules block changed.
3. **End your turn.** No new cascade work — update is its own
   atomic operation. The next `/nightly` invocation will pick up
   the refreshed content.

## Flags worth knowing

- `nightly update --version v0.1.0` — pin to a specific tag instead
  of `main`.
- `nightly update --dry-run` — fetch + show the commit delta without
  checking out or syncing.
- `nightly update --no-refresh-repo` — update source only; don't
  re-init this repo's hosts. Useful when running from a fresh shell
  before opening any project.

## When `nightly update` doesn't apply

Inspired by [gsd-build](https://github.com/gsd-build/get-shit-done)'s
re-installable-package pattern, but Nightly is git-based today. If
the source isn't a git checkout (PyPI / pipx / `uv tool install`),
`nightly update` prints the right upgrade command for the install
method instead of trying to git-pull.
"""


BUG_SKILL_MD = """\
---
name: nightly-bug
description: HUMAN-ONLY off-ramp invoked when the operator types `/nightly-bug` after observing that Nightly itself is misbehaving (self-concluding, ignoring the cascade, runaway loops, hook misfires). NEVER call this skill or run `nightly bug` yourself — self-filing masks the very bug the operator needs to triage.
---

# /nightly-bug  *(human-invoked only)*

This skill **only** runs when the human operator explicitly types
`/nightly-bug` after seeing Nightly misbehave. The Nightly agent
itself must never invoke this skill or run `nightly bug` — see
rules.py rule 10 (the agent never reaches for the human off-ramps).

## What this skill does (when the human invokes it)

1. **Run `nightly bug`** in the shell. This:
   - Bundles `keepalive.log`, run markers (CONCLUDE / STOP /
     SESSION_ACTIVE / keepalive.turns), every plan's `status`,
     the last `briefing.md`, `nightly status`, `nightly next`,
     recent `git log`, and the AGENTS.md / CLAUDE.md rules block
     into a single markdown report.
   - Writes the report to `.nightly/bugs/<timestamp>/report.md`.
   - If `gh` is available, opens an issue against the Nightly
     source repo (default `ulmentflam/nightly`) with the report
     as the issue body. Without `gh`, prints the would-be command
     so the operator can run it elsewhere.
2. **Show the operator the result.** Either the issue URL (success)
   or the path to the on-disk report plus the `gh` command they
   can copy. Don't go off and start fixing the bug yourself — the
   point is to capture state for human triage.
3. **End your turn.** Filing a bug is an atomic operation; no
   cascade work follows. The next `/nightly` invocation continues
   as normal.

## Useful flags

- `nightly bug --describe "<one-liner>"` — short free-text summary
  that becomes the report's "Operator summary" section.
- `nightly bug --title "<title>"` — override the auto-generated
  issue title.
- `nightly bug --repo owner/name` — file against a fork or
  internal mirror instead of `ulmentflam/nightly`.
- `nightly bug --no-submit` — write the report to disk only; skip
  the `gh issue create` step entirely (useful when gathering state
  without filing publicly).

## Why this is a separate skill

Bugs in Nightly's autonomous behavior are by definition the kind of
thing the agent itself shouldn't be triaging — its judgment is what's
in question. The skill exists to give the operator a single
deterministic command to capture *exactly* the state Nightly saw,
without trusting the agent's own retelling. A slash command makes the
capture as low-friction as possible the moment the human notices
something's off.
"""
