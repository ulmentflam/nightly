---
status: implemented
phase_a: implemented
phase_b: implemented
phase_c: implemented
sized: true
title: First-class pi host — in-process keep-alive via the extension API
created: 2026-07-29
sized_on: 2026-07-29
accepted_on: 2026-07-29
implemented_on: 2026-07-29
author: operator
source: interactive_seed
estimated_effort: ~11h across 3 phases
upstream: https://github.com/earendil-works/pi
status_note: |
  Shipped as designed. Two things the RFC did not anticipate are recorded
  as D1/D2 in the checklist: a pre-existing drift in `supported_hosts()`,
  and a test-isolation hole that let a full-suite run write into the
  author's real `~/.pi/`. The second changed the design — `pi_home` is now
  resolved per call from `$NIGHTLY_PI_HOME` rather than cached at import.
  Live pi 0.87.1 validation passed on 2026-09-25 with a deterministic
  loopback provider and local Ollama qwen2.5:7b-instruct. Both verified
  continuation, disarmed release, and isolation of unrelated repositories.
---

# RFC 013 — First-class pi host

## Status

`implemented` — 2026-07-29, same session as the RFC 008 / RFC 010
close-out that prompted it.

**Live validation completed 2026-09-25.** Real pi 0.87.1 sessions with
both a deterministic loopback provider and Ollama's `qwen2.5:7b-instruct`
continued into a second model turn, exited after the fixture was disarmed,
and left unrelated repositories at one turn. The first run exposed an
open-stdin deadlock: `nightly hook stop` waits for EOF, but the extension
never closed its child process's input. The extension now sends an empty
JSON payload and closes stdin before awaiting the result. An executable
Node regression test covers this subprocess boundary.

These checks exercise pi's actual extension loader and event loop. They
do not install or start the respawn supervisor service.

Originally `proposed` — operator request, 2026-07-29 interactive session.

Note that `pi` is **already in `HostId`**. `contract.py` lists it, and
`model_probe.py` knows its env-var fingerprints. What it does not have is
an integration package, so `nightly init --host pi` cannot install
anything and there is no keep-alive. This RFC promotes pi from
"recognized by the router" to a host with the full contract.

## Context

Nightly currently ships six host integrations, and every one of them
implements the keep-alive the same way: Nightly writes a **hook script**
into the host's settings, the host spawns it as a subprocess at each turn
boundary, the script reads JSON on stdin and writes a decision on stdout.
That shape is forced on us — it is the only extension point those hosts
expose — and it is the direct cause of Nightly's worst operational
failures:

