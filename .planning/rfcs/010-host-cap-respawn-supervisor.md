---
status: implemented
phase_a: implemented
phase_b: implemented
phase_c: implemented
sized: true
title: Host-cap respawn supervisor — auto-resume on involuntary stops with redacted telemetry
created: 2026-06-06
sized_on: 2026-07-29
accepted_on: 2026-07-29
implemented_on: 2026-07-29
author: nightly-seed
source: interactive_seed
estimated_effort: ~14h across 3 phases
supersedes: 010-respawn-supervisor.md
status_note: |
  Three deviations from the draft, each recorded at its checklist item:
  the FSEvents watcher became a polling loop (B4 — the v0.0.10 preemptive
  marker made file-creation the wrong signal), the respawn launcher grew
  from one strategy to three (B5 — a detached daemon has no TTY), and
  bug-report redaction became unconditional rather than gated behind
  `--share` (A7 — `nightly bug` files a public issue by default).
---

# RFC 010 — Host-cap respawn supervisor

## Status

`implemented` — all three phases shipped 2026-07-29. Accepted and sized
the same day.

**Absorbed a duplicate.** A second, leaner RFC 010
(`010-respawn-supervisor.md`, `proposed`, 2026-06-09) was drafted
during the v0.0.10 keepalive fix by an agent that did not see this
file — it was authored 2026-06-06 but never committed, so it was
invisible to `git`-based checks. The two agreed on the shape
(detached watcher, `RESPAWN_REQUESTED` trigger, bounded respawn
budget with backoff, CONCLUDE/STOP off-ramps) and this file is the
superset, carrying the telemetry and redaction halves the lean draft
dropped. This file is canonical; the lean one is deleted. One idea
was folded in from it and is strictly better than what was here:
the **four-condition trigger** below, whose heartbeat-staleness
check distinguishes "the session is dead" from "the session is
alive and the hook is blocking" — the original marker-presence-only
trigger could not tell those apart and would respawn into a live
session. See Resolved #5a.

Prior status was `draft` — v0.0.8 shipped the disk-state half of this RFC (the
`RESPAWN_REQUESTED` marker written when `compute_stop_hook_decision`
yields to `host_cap`, surfaced at `nightly session start`, cleared
on re-arm). The marker turns a manual `/nightly` re-invocation into
a clean continuation, but the operator still has to walk to the
computer and type the slash command. RFC 010 closes that loop with
a **supervisor daemon** that watches for the marker and re-invokes
the host on the operator's behalf. The same RFC also defines the
**telemetry & redaction contract** so we can finally answer the
question the bug reports kept raising: *why is the session stopping?*

The v0.0.8 marker is necessary but not sufficient — an unattended
overnight run that hits `host_cap` at 03:00 still loses everything
until the operator wakes up. RFC 010 makes the overnight contract
honest.

## Context

Three problems are entangled and ship together because they share
the same data path (`.nightly/runs/<id>/telemetry/`):

**1. Involuntary stops still strand the run.** Bug reports #13/#16
both ended after a single `force_continue` followed by `host_cap`
~17 minutes later. v0.0.8's marker lets the operator resume cleanly
*if they re-invoke `/nightly` manually*. For attended sessions
that's fine; for the overnight contract that's a regression — the
whole point of Nightly is monotonic forward progress while the
operator sleeps. A 03:00 `host_cap` becomes seven hours of stranded
cascade work.

**2. We can't tell *why* hooks fired.** `keepalive.log` records
*decisions* (`force_continue`, `host_cap`, `inactive`, `stop`,
`conclude`) but not *causes*. From the log alone we cannot tell:

- How many consecutive blocks led up to the `host_cap` yield (Claude
  Code's threshold is 9 — were we at 9, or did the host see
  something earlier that flipped `stop_hook_active`?).
- How many tool-denial dialogs the session worked through between
  force-continues.
- How many sub-agent spawns happened in the window.
- Whether the cascade was making progress (different picks per
  walk) or stuck (same pick repeated).
- Wall-clock between force-continue and the next turn boundary —
  was the model thinking, dispatching, or genuinely idle?

Without these signals the bug reports tell us "host_cap fired" and
nothing more. Operators can't decide whether to lengthen the
session, shorten the cascade, or accept that some sessions just
hit the host's cap.

**3. State we'd want to share is operator-private.** When the
operator runs `nightly bug` against #13/#16, the bundled report
includes file paths under `~/Workspace/playground/nightly/`,
working-tree absolute paths in worktree config, hostnames in the
git remote URL, branch names that may reveal feature plans, plan
titles ("auth-rewrite-for-compliance-q3"), and uncertainty.md
content that may name internal systems. We push that into a
public GitHub issue today. The current `bug.py` does some basic
substitution but it's ad-hoc and easy to miss the next path-shaped
field someone adds to the run state.

## Non-goals

