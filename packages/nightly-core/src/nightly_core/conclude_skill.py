"""Shared `/nightly-conclude` skill content.

The conclude skill is the same across all five hosts — it just runs
`nightly conclude` and ends the turn. Each host package imports
`CONCLUDE_SKILL_MD` and writes it at its host-specific skill path
(`.claude/skills/nightly-conclude/SKILL.md`, `.cursor/commands/nightly-conclude.md`,
etc.).

The skill exists because the Stop hook would otherwise force-continue
when the user wants to wind down — running `nightly conclude` from the
chat is fiddly (the agent doesn't naturally pick up shell commands
mid-conversation). A dedicated slash command lets the user type
`/nightly-conclude` and the host invokes it cleanly.
"""

from __future__ import annotations

__all__ = ["CONCLUDE_SKILL_MD"]


CONCLUDE_SKILL_MD = """\
---
name: nightly-conclude
description: Gracefully end the current Nightly run. Run `nightly conclude` in the shell to drain the current task, render the morning briefing, and let the Stop hook stop force-continuing the session.
---

# /nightly-conclude

You were invoked because the human wants Nightly to wind down. Do the
following, in order, and **do not** pick up new cascade work:

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

## Why this is a separate skill

The /nightly skill arms the Stop-hook keep-alive (`nightly session
start`) at invocation. Without a dedicated `/nightly-conclude` skill,
the only way to disarm cleanly was to type the shell command into the
chat — which the agent treats as conversation rather than execution.
A slash command makes the wind-down deterministic.
"""