- **Claude Code's without-progress cap.** A Stop hook that blocks 8 times
  consecutively without progress gets overridden by the host, ending the
  session. Nightly raises it via `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, but
  it is a host-side backstop we can only push back, never remove.
- **Bug reports #13 / #16 / #19 / #25**, the `stop_hook_active` misread,
  and ultimately **the entire RFC 010 supervisor** — a background daemon
  whose sole job is to restart sessions the hook could not save.
- **Cursor's `loop_limit`**, which caps `followup_message` continuations
  at 5 by default.

pi's extension API is a different kind of surface, and the difference is
the reason this RFC is worth writing rather than adding a seventh
hook-script host.

### What pi actually offers

Verified against `earendil-works/pi` at `main`, 2026-07-29:

**Extensions are in-process TypeScript modules**, loaded from
`~/.pi/agent/extensions/` (global) or `.pi/extensions/` (project), with a
rich lifecycle. The event that matters:

```
├─► agent_end                                            │
└─► agent_settled  (no retry / compaction / follow-up left)
```

`agent_settled` is documented as firing when "Pi will not continue
running automatically" — the precise semantics of a Stop hook, but
delivered as a function call inside the process. And the docs note
`ctx.isIdle()` is true there *"unless another extension started a new
run"* — confirming an extension may start one. The mechanism is
`pi.sendMessage()`:

```typescript
pi.sendMessage(
  { customType: "nightly", content: reason, display: true },
  { deliverAs: "followUp", triggerTurn: true },
);
```

`deliverAs: "followUp"` waits until the agent has no more tool calls;
`triggerTurn: true` starts an LLM response when idle. That is
force-continue, with **no consecutive-block cap anywhere in the
mechanism** — nothing is overriding a hook, because nothing is a hook.

**Context files are native.** pi loads `AGENTS.md` and `CLAUDE.md` from
`~/.pi/agent/`, every ancestor directory, and the cwd — and per
`security.md`, *"`AGENTS.md` and `CLAUDE.md` context files are loaded
regardless of project trust."* Nightly's rules block already writes
there. The entire rules-propagation half of a host integration is done
before we start.

**Skills follow the Agent Skills standard.** pi discovers `SKILL.md`
directories under `.pi/skills/`, `.agents/skills/`, `~/.pi/agent/skills/`,
and `~/.agents/skills/`, exposes them as `/skill:name`, and explicitly
documents pointing `settings.json` at `~/.claude/skills`. Nightly's
existing SKILL.md format needs no translation.

**Headless and sessions are first-class**: `pi -p "<prompt>"` (print
mode), `--mode json` (JSONL event stream), `--mode rpc`, `pi -c`
(continue most recent), `pi -r`, `--session <path|id>`, `--session-id`,
`--fork`. Sessions live in `~/.pi/agent/sessions/`.

**Sub-agents are not built in.** pi ships a *subagent example extension*
that spawns separate `pi` processes with isolated context. That is
architecturally identical to what `dispatch.start_background` already
does for every other host, so Nightly does not need pi's version.

**Thinking levels** are `off | minimal | low | medium | high | xhigh |
max` via `--thinking`. Nightly's `ReasoningEffort` is `low | medium |
high | xhigh | max` — a subset, so RFC 007 routing maps 1:1 with no
translation table.

### The two things that will actually bite

**1. Project trust silently disables project-local extensions in
headless mode.** From `settings.md`: *"Non-interactive modes (`-p`,
`--mode json`, and `--mode rpc`) do not show a trust prompt. Without an
applicable saved trust decision, they use `defaultProjectTrust` from
global settings: `ask` (default) and `never` ignore those project
resources."* A keep-alive extension installed to `.pi/extensions/` would
therefore **load interactively and silently not load headlessly** — the
worst possible failure mode, because it works when you test it by hand
and fails at 03:00. Resolved #3 addresses this.

**2. The existing model probe is wrong.** `model_probe.py:91` declares
`"pi": (("run", "--help"), ("--help",))`. pi has no `run` subcommand;
its flags are `-p` / `--mode` / `--model` / `--print`. The first probe
tuple always fails and the fallback carries it. Harmless today, wrong
regardless, and it must be fixed before the probe is load-bearing.

## Non-goals

- **Replacing the hook-script architecture for the other six hosts.**
  They have no in-process surface; the hook script is not a design
  choice we are free to revisit for them.
- **Reimplementing the cascade in TypeScript.** The extension must stay
  a thin adapter. Every decision — cascade walk, livelock reroute,
  context diet, digest refresh, telemetry — already lives in
  `compute_stop_hook_decision` and must keep living there, or pi
  silently diverges from every other host the first time that logic
  changes.
- **Using pi's subagent extension for specialist dispatch.** Nightly's
  `start_background` already spawns detached host processes and records
  PID/log/tier state that the vault and briefing depend on. Adopting
  pi's parallel mechanism would fork that state.
- **Shipping a pi *provider*.** pi is a unified multi-provider LLM API
  in its own right. Nightly routes to hosts, not to model APIs; pi's
  provider layer is pi's business.
- **Bundling a TypeScript toolchain.** The extension ships as a single
  `.ts` file that pi's own runtime loads. Nightly does not compile,
  bundle, typecheck, or test it with `tsc`.
- **Supporting pi's `--mode rpc`.** The JSONL `--mode json` stream is
  sufficient for headless dispatch and matches the `run_headless`
  contract. RPC is a richer bidirectional surface with no current need.

## Proposed direction

Three approaches for the keep-alive, which is the only genuinely new
part. **Approach B** ships as v1.

---

### A — Hook-script parity: shell out from a generic wrapper

Treat pi like the other six. Find whatever pi surface can run a
subprocess at a turn boundary and point it at `nightly hook stop`.

**Pros:**
- Zero new artifact types. Nightly keeps shipping only markdown and JSON.
- Maximum consistency: seven hosts, one mechanism, one thing to debug.

**Cons:**
- **pi has no such surface.** Extensions are the turn-boundary API, and
  they are TypeScript modules. There is no "run this command on
  `agent_settled`" setting to write. Approach A is not merely worse; it
  does not exist without first writing the extension it claims to avoid.
- Even if it did, it would spawn a Python process per turn boundary and
  inherit the wire-format fragility we already have.

---

### B — Thin TypeScript extension that shells out to `nightly hook stop`

Ship `nightly-keepalive.ts`. On `agent_settled`, it spawns
`nightly hook stop --format pi`, parses the JSON decision, and — when the
decision is `block` — calls `pi.sendMessage(reason, { deliverAs:
"followUp", triggerTurn: true })`.

**Pros:**
- **All decision logic stays in Python.** The extension is roughly 60
  lines: spawn, parse, forward. Cascade changes, livelock reroutes,
  context-diet blocks, telemetry, and digest writes reach pi
  automatically because they are all inside `compute_stop_hook_decision`.
- **No consecutive-block cap.** `sendMessage` is a queue insertion, not
  a hook veto. Nothing overrides it.
- Reuses the existing `HOOK_FORMATS` wire abstraction, which was built
  for exactly this — a fifth format is a one-line addition.
- The extension can be installed **globally**, sidestepping the project-
  trust trap (Resolved #3).

**Cons:**
- A `.ts` file is a new artifact type in a Python repo. It needs its own
  install path, its own drift check, and it cannot be exercised by
  pytest the way a rendered markdown skill can.
- One subprocess spawn per turn boundary — same cost the other six hosts
  already pay, so not a regression, but not the free in-process call the
  extension API tempts you toward.

---

### C — Port the cascade decision into the extension

Reimplement `compute_stop_hook_decision` in TypeScript so the keep-alive
is fully in-process with no subprocess at all.

**Pros:**
- Fastest possible turn boundary — no process spawn, no JSON round trip.
- Could read `.nightly/` state directly and render prompts natively.

**Cons:**
- **Two implementations of the most safety-critical logic in the
  project.** The Stop decision governs whether an unattended overnight
  session continues or dies. Two copies means they drift, and the drift
  surfaces at 03:00 on the host nobody tested.
- The cascade reaches into `gh`, git, plan parsing, the proposer suite,
  and the vault. Porting it is not porting a function; it is porting
  Nightly.
- Rejected without much deliberation. The performance argument is
  irrelevant — a subprocess spawn per *turn boundary* is noise next to
  an LLM call.

---

## Resolved technical decisions

**1. Approach B ships as v1.** Approach A does not exist without B's
artifact; Approach C duplicates safety-critical logic. B is the only
option that keeps one implementation of the decision while using pi's
better continuation mechanism.

**2. New package `nightly-host-pi`,** matching the six existing host
packages: `packages/nightly-host-pi/src/nightly_host_pi/` with
`integration.py`, `skill.py`, `skill.md`, `respawn.py`, and the new
`keepalive.ts`. Registered in `cli._HOST_LOADERS` and in the
`[project.optional-dependencies]` host extras.

**3. The keep-alive extension installs GLOBALLY, to
`~/.pi/agent/extensions/nightly/index.ts`** — not to `.pi/extensions/`,
even at `--scope project`. This is the single most important decision in
the RFC and it deliberately breaks the pattern every other host follows.

Reason: project-local extensions are gated behind project trust, and
non-interactive modes **do not prompt** — they silently fall back to
`defaultProjectTrust`, whose default (`ask`) means *ignore project
resources*. A project-local keep-alive would work when the operator
tested it interactively and fail silently in every headless run. That is
precisely the class of bug RFC 010 exists to clean up after.

Consequences, both handled:

- The extension fires in **every** pi session on the machine, including
  ones that have nothing to do with Nightly. It therefore no-ops
  immediately unless `.nightly/runs/CURRENT` resolves to a run carrying
  `SESSION_ACTIVE` for the session's cwd. Same guard the other hosts
  use; load-bearing here rather than merely tidy.
- `nightly init --host pi --scope project` writes a **project** SKILL.md
  and a **global** extension. `nightly doctor` and the uninstall path
  must both understand that split, and the install output must say so
  plainly rather than letting the operator discover it.

**4. `--scope project` still writes the skill to `.pi/skills/nightly/SKILL.md`.**
Skills are subject to project trust too, but a missing skill degrades
visibly (the agent cannot find `/skill:nightly`) rather than silently, and
`nightly init` can save the trust decision. Recommend `.agents/skills/`
as an alternative in docs for operators sharing one skill tree across
harnesses — pi reads that path natively.

**5. A fifth wire format: `pi`.** `HOOK_FORMATS` gains `"pi"`, and
`format_decision` returns:

```json
{"deliver_as": "followUp", "message": "<reason>"}
```

Reusing `claude_code`'s `{"decision": "block"}` would work, since the
extension only reads one field — but `"block"` is a lie in pi's model.
Nothing is being blocked; a message is being queued. The format names
what actually happens, and the allow-stop case stays the universal `{}`.

**6. The extension's guard order, which must not change:**

1. Resolve the repo root from the session cwd. Not a repo → return.
2. `.nightly/runs/CURRENT` missing → return.
3. `SESSION_ACTIVE` absent → return. **Non-Nightly sessions are
   untouched**, which is what makes a global install acceptable.
4. Spawn `nightly hook stop --format pi`. Non-zero exit, timeout, or
   unparseable output → return (never trap the session on our bug).
5. Empty object → allow the stop.
6. `deliver_as` present → `pi.sendMessage(...)`.

Every failure path releases the session. A keep-alive that traps a user
who never asked for Nightly is far worse than one that occasionally
fails to continue.

**7. `run_headless` uses `pi -p --mode json`.** The JSONL stream carries
`agent_end` with the message list, which is what `HeadlessResult.output`
wants. Add `-a`/`--approve` so headless dispatch trusts the project —
without it, project settings and skills are ignored and the dispatched
agent cannot find the Nightly skill it was asked to run.

**8. Specialist dispatch reuses `start_background`,** with
`build_argv("pi", ...)` returning:

```
pi -p --mode json -a [--model <m>] [--thinking <effort>] <prompt>
```

No new dispatch primitive. `model_flag` in the tier config is `--model`;
the effort flag is `--thinking`, and RFC 007's `ReasoningEffort` values
are already a subset of pi's thinking levels, so the mapping is
identity.

**9. Fix the model probe.** `model_probe.py:91` becomes
`"pi": (("--version",), ("--help",))`. The current `("run", "--help")`
targets a subcommand pi does not have. Also add `PI_MODEL` to the
env-var fingerprints if probing confirms pi exports it; leave
`("PI_SESSION", "PI_AGENT")` in place regardless.

**10. Respawn launcher (RFC 010 §C5) is real for pi, not a stub** — and
it is *better* than the Claude one. `pi -c` continues the most recent
session for the cwd, so a respawned pi resumes the actual conversation
rather than starting fresh from `/nightly`. The launcher reuses the same
tmux → Terminal → headless ladder:

```
pi -c -a          # interactive resume, in a terminal
pi -p -a "/skill:nightly"   # headless fallback
```

This makes pi the **second** supported respawn host and the first where
respawn is a true resume.

**11. The supervisor matters less for pi, and the docs must say why.**
pi has no consecutive-block cap, so the failure mode that motivated RFC
010 does not exist here. Crashes, OOM, and disconnects still do, so the
supervisor is still worth installing — but the honest framing is
"belt-and-braces for crashes" rather than "the thing that saves your
night," and the README host matrix should not imply otherwise.

**12. Drift detection for a TypeScript artifact.** `nightly doctor`
cannot parse `.ts` meaningfully, so the check is a version marker: the
extension's first line is
`// nightly-keepalive v<__version__> — do not edit`, and doctor compares
that version to the installed package's. Mismatch → re-install. Same
shape as `_REQUIRED_SKILL_TOKENS`, one token, no parsing.