- **Cross-host supervisor.** v1 ships the Claude Code path. The
  Codex / Cursor / Antigravity / opencode supervisors are
  follow-ups in v2+ once the protocol shape is proven on Claude
  (different hosts have different consecutive-block semantics —
  Cursor's `stop` hook fires differently, opencode has no hook
  surface at all). The supervisor design must not bake in
  Claude-only assumptions, but v1 only validates against Claude.
- **Modifying Claude Code's hook protocol upstream.** The 9-block
  override is a documented host-side safety. RFC 010 routes around
  it by re-invoking, not by trying to suppress it.
- **Token / spend telemetry.** Stop-reason counters track *behavior*
  (how the session ended), not *cost*. Cost telemetry is a
  separate concern with its own privacy surface (model name leaks,
  pricing leaks) and ships in a later RFC.
- **Aggregating telemetry across machines.** Everything lives under
  `.nightly/runs/<id>/telemetry/` on the operator's local disk.
  No phone-home, no shared dashboard, no upstream collector. Bug
  reports remain the only path off the machine, and they're
  redacted (see Resolved #7-#10).
- **Auto-restart on any non-`host_cap` exit.** The supervisor only
  re-invokes when `RESPAWN_REQUESTED` is present. `CONCLUDE` and
  `STOP` markers are honored — those are the operator-explicit
  off-ramps the v0.0.3 contract preserves.
- **Replacing `nightly bug`.** The supervisor's redaction pipeline
  feeds `nightly bug`; it doesn't replace the verb.
- **Headless / `nightly run` integration.** Headless runs don't
  use the host's hook surface (they drive the cascade directly),
  so they can't hit `host_cap`. The supervisor watches only
  interactive `/nightly` runs.

## Proposed direction

Three approaches; **Approach B** ships as v1.

---

### A — Bash watcher launched by `nightly session start`

`nightly session start` forks a watcher child process. The child
polls `RESPAWN_REQUESTED` every N seconds; on match, it spawns
`claude --slash-command nightly`. The watcher exits when the parent
session exits.

**Pros:**
- Zero new infrastructure. No daemon to install, no system
  integration, no plist files.
- Lifecycle is unambiguous — watcher belongs to its session,
  dies with it.

**Cons:**
- If the parent shell dies (laptop closed, ssh disconnect), the
  watcher dies with it and the operator loses the supervisor
  exactly when they need it (overnight run, operator asleep).
- Polling-only — no inotify / FSEvents integration, so latency
  is bounded by the poll interval. Either too fast (CPU cost,
  battery drain on laptop) or too slow (host_cap → daemon wakes
  → re-invoke window stretches to minutes).
- Hard to debug — watcher state is in process memory; nothing on
  disk for `nightly bug` to bundle.

---

### B — `nightly-supervisor` daemon installed via launchd / systemd

A long-lived daemon process registered with the OS service
manager (launchd on macOS, systemd --user on Linux). The daemon
watches `~/.cache/nightly/runs/` (and any repo's `.nightly/runs/`
the operator has touched recently — discovered via a tiny
`registry.json` the CLI maintains) for `RESPAWN_REQUESTED` markers.
On detection: spawn the host, log the respawn, increment a
counter.

**Pros:**
- Survives shell disconnect. The whole point of the overnight
  contract is the laptop running alone; the supervisor must too.
- OS-native file watching (FSEvents / inotify) via `watchdog` —
  sub-second latency, no polling cost.
- State is on disk (`~/.cache/nightly/supervisor/state.json`) so
  `nightly bug` can include it; `nightly supervisor status`
  gives the operator a live view.
- One process per machine, one launchd / systemd unit — clean to
  install, clean to uninstall.

**Cons:**
- Net-new install step. The first-time operator has to run
  `nightly supervisor install` (which writes the launchd plist /
  systemd unit). Opt-in by design; we don't mass-deploy daemons.
- Daemon survival is a new operational responsibility. If the
  daemon crashes, the operator may not notice until the next
  `host_cap` event leaves a session stranded.
- Two-machine drift (work laptop + home laptop) — each has its
  own supervisor; bug reports might come from either.

---

### C — In-host watcher via a long-running specialist sub-agent

When `nightly session start` runs, it spawns a `supervisor` role
specialist (analogous to `implementer` / `tester`) that the host
keeps alive as a sub-agent. The sub-agent reads markers via
disk polling; on detection, it issues a `slash_command` action
through the host's API.

**Pros:**
- Lives inside the host's session model — no OS integration.
- Cross-host generalization is straightforward (every host has
  some sub-agent dispatch concept).

**Cons:**
- A sub-agent is bounded by the same `host_cap` as the parent.
  When the host enforces consecutive-block stops, the supervisor
  sub-agent dies with the rest of the session. We'd need a
  supervisor for the supervisor — back to approach B.
- Token cost. A sub-agent that polls a file once a minute for
  eight hours is wasteful even with prompt caching.
- Defeats the v0.0.8 marker's point: the marker exists because
  in-host approaches can't fight `host_cap`.

---

## Resolved technical decisions

**1. Approach B ships as v1.** Approach A was rejected because the
overnight contract requires daemon survival across shell
disconnect — a watcher tied to the interactive shell is a
watcher that dies exactly when the operator most needs it.
Approach C was rejected because the in-host failure mode (`host_cap`
killing the supervisor along with everything else) is the same
failure mode the supervisor is meant to address. Approach B's
launchd/systemd dependency is the only choice that survives the
threat model.

**2. Daemon binary: `nightly-supervisor`, also reachable as
`nightly supervisor`.** Lives in
`packages/nightly-core/src/nightly_core/supervisor/__init__.py`
with an entrypoint at `nightly_core.supervisor.main:main` exposed
via `pyproject.toml`'s `[project.scripts]` table. The `nightly
supervisor <verb>` subcommand group on the existing CLI proxies
to the same module so operators don't need to learn two binary
names.

**3. Verbs:**

```
nightly supervisor install      # write launchd plist / systemd unit
nightly supervisor uninstall    # remove the plist / unit
nightly supervisor start        # one-shot foreground run (for debugging)
nightly supervisor status       # live view of registry + last events
nightly supervisor logs [-n N]  # tail the daemon log
```

Install is opt-in. First-time setup prints a one-screen explanation
of what the daemon does, where its plist / unit lives, how to
uninstall, and what state it writes. The operator types `y` to
confirm. The CLI never auto-installs the supervisor — explicit
opt-in is the cost of background-process trust.

**4. Watch surface: a registry maintained by `nightly start`.** Every
`nightly start` appends the absolute repo path to
`~/.cache/nightly/supervisor/registry.json`:

```json
{
  "watched_repos": [
    {"path": "/Users/x/work/repo-a", "last_seen": "2026-06-06T20:33:32Z"},
    {"path": "/Users/x/work/repo-b", "last_seen": "2026-06-06T18:12:04Z"}
  ]
}
```

Entries older than 30 days are pruned at supervisor startup. The
daemon watches each path's `.nightly/runs/CURRENT` → resolved-run-id
→ `<run-id>/RESPAWN_REQUESTED` via FSEvents/inotify. New repos
added to the registry mid-run are picked up on the daemon's next
registry-reread (every 60s).

**5a. Trigger condition — four conditions, all required.** (Folded in
from the absorbed lean draft.) Marker presence alone is not a
sufficient trigger. Since v0.0.10 the hook writes `RESPAWN_REQUESTED`
*preemptively* on every forced-continuation block, so the marker is
present during perfectly healthy sessions — the whole point is that
it is already on disk when the kill lands. A supervisor that fires on
presence alone would respawn into a live session on its first poll.
All four must hold:

1. `SESSION_ACTIVE` marker present (never supervise a non-Nightly
   session).
2. No `CONCLUDE` and no `STOP` marker under the run dir.
3. `RESPAWN_REQUESTED` present under the run dir.
4. `keepalive.log` heartbeat stale beyond `heartbeat_stale_seconds`
   (default 90s), measured as the file's mtime. The hook appends a
   line on every firing, so a live blocking chain keeps it fresh.

Condition 4 is what carries the "is it actually dead" signal;
conditions 1–3 are the "should it be alive" signal. `should_respawn()`
is a pure function over a run directory so the whole trigger is unit
-testable without a daemon.

**5. Respawn action.** On detecting a firing trigger:

- Resolve the host from `.nightly/config.yml`'s `host:` key (or
  the default `claude` if unset).
- Spawn `claude` (or codex / cursor / etc.) in the registered repo
  via a host-specific launcher that types `/nightly` as the first
  input. v1 supports Claude Code only; the launcher lives at
  `packages/nightly-host-claude/src/nightly_host_claude/respawn.py`
  and uses `osascript` on macOS to open a new Terminal/iTerm tab
  with the command, or a pre-existing tmux session if one is
  named in config.
- Increment `supervisor.respawn_count` for the run.
- Log the event to `~/.cache/nightly/supervisor/events.jsonl` and
  to the run's `telemetry/supervisor.jsonl`.

The supervisor does **not** suppress the original `host_cap` —
that already happened in-host. It only spawns a *new* session
that picks up where the previous left off, courtesy of v0.0.8's
marker.

**6. Loop guard: max 5 respawns per run, exponential backoff.**
A pathological session that hits `host_cap` immediately on every
restart would spawn infinitely without a guard. Resolved policy:

- First respawn: immediate.
- Second through fifth respawns: backoff of 60s, 180s, 540s, 1620s
  (3× multiplier).
- Sixth attempt: abort. Write a `SUPERVISOR_ABORTED` marker
  alongside `RESPAWN_REQUESTED` so the morning briefing surfaces
  the cap clearly. Operator-only recovery from there — re-invoke
  manually.

Per-run counters reset when a new run starts (`nightly start`
clears them). The five-respawn ceiling is configurable via
`supervisor.max_respawns` in `~/.cache/nightly/supervisor/config.yml`.

**7. Telemetry shape — one structured JSONL per signal class
under `.nightly/runs/<id>/telemetry/`.** Today's `keepalive.log`
is human-readable and stays — but it's append-only text, not
queryable. Telemetry is the queryable layer:

```
.nightly/runs/<id>/telemetry/
├── stop_reasons.jsonl     # one event per Stop-hook decision
├── tool_denials.jsonl     # one event per refused tool prompt
├── dispatch_events.jsonl  # specialist spawn/finish events
├── cascade_walks.jsonl    # one event per `nightly next` call
├── supervisor.jsonl       # respawn / abort / backoff events
└── summary.json           # rolling counters, written on each event
```

Every line is a JSON object with at minimum:

```json
{
  "ts": "2026-06-06T20:33:32Z",
  "kind": "<one of: stop_decision, tool_denial, dispatch, cascade, respawn>",
  "session_id": "<host-session-id-redacted-to-hash-prefix>",
  "run_id": "<run-id>",
  "details": { ...kind-specific fields, redacted per Resolved #8-#10... }
}
```

`summary.json` rolls all event counts into a single read-once shape
that `nightly status` and the morning briefing render. Example:

```json
{
  "stop_reasons": {
    "force_continue": 47, "host_cap": 1, "inactive": 0,
    "stop": 0, "conclude": 0, "no_run": 0
  },
  "consecutive_blocks_before_host_cap": 9,
  "tool_denials_total": 3,
  "dispatch_events": {"spawned": 12, "finished_ok": 11, "finished_err": 1},
  "cascade_walks": 47,
  "cascade_pick_distribution": {
    "resume_in_flight": 18, "pr_rescue": 5, "accepted_rfc": 22,
    "github_issue": 0, "ideate": 2, "ideate_fallback": 0, "nothing": 0
  },
  "respawns": {"count": 1, "last_at": "2026-06-06T20:33:32Z"}
}
```

The summary is the load-bearing artifact for *answering the bug
reports' question*: was the session stuck (cascade_pick_distribution
heavily skewed to one source), starved (low cascade_walks /
high stop count), or genuinely productive (balanced distribution,
high dispatch_events.finished_ok)? Without it the operator's only
recourse is to eyeball `keepalive.log` and guess.

**8. Redaction is the contract for what leaves the operator's
machine.** Telemetry on disk under `.nightly/runs/<id>/telemetry/`
contains operator-private context — paths, branch names, plan
titles, hostnames — because that context is necessary for the
operator's own post-mortems. The redaction contract applies to:

- `nightly bug` report bodies (the GitHub-issue payload).
- The morning briefing's "Telemetry" section when rendered with
  `--share` (a new flag; default behavior remains unredacted).
- Any `nightly supervisor status --share` output the operator
  pastes externally.

Redaction is **always applied** for these surfaces and **never
applied** for the on-disk files themselves. The on-disk files
are the operator's own audit trail.

**9. The redaction primitives.** A `nightly_core.redaction` module
ships these passes, applied in order:

- **Path tokenization.** Absolute paths under `$HOME` become
  `~/<sha8>/...`. Absolute paths outside `$HOME` (system paths
  like `/usr/local/...`) keep their leading component
  (`/usr/local/<sha8>/...`). The hash is per-path, content-
  addressed by the *path*, so the same path always hashes the
  same way within a report — preserves "this happened twice in
  the same file" signal without leaking what the file is.
- **Branch / plan slug masking.** Branch names matching the
  Nightly prefix (`nightly/<slug>-<timestamp>`) keep `nightly/`,
  hash the slug to `<sha8>`, drop the timestamp. Plan slugs in
  `tasks/<n>-<slug>/` get the same treatment.
- **Git remote URL stripping.** `git remote -v` output becomes
  `<git-remote-redacted>`. Same for any `git@` / `https://` URL
  patterns elsewhere.
- **Hostname masking.** `socket.gethostname()` and any matching
  hostname in payload text becomes `<host>`. The operator's
  username (`$USER`) becomes `<user>`.
- **Free-text scrub.** Uncertainty.md / plan.md / briefing.md
  content fed into reports gets a regex pass for:
  - Email addresses → `<email>`
  - IP addresses (v4/v6) → `<ip>`
  - Long alphanumeric strings (≥ 20 chars, mixed-case w/
    digits) that look like tokens / API keys → `<token>`
  - URLs containing `.<TLD>/<path>` → keep scheme + TLD only:
    `https://<host>.com/<path-redacted>`
- **Project-specifics catalog.** A
  `.nightly/redaction.yml` (optional) lets the operator declare
  project-specific terms to scrub: company names, internal
  service names, codenames. Each entry becomes a literal
  replacement (`"acmecorp" → "<org>"`).

Each pass writes its substitution map to a side-channel file
(`<report>.redaction-map.json`) that **stays on the operator's
machine** and is not included in the share payload. Lets the
operator inspect "what got redacted" if a bug-report submission
later needs follow-up clarification.

**10. The LLM-pass redaction backstop.** Pattern matching catches
the *known* shapes; project-specific shapes the operator didn't
think to catalog will leak. The redaction module includes an
**LLM pass** that runs *after* the pattern passes on
`nightly bug --share` reports:

> *"You are scrubbing a debug report for public posting. Read
> the redacted body below and identify any remaining strings
> that look like (a) absolute filesystem paths, (b) project
> codenames, (c) personal names, (d) internal-system identifiers,
> (e) anything else that would let a reader infer the operator's
> employer, residence, or workflow. For each finding, return
> `{"original": "<text>", "redacted": "<placeholder>"}`. The
> redaction must remain useful for debugging — preserve the
> *category* of the redacted thing. Return JSON only."*

The LLM pass runs through the host's headless CLI (same
mechanism as RFC 009's `SynthesisProposer`). Cost is bounded by
report size (~5k tokens worst case, < $0.01 on Sonnet). Failures
degrade silently to the pattern passes — the report still ships,
just without the LLM backstop.

Operators opt out via `redaction.llm_backstop: false` in
`~/.config/nightly/redaction.yml`. Default is **on** for
`--share` paths; **off** for on-disk telemetry.

**11. The supervisor's own logs are subject to redaction.**
`~/.cache/nightly/supervisor/events.jsonl` contains absolute
repo paths from the registry. When the operator runs
`nightly bug --include-supervisor`, those entries pass through
the same redaction pipeline as run-local telemetry.

**12. Config schema additions:**

```yaml
# .nightly/config.yml (per-repo)
supervisor:
  enabled:        true            # respect RESPAWN_REQUESTED for this repo
  max_respawns:   5               # per-run cap
  backoff_seconds: [0, 60, 180, 540, 1620]
  host:           claude          # which host the daemon respawns
telemetry:
  enabled:        true            # write the structured JSONL files
  retention_days: 30              # prune older runs' telemetry on startup
redaction:
  llm_backstop:   true            # run the LLM pass on `--share` reports
```

```yaml
# .nightly/redaction.yml (per-repo, optional — operator-authored)
project_specifics:
  - {pattern: "acmecorp", replacement: "<org>"}
  - {pattern: "payment-router", replacement: "<service>"}
```

```yaml
# ~/.config/nightly/redaction.yml (per-user, optional)
project_specifics:
  - {pattern: "evan@personal-email.com", replacement: "<email>"}
```

`load_supervisor_config`, `load_telemetry_config`,
`load_redaction_config` helpers in `nightly_core.config` follow
the existing `frozen dataclass + load` shape.

**13. Briefing integration.** The morning briefing gains a
"Session telemetry" section that renders `summary.json`. When
`host_cap` fired during the session, the section leads with a
callout:

> ⚠ This session hit `host_cap` N times. The supervisor
> respawned the host M times before the cap policy aborted.
> Stop-reason breakdown: ...

When `host_cap` didn't fire, the section is a quiet one-line
summary. The structural goal is: the operator opening the
briefing in the morning can see at a glance whether the night
was smooth or fraught.

**14. Bug-report integration.** `nightly bug` accepts a new
`--include-telemetry` flag (default **off**); when set, the
last 24h of telemetry events for the run get appended to the
report (post-redaction). The flag is off by default because
telemetry payloads are large; operators opt in when the bug
report is *about* telemetry (e.g. "host_cap fired four times in
one run"). `--share` (implied by `nightly bug` submitting to
GitHub) always runs the redaction pipeline.

## Risks

- **Daemon survival without operator awareness.** A daemon that
  dies silently means the next `host_cap` event strands the run
  with no recourse. Mitigation: `nightly supervisor status`
  shows daemon liveness; the morning briefing surfaces "supervisor
  not running" prominently when the registry shows runs but no
  recent supervisor events; the launchd plist sets `KeepAlive`
  so launchd auto-restarts the daemon on crash.

- **launchd plist drift across macOS versions.** Apple's plist
  schema changes (rarely, but it has). Mitigation: the installer
  writes the plist using `pyobjc`'s `NSDictionary.writeToFile_`
  to ensure compatibility; tests run on the lowest supported
  macOS version in CI.

- **Redaction false-negatives.** Pattern passes miss
  project-specific strings; the LLM backstop catches some but
  not all. Mitigation: side-channel `.redaction-map.json` lets
  the operator inspect *what was removed* before submission;
  `nightly bug --no-submit` writes the report to disk only so
  the operator can read the redacted body before deciding to
  file. The bug-report template explicitly asks: "Review the
  body below for anything you don't want public; cancel
  submission if you spot a leak."

- **Telemetry file growth.** A long overnight run could produce
  thousands of JSONL events. Mitigation:
  `telemetry.retention_days` prunes old runs on supervisor
  startup; `summary.json` is the bounded-size readable view; the
  raw JSONL is for forensics.

- **Respawn cycles burning model budget.** Five respawns per
  run × overnight hours = potentially many fresh sessions, each
  paying the cold-start cost. Mitigation: the backoff schedule
  (60s, 180s, 540s, 1620s) means a session that's clearly
  stuck escalates *slowly*; the operator's morning briefing
  surfaces `respawns.count` so a runaway session is visible.

- **LLM-redaction false-positives.** The backstop pass might
  redact legitimate debugging signal ("the `payment-router`
  service is part of the codebase being reviewed, not a project
  codename"). Mitigation: the `.redaction-map.json` lets the
  operator review what got redacted; `redaction.llm_backstop:
  false` opts out per-repo; the prompt explicitly prefers
  category-preserving redaction over deletion.

- **Cross-machine drift.** Operator with two machines (laptop +
  desktop) ends up with two supervisor daemons, two registries,
  two telemetry trees. Mitigation: out of scope for v1 — explicit
  non-goal. The on-disk shape is per-machine by design, and bug
  reports note which machine they came from in their footer.

- **Supervisor races the marker.** A session that writes
  `RESPAWN_REQUESTED` and then ends so quickly that the daemon
  hasn't observed the marker yet would still respawn (FSEvents
  fires once the file appears, even after the writer exits).
  But the **reverse** race is real: the daemon could observe the
  marker *during* the writer's `_write_respawn_marker` call when
  the file is empty. Mitigation: `_write_respawn_marker` writes
  to `RESPAWN_REQUESTED.tmp` then renames, so the daemon never
  sees a partial file.

## Implementation phases

Three phases, ~14h total. Phases A and B can ship independently —
the telemetry + redaction phase delivers value on its own (better
bug reports, better briefings) even before the supervisor lands.

### Phase A — Telemetry + redaction (~6h)

The foundational phase. Lands the structured JSONL writers, the
`summary.json` roll-up, and the redaction pipeline. Even without
the supervisor, this phase makes bug reports privacy-clean and
makes the briefing more informative about what the session did.

- **A1.** `nightly_core.telemetry` module — JSONL writers for the
  five event classes (stop_reasons, tool_denials,
  dispatch_events, cascade_walks, supervisor). Each writer takes
  a `run.path` and an event dict, appends to the matching
  `<run>/telemetry/<class>.jsonl`. Idempotent file creation;
  best-effort (OSError suppressed).
- **A2.** `nightly_core.telemetry.summary` builder — reads the
  five JSONL files for a run and emits `summary.json` per the
  shape in Resolved #7. Recomputed after every event write
  (cheap; runs are bounded).
- **A3.** Wire `keepalive_hook.compute_stop_hook_decision` to
  emit `stop_reasons` events alongside the existing
  `log_heartbeat` text log. Same for tool-denial paths (currently
  un-instrumented), dispatch start/finish in `dispatch.py`, and
  `cascade.next_task` in `cascade.py`.
- **A4.** `nightly_core.redaction` module — pattern passes (path
  tokenization, branch/plan slug masking, git-remote stripping,
  hostname/username masking, free-text scrub) per Resolved #9.
- **A5.** `nightly_core.redaction.llm_backstop` — host-CLI spawn
  via the same `run_headless` surface RFC 009's
  `SynthesisProposer` uses. Off by default for on-disk telemetry,
  on by default for `--share` paths.
- **A6.** `load_telemetry_config` + `load_redaction_config`
  helpers in `nightly_core.config`. New default-config block in
  `cli.py::_DEFAULT_CONFIG_YML` and `doctor.py::_DEFAULT_CONFIG_YML`.
- **A7.** `nightly bug` integration: `--include-telemetry`
  appends the last 24h of events post-redaction; `--share`
  (already implied by submission) runs the full redaction
  pipeline including LLM backstop when configured.
- **A8.** Briefing's "Session telemetry" section reads
  `summary.json` and renders the host_cap callout when
  applicable.
- **A9.** Unit tests covering: each redaction primitive's
  substitution shape; round-trip with the
  `.redaction-map.json`; JSONL writers handle disk-full
  gracefully; summary roll-up matches event counts;
  briefing section renders for both host_cap-fired and
  smooth sessions.

**Merge gate for Phase A:** telemetry writers land; redaction
pipeline lands; existing `keepalive.log` text log unchanged; bug
report bodies through `nightly bug --include-telemetry` show no
absolute paths under `$HOME` and no branch slugs containing
operator-private content.

### Phase B — Supervisor daemon (~6h)

The active half. Adds the `nightly-supervisor` binary, the
launchd / systemd installer, the FSEvents/inotify watcher, and
the respawn launcher.

- **B1.** `nightly_core.supervisor` package skeleton —
  `daemon.py` (main loop), `registry.py` (watched-repos
  catalog), `respawn.py` (host-launcher dispatch).
- **B2.** Daemon entrypoint in `pyproject.toml`'s
  `[project.scripts]`: `nightly-supervisor =
  nightly_core.supervisor.daemon:main`.
- **B3.** `watchdog` library dependency added (FSEvents on
  macOS, inotify on Linux). New dependency declared in
  `nightly-core/pyproject.toml`.
- **B4.** Watcher logic: subscribe to each registered repo's
  `.nightly/runs/CURRENT` and the resolved
  `<run-id>/RESPAWN_REQUESTED` path. Debounce duplicate fires
  (single 500ms window).
- **B5.** Respawn launcher (`nightly-host-claude/respawn.py`):
  macOS `osascript` opens an iTerm tab with the `/nightly`
  command; Linux falls back to spawning Claude Code via
  `gtk-launch` / direct binary path (configurable). v1
  supports Claude only.
- **B6.** Backoff guard: per-run respawn counter +
  `SUPERVISOR_ABORTED` marker. Counter resets on new run start.
- **B7.** `nightly supervisor install` writes the launchd plist
  (`~/Library/LaunchAgents/com.nightly.supervisor.plist`) or
  systemd unit (`~/.config/systemd/user/nightly-supervisor.service`).
  `nightly supervisor uninstall` removes them.
- **B8.** `nightly supervisor status` reads
  `~/.cache/nightly/supervisor/state.json` and prints
  registry + last events + daemon liveness.
- **B9.** `nightly start` appends repo path to registry;
  `nightly conclude` decrements `last_seen` so a stale registry
  doesn't accumulate.
- **B10.** Integration tests via a fake watched-repo
  fixture; mocked respawn launcher; verify backoff schedule;
  verify abort after fifth respawn.

**Merge gate for Phase B:** Phase A merged; daemon installs on
macOS and Linux CI; supervisor respawns a fake host in tests;
abort marker fires at fifth respawn; bug report includes
`supervisor.jsonl` events via `--include-telemetry`.

### Phase C — README + documentation + cross-host stubs (~2h)

- **C1.** README "Supervisor" section explaining what the daemon
  does, the opt-in install flow, the disk footprint, and the
  uninstall procedure. Includes a one-paragraph explanation of
  the telemetry/redaction contract for the operator's privacy
  understanding.
- **C2.** New `Supervisor` row in the host-comparison matrix:
  Claude (v1: full), Codex (v2: planned), Cursor (v2: planned),
  Antigravity (v2: planned), opencode (v3: experimental — no hook
  surface, must use polling fallback).
- **C3.** CLAUDE.md / AGENTS.md rule about the supervisor: the
  agent never installs / uninstalls the supervisor; that's an
  operator-only verb like `nightly bug`, `nightly conclude`.
- **C4.** `nightly doctor` extends to flag missing supervisor
  install when `RESPAWN_REQUESTED` markers have been observed
  recently — gentle nudge, not an error.
- **C5.** Per-host respawn-launcher stubs in
  `nightly-host-{codex,cursor,antigravity,opencode}/respawn.py`
  that raise `NotImplementedError("v2 — see RFC 010")`. Lets the
  daemon dispatch by host id without crashing when the operator
  is on a non-Claude host; v2 fills these in.

**Merge gate for Phase C:** Phases A + B merged; README has the
supervisor section; doctor surfaces the missing-install nudge;
cross-host stubs land with TODO markers for v2.

## Sized checklist

**Phase A — Telemetry + redaction**
- [x] A1. `nightly_core.telemetry` module with five JSONL writers
- [x] A2. `summary.json` roll-up builder
- [x] A3. Instrumentation wiring for stop_reasons, dispatch_events, cascade_walks.
      Carried on `StopHookDecision.telemetry` and written by `log_telemetry`
      at the CLI call site, mirroring how `log_heartbeat` already works —
      the decision function stays pure. `record_tool_denial` ships and is
      tested, but **is not yet wired to a caller**: no host currently
      surfaces a denial event to Nightly, so there is nothing to hook. The
      writer exists so the class is in the schema from day one rather than
      being retrofitted; `tool_denials_total` reads 0 until a host exposes it.
- [x] A4. `nightly_core.redaction` pattern passes (path, branch, remote, hostname, free-text)
- [x] A5. `nightly_core.redaction.llm_backstop` LLM pass. Spawns `claude -p`
      directly with an injectable `runner` rather than going through
      `run_headless` as drafted — `run_headless` is an async per-host
      integration method, and redaction needs a synchronous call from
      inside report building. Degrades silently on any failure.
- [x] A6. `load_telemetry_config` + `load_redaction_config` helpers; default-config blocks.
      Added a shared `_load_block` / `_coerce_bool` pair to `config.py`; the
      older loaders still inline the same sequence and were left alone.
- [x] A7. `nightly bug --include-telemetry` flag. **Redaction is now
      unconditional on the report body**, not gated behind a `--share` flag
      as drafted — `nightly bug` submits to a public issue by default, so
      opt-in redaction would be the wrong default. `--no-llm-backstop`
      opts out of the model pass only.
- [x] A8. Briefing "Session telemetry" section + alert callout. The alert
      reports only the single worst condition (abort > respawn > deep chain);
      a stacked list buries the one that matters.
- [x] A9. Tests: 37 redaction, 32 telemetry, 21 integration

**Phase B — Supervisor daemon**
- [x] B0. `should_respawn()` pure function over the four trigger conditions (Resolved #5a)
- [x] B1. `nightly_core.supervisor` package — `trigger` / `registry` /
      `respawn` / `daemon` / `service`. Split five ways rather than the
      drafted three: the trigger is the safety argument and belongs alone
      in a module with no I/O, and the launchd/systemd plumbing has
      nothing to do with the poll loop.
- [x] B2. `nightly-supervisor` entrypoint in `[project.scripts]`
- [x] ~~B3. `watchdog` dependency~~ — **dropped, see B4**
- [x] B4. ~~FSEvents/inotify watcher with debounce~~ → **polling loop**.
      Approach B chose file-watching because it assumed v0.0.8 semantics,
      where `RESPAWN_REQUESTED` appeared *only* when the hook yielded to
      `host_cap` — so the file's creation event was the death signal, and
      sub-second latency mattered. v0.0.10 changed that: the marker is now
      written **preemptively on every forced-continuation block**, so it is
      present throughout a perfectly healthy session. A watcher firing on
      its creation would respawn into a live session on the first block.
      The real trigger (Resolved #5a) is the marker *persisting* while the
      `keepalive.log` heartbeat goes stale — a timeout, which no filesystem
      event can deliver. Polling is not a compromise here; it is the only
      mechanism that can express the condition. At a 30s interval against
      a 90s staleness threshold the cost is a handful of `stat` calls a
      minute, and dropping `watchdog` keeps the dependency set at five.
- [x] B5. Claude-host respawn launcher — **three strategies, not one**:
      tmux → macOS Terminal via `osascript` → headless `claude -p`. The
      draft assumed `osascript` on macOS and a "binary spawn" on Linux,
      but a bare binary spawn does not work: Claude Code's interactive
      mode wants a TTY and a detached daemon has none. tmux leads because
      it is the only option that survives disconnect and works over SSH.
      The headless fallback is labelled distinctly (`is_full_resume` is
      False) since it has no live Stop hook — one turn, not a resumed
      night, and the briefing must not overclaim.
- [x] B6. Backoff guard + `SUPERVISOR_ABORTED` marker. The marker is
      *sticky* — `should_respawn` treats its presence as budget-exhausted,
      without which the daemon re-fires every poll once the count passes
      the cap.
- [x] B7. `install` / `uninstall` verbs (launchd plist + systemd unit).
      Plist written as XML directly, not via `pyobjc` as the risk section
      suggested — validated by `plistlib` in tests, and it avoids a
      heavyweight macOS-only dependency in a cross-platform package.
      Install prints a full disclosure screen and requires confirmation.
- [x] B8. `status` verb reading `state.json`, plus `logs`. Status calls
      out the installed-but-not-running case specifically: that is the
      failure where the operator believes they are covered and is not.
- [x] B9. Registry maintenance in `nightly start`. **Not** in `nightly
      conclude` as drafted — concluding one run says nothing about
      whether the repo is still in use, and deregistering there would
      unwatch a repo the operator uses nightly. Staleness is handled by
      the 30-day `last_seen` prune plus a path-existence check.
- [x] B10. Integration tests — 58 covering trigger, registry, launcher,
      poll loop, budget, config, service files, rules, and doctor.

**Phase C — Documentation + cross-host stubs**
- [x] C1. README "Supervisor" section + "Session telemetry and the
      redaction contract". Also corrected a stale claim next to it: the
      README still described the `SESSION_ACTIVE` marker as having a
      4-hour TTL, removed back in v0.0.3.
- [x] C2. Host-comparison matrix row — folded into the keep-alive hook
      table as a "Supervisor respawn" column rather than a separate
      matrix, since the two are read together.
- [x] C3. Rules block **rule 14** (operator-only verbs), propagated to
      every host by `seed_rules`. Explicitly carves out `status` / `logs`
      as read-only and permitted — forbidding diagnosis alongside
      installation would leave the agent unable to explain a session that
      had been respawned.
- [x] C4. `nightly doctor` nudge. Evidence-driven: fires only when a run
      actually carries a `RESPAWN_REQUESTED` marker, and always reports
      `ok` — a `missing` status would be an instruction rule 14 forbids
      the agent from following.
- [x] C5. Per-host respawn-launcher stubs raising `NotImplementedError`,
      each documenting the specific blocker for that host (Codex lacks an
      `acceptEdits` equivalent; Cursor's agents run in an unreachable
      cloud VM; Antigravity needs GUI automation; opencode has no hook
      surface so the marker is never written at all — hence v3).
