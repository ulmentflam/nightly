---
status: proposed
sized: false
title: Respawn supervisor — daemon that re-invokes the host on involuntary kill
created: 2026-06-09
author: nightly-seed
source: interactive_seed
---

# RFC 010 — Respawn supervisor

## Status

`proposed` — operator directive in issues #13 / #16 / #19 / #25: "start
a background shell in an interactive session that keeps it alive ALWAYS."
v0.0.10 ships the necessary prerequisite (the Stop-hook `stop_hook_active`
misread is fixed; the hook now rides forced-continuation chains
indefinitely and writes RESPAWN_REQUESTED preemptively). RFC 010 is the
belt-and-braces follow-up: a detached supervisor process that detects an
involuntary kill and re-invokes the host headlessly, so the operator
never has to manually re-enter `/nightly`.

## Context

### Problem history

Four separate bug reports converge on the same failure mode: interactive
Nightly sessions die silently mid-overnight and leave cascade work on
disk.

- **#13 / #16** — `host_cap` misread. Earlier hooks misread Claude Code's
  `stop_hook_active: true` stdin flag as "the 9-consecutive-block cap is
  about to override us" and yielded immediately. In reality that flag
  is set on *every* Stop event following a hook-forced continuation. The
  yield happened after exactly one force-continue — minutes into an
  overnight run.

- **#19 / #25** — operator request. "I want Nightly alive ALWAYS in an
  interactive session." Even after fixing the `stop_hook_active` misread,
  Claude Code's real without-progress cap (8 consecutive blocks; raisable
  via `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`) can still kill a session if the
  agent loops without making forward progress. A crash (OOM, network
  disconnect, host process exit) is another kill path no Stop hook can
  intercept.

v0.0.10 addresses #13 / #16 / #19 in the hook layer and raises the cap
via `settings.local.json`. RFC 010 adds a supervisor layer that catches
the remaining kill paths: genuine without-progress cap hits and crashes
that bypass the Stop hook entirely.

### The RESPAWN_REQUESTED marker (v0.0.10 disk-state half)

During a forced-continuation chain the hook now writes/refreshes
`.nightly/runs/<run-id>/RESPAWN_REQUESTED` preemptively. A fresh
(non-chain) turn boundary clears the stale marker. `nightly status`
and `nightly session start` surface it prominently; the skill reads
the output and skips the fresh-session prelude when seen. This is the
disk-state half of RFC 010 — a human re-invoking `/nightly` gets a
clean resume, but the supervisor is the automated half that fires
that re-invocation automatically.

## Non-goals

- **Replacing the Stop-hook keep-alive.** The hook is still the primary
  keep-alive mechanism. The supervisor is belt-and-braces for the cases
  the hook cannot intercept.
- **Multi-host supervisor abstraction.** v1 targets Claude Code
  (`claude -p`) only. Other hosts follow in future iterations.
- **Supervisor UI / TUI.** The supervisor runs fully detached; operator
  feedback is via `nightly status` and the keepalive log.
- **Infinite respawn budget.** A bounded respawn budget with exponential
  backoff prevents runaway loops on systemic failures (broken config,
  no connectivity, etc.).
- **Integration with RFC 007 model-tier routing.** Supervisor re-invokes
  on the operator's default model; tier selection is a follow-up.

## Proposed direction

`nightly session start` (or a new `nightly supervise` command) spawns a
detached watcher process that polls the run directory at a configurable
interval. When the trigger condition is met, the supervisor re-invokes
the Claude Code CLI headlessly (`claude -p "/nightly" --permission-mode
acceptEdits`) with an exponential backoff and bounded respawn budget.
Off-ramps (CONCLUDE / STOP markers) kill the supervisor cleanly.

## Design sketch

### Trigger condition

All four conditions must hold before the supervisor fires a respawn:

1. `SESSION_ACTIVE` marker present under `.nightly/` (non-Nightly
   sessions must not be supervised).
2. No CONCLUDE or STOP marker under the current run dir.
3. `RESPAWN_REQUESTED` marker present under the current run dir
   (written by the hook when an involuntary kill is suspected).
4. `keepalive.log` heartbeat stale beyond a threshold (default: 90s).
   This distinguishes "session is dead" from "session is idle while
   the hook blocks." The heartbeat is the last-write timestamp of
   `keepalive.log`; the hook appends a line on every firing.

Conditions 3 + 4 together prevent spurious re-invocations: the hook
writes RESPAWN_REQUESTED only during a forced chain, and a stale
heartbeat confirms the chain stopped progressing.

### Supervisor lifecycle

```
nightly session start
  └─ (if supervisor not running) spawn detached: nightly-supervisor --run-id <id>
       │
       ├─ poll every N seconds (default: 30s)
       │    ├─ check trigger condition
       │    ├─ if met → attempt respawn
       │    │    ├─ increment respawn counter
       │    │    ├─ if counter > MAX_RESPAWNS → write STOP, exit
       │    │    ├─ sleep 2^counter seconds (exponential backoff, capped at 300s)
       │    │    └─ exec: claude -p "/nightly" --permission-mode acceptEdits
       │    └─ if not met → continue polling
       │
       └─ exit when CONCLUDE or STOP marker appears
```