**13. Tests do not run pi.** The extension is asserted as *text*
(contains the guard order, the version marker, the `sendMessage` call)
and the Python side is tested normally: argv construction, format
emission, install/uninstall paths, doctor drift. An end-to-end test
needs npm, a pi install, and a model — that is a manual smoke step in
the merge gate, documented, not automated.

**14. `keepalive_support` stays `"forced"`; a new `keepalive_mechanism`
carries the distinction.** The contract today is
`KeepaliveSupport = Literal["forced", "soft", "none"]`, and it answers
"can this host be made to continue against its will?" For pi the answer
is unambiguously `"forced"` — more reliably than for any hook host,
since nothing can override a queued follow-up.

What the README matrix wants to show is a *different* axis: how the
forcing happens. Overloading `keepalive_support` with `"extension"`
would conflate capability with mechanism and break every existing
consumer that branches on `== "forced"`. So the contract gains:

```python
KeepaliveMechanism = Literal["hook", "extension", "rules"]
```

defaulting to `"rules"`, set to `"hook"` by the five hook hosts and
`"extension"` by pi. Purely descriptive — nothing branches on it except
rendering.

## Risks

- **The global-extension decision surprises operators.** Someone runs
  `nightly init --host pi --scope project` and gets a file in their home
  directory. Mitigation: install output states the path and the reason
  in one line; `nightly doctor` reports both locations; README documents
  it under the pi section. The alternative — a keep-alive that silently
  does nothing headlessly — is strictly worse.

