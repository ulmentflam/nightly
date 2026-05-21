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

The `UPDATE_SKILL_MD` lives in this module too because its role is
sibling — both are host-portable "operational" slash commands that
wrap a shell action. See `nightly_core.update` for the implementation
that `/nightly-update` invokes.
"""

from __future__ import annotations

__all__ = ["CONCLUDE_SKILL_MD", "UPDATE_SKILL_MD"]


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