`MAX_RESPAWNS` defaults to 5. The backoff sequence is 1s, 2s, 4s, 8s,
16s (then capped at 300s) to avoid hammering the host on persistent
failures.

### Configuration (`.nightly/config.yml` additions)

```yaml
supervisor:
  enabled: true                  # set false to disable entirely
  poll_interval_seconds: 30      # how often to check trigger condition
  heartbeat_stale_seconds: 90    # stale threshold before respawn
  max_respawns: 5                # bounded budget
  respawn_command: "claude -p \"/nightly\" --permission-mode acceptEdits"
```

### Off-ramps

- **CONCLUDE marker** — supervisor detects it on the next poll and exits
  cleanly. The graceful drain is preserved.
- **STOP marker** — same: supervisor exits immediately.
- **Ctrl-C / `/quit`** — kills the supervised session; the supervisor's
  next poll finds a stale heartbeat but no SESSION_ACTIVE (the session
  wrote a shutdown marker), so it exits.
- **`MAX_RESPAWNS` budget exhausted** — supervisor writes a STOP sentinel
  to prevent a runaway loop, then exits. The operator is informed via
  `nightly status`.

## Relationship to v0.0.10

v0.0.10 reduces RFC 010 from "primary keep-alive" to "belt-and-braces":

| Failure mode | v0.0.9 mitigation | v0.0.10 + RFC 010 |
|---|---|---|
| `stop_hook_active` misread | Sessions surrender after 1 force-continue | Fixed in hook — rides chains indefinitely |
| Without-progress cap | `host_cap` yield + manual respawn | Cap raised via `BLOCK_CAP=5000`; supervisor auto-respawns on genuine cap hit |
| Crash / OOM | No mitigation | RESPAWN_REQUESTED written preemptively; supervisor detects stale heartbeat and re-invokes |
| Operator forgets to re-invoke | Manual: operator must type `/nightly` | Supervisor fires automatically |

## Risks

- **Supervisor outlives the repo session.** A detached process can
  survive past the operator's intent. Mitigation: supervisor exits on
  CONCLUDE / STOP; `nightly session stop` writes a STOP sentinel that
  the supervisor also respects; `nightly status` shows supervisor PID so
  the operator can kill it manually.
- **Respawn into a broken state.** If the underlying cause of the kill
  is a broken config, the supervisor respawns into the same failure.
  Mitigation: exponential backoff + `MAX_RESPAWNS` cap; after exhaustion,
  supervisor writes STOP and exits — the operator returns to a stopped
  session rather than a runaway loop.
- **PID file collisions across runs.** Two simultaneous Nightly sessions
  (rare but possible) could each spawn a supervisor. Mitigation: PID file
  lives under `.nightly/runs/<run-id>/supervisor.pid` — one per run, not
  one per repo.
- **Claude Code CLI path not on PATH.** The respawn command assumes
  `claude` is on PATH. Mitigation: `nightly session start` validates the
  path at supervisor-spawn time and logs a warning if absent; `nightly
  doctor` extends to flag this.

## Implementation phases

Two phases; sizing deferred to acceptance.

### Phase A — Supervisor process + trigger condition

- **A1.** `nightly-supervisor` entry point (or `nightly supervise`
  subcommand) in `nightly_core.cli`. Detached via `subprocess.Popen`
  with `start_new_session=True`.
- **A2.** Trigger condition check as a pure function:
  `should_respawn(run_dir: Path, stale_threshold_s: int) -> bool`.
- **A3.** PID file under `runs/<run-id>/supervisor.pid`; `nightly
  session start` checks for a running supervisor before spawning a
  second.
- **A4.** Config schema additions in `nightly_core.config`.
- **A5.** Tests: trigger condition logic, PID file lifecycle, config
  defaults.

### Phase B — Respawn execution + off-ramps + `nightly status` surface

- **B1.** Respawn execution loop with exponential backoff and
  `MAX_RESPAWNS` budget.
- **B2.** Off-ramp detection: CONCLUDE, STOP, and SESSION_ACTIVE
  absence each exit the supervisor cleanly.
- **B3.** `nightly status` gains a "Supervisor" row showing PID,
  respawn count, and last-respawn timestamp.
- **B4.** `nightly doctor` extends with a `claude`-on-PATH check.
- **B5.** Tests: respawn budget exhausted writes STOP; off-ramps
  exit cleanly; status surface reflects supervisor state.

## Sized checklist

*(not yet sized — pending acceptance)*

**Phase A — Supervisor process + trigger condition**
- [ ] A1. `nightly supervise` entry point with detached spawn
- [ ] A2. `should_respawn()` pure function covering all four trigger conditions
- [ ] A3. PID file lifecycle (create on spawn, check before re-spawn, clean on exit)
- [ ] A4. Config schema additions in `nightly_core.config`
- [ ] A5. Unit tests for trigger condition, PID file, config defaults

**Phase B — Respawn execution + off-ramps + status surface**
- [ ] B1. Respawn loop with exponential backoff and `MAX_RESPAWNS` budget
- [ ] B2. Off-ramp detection for CONCLUDE, STOP, SESSION_ACTIVE absence
- [ ] B3. `nightly status` supervisor row (PID, respawn count, last timestamp)
- [ ] B4. `nightly doctor` PATH check for `claude` binary
- [ ] B5. Integration tests for budget exhaustion, off-ramp exits, status surface