- **pi's extension API is young and may change.** `agent_settled`,
  `sendMessage`, and the `deliverAs` options are all documented, but pi
  is a fast-moving project. Mitigation: the extension is ~60 lines and
  touches three API surfaces; the version marker (Resolved #12) makes
  staleness detectable; failures release the session rather than
  trapping it.

- **A globally-installed extension misfires in an unrelated repo.** If
  the `SESSION_ACTIVE` guard is wrong, every pi session on the machine
  gets Nightly prompts injected. Mitigation: guard order is Resolved #6
  and is the first thing the tests assert; it fails closed at every step.

- **Project trust blocks the skill even when the extension loads.** The
  extension is global and fires; the skill is project-local and was not
  trusted, so `/skill:nightly` does not resolve and the injected prompt
  lands with nothing to act on. Mitigation: `nightly init --host pi`
  offers to write the trust decision to `~/.pi/agent/trust.json`;
  `nightly doctor` flags "extension installed but project untrusted" as
  a distinct, named condition rather than a generic failure.

- **`pi -p` output parsing drifts.** `--mode json` is versioned
  (`{"type":"session","version":3,...}`) and could change shape.
  Mitigation: `run_headless` reads only `agent_end` and tolerates
  unknown event types; a version bump degrades to "returned no parsed
  output" rather than crashing.

- **Two keep-alive architectures to maintain.** Six hook-script hosts
  plus one extension host means the next change to continuation
  behavior has two shapes to update. Mitigation: the extension owns no
  decision logic (Resolved #6 step 4), so nearly every such change is
  confined to `compute_stop_hook_decision` and reaches pi for free.

## Implementation phases

Three phases, ~11h. Phase A is independently useful — it fixes a real
bug and makes `nightly init --host pi` stop lying about what it can do.

### Phase A — Package skeleton, probe fix, skill install (~4h)

- **A1.** `packages/nightly-host-pi/` with `pyproject.toml`,
  `integration.py`, `skill.py`, `skill.md`, `py.typed`. Registered in
  `cli._HOST_LOADERS`, the workspace members list, and host extras.
- **A2.** `install` / `uninstall` / `is_installed` writing
  `.pi/skills/nightly/SKILL.md` (project) or
  `~/.pi/agent/skills/nightly/SKILL.md` (user). Conclude / update / bug /
  init sub-skills follow the same layout the other hosts use.
- **A3.** Fix `model_probe.py`'s pi entry: `("--version",)` then
  `("--help",)`. Confirm the env fingerprints against a real install.
- **A4.** `session_id()` from `PI_SESSION` when present, falling back to
  the shared generator like the other hosts.
- **A5.** Tests: install/uninstall round trip at both scopes, loader
  registration, probe argv, `registry_totality` covers pi.

**Merge gate:** `nightly init --host pi` installs a working skill;
`nightly doctor` reports pi; no keep-alive yet, and the install output
says so explicitly rather than implying coverage.

### Phase B — The extension keep-alive (~5h)

- **B1.** `"pi"` added to `HOOK_FORMATS`; `format_decision` emits
  `{"deliver_as": "followUp", "message": reason}`.
- **B2.** `keepalive.ts` shipped as package data, implementing the
  Resolved #6 guard order and carrying the Resolved #12 version marker.
- **B3.** Install writes it to `~/.pi/agent/extensions/nightly/index.ts`
  at **both** scopes; uninstall removes it; install output names the
  global path and the project-trust reason in one line.
- **B4.** New `keepalive_mechanism` field on the integration contract
  (Resolved #14) so the README matrix and `nightly status` can say
  "extension" without misreporting pi's `keepalive_support`.
- **B5.** `nightly doctor` version-marker drift check; plus the distinct
  "extension installed but project untrusted" condition.
- **B6.** `nightly init --host pi` offers to record the project trust
  decision in `~/.pi/agent/trust.json`.
- **B7.** Tests: format emission; extension text asserts guard order,
  version marker, and `sendMessage` shape; install/uninstall touches
  both locations; doctor flags a stale marker and an untrusted project.

**Merge gate:** Phase A merged; a manual smoke run confirms a real pi
session force-continues and that a pi session in an unrelated repo is
untouched. Both outcomes recorded in the PR body — this is the one
behavior no automated test covers.

### Phase C — Dispatch, headless, respawn, docs (~2h)

- **C1.** `build_argv("pi", ...)` → `pi -p --mode json -a [--model]
  [--thinking]`. `model_flag` and effort mapping in the tier config
  defaults.
- **C2.** `run_headless` parsing the `--mode json` JSONL stream, reading
  `agent_end`, tolerating unknown event types.
- **C3.** Replace `nightly-host-pi/respawn.py`'s stub with a real
  launcher using `pi -c -a` (Resolved #10).
- **C4.** README: pi row in the host matrix with `keepalive: extension`
  and `supervisor: ✅ v1 (true resume)`; a short subsection on the
  global-extension decision and project trust; the honest note that the
  supervisor matters less for pi (Resolved #11).
- **C5.** Tests: argv construction per tier, headless parse of a
  recorded JSONL fixture, respawn ladder with an injected runner.

**Merge gate:** Phases A + B merged; `nightly dispatch start <slug>
--role implementer` completes against pi; README matrix matches
behavior.

## Sized checklist

**Phase A — Package skeleton, probe fix, skill install**
- [x] A1. `packages/nightly-host-pi/` skeleton + loader/workspace/extras registration
- [x] A2. Skill install / uninstall / is_installed at both scopes
- [x] A3. Fix `model_probe.py` pi probe argv (`run --help` → `--version`)
- [x] A4. `session_id()` from `PI_SESSION` / `PI_AGENT` with shared fallback
- [x] A5. Tests: install round trip, loader registration, probe argv, registry totality

**Phase B — Extension keep-alive**
- [x] B1. `"pi"` in `HOOK_FORMATS`; `format_decision` emits the `deliver_as` shape
- [x] B2. `keepalive.ts` with the Resolved #6 guard order + version marker
- [x] B3. Global extension install/uninstall at both scopes; install output explains the split
- [x] B4. `keepalive_mechanism` field on the integration contract (Resolved #14).
      All six existing hosts declare theirs too (`hook` ×5, `rules` for
      opencode) so the field is total rather than pi-only.
- [x] B5. `nightly doctor` version-marker drift + untrusted-project condition.
      The trust check reports **`warning`, not `missing`** — `missing` fails
      `healthy` and exits non-zero, which would be wrong twice: doctor
      cannot repair the decision (it lives inside pi and belongs to the
      operator), and nothing Nightly does is broken by it, since every
      dispatch Nightly builds passes `-a`. Also required making the
      keep-alive check *mechanism-aware*: the old check gated on
      `scope == "project"`, which is right for hook hosts and wrong for a
      global extension.
- [x] B6. `nightly init --host pi` offers to record the trust decision.
      Degrades to guidance when stdin is not a TTY — `typer.confirm` raises
      `Abort` on EOF, which killed an otherwise-successful init from CI or
      a piped install script over an optional setting.
- [x] B7. Tests: format, extension text, dual-location install, doctor conditions

**Phase C — Dispatch, headless, respawn, docs**
- [x] C1. `build_argv("pi", ...)` with `--model` / `--thinking`. Threading
      effort required a new `effort` parameter on `build_argv` and
      `start_background`: until pi, no host had a real reasoning-effort
      flag, so tier intent only ever reached agents as a prompt directive.
      Ignored by every other host, so a host gaining such a flag later is
      additive.
- [x] C2. `run_headless` over the `--mode json` JSONL stream, via
      `parse_json_stream`. Reads `agent_end`, falls back to accumulated
      `message_end` text when the stream was truncated, skips unknown
      event types so an upstream schema addition costs nothing.
- [x] C3. Real `respawn.py` using `pi -c -a`
- [x] C4. README host matrix row + pi subsection + honest supervisor note
- [x] C5. Tests: argv per tier, headless JSONL fixture parse, respawn ladder

**Unplanned, found while implementing**
- [x] D1. `supported_hosts()` restated the dispatchable-host list as a
      second literal instead of returning `HEADLESS_HOSTS` — the exact
      drift `HEADLESS_HOSTS`'s own docstring claims to have eliminated. It
      went stale the moment pi landed. Now derived, with a test pinning
      the two together.
- [x] D2. `packages/conftest.py` — a workspace-wide autouse fixture
      redirecting `$NIGHTLY_PI_HOME` to `tmp_path`. **A full-suite run
      wrote real files into the author's `~/.pi/agent/`** during
      implementation: pi is the only host that writes a global path at
      project scope, and integrations Nightly builds internally
      (`doctor --all`, `init`, `update`) take no override argument. No
      single test file reproduced it — only the combination — which is
      the kind of pollution that survives review. Fixed at the root by
      resolving `pi_home` per call from the env rather than caching
      `Path.home()` at import.
