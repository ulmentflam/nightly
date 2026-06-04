# Nightly

[![CI](https://github.com/ulmentflam/nightly/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ulmentflam/nightly/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

> A continuously-running, host-native coding agent. Drops into the
> coding CLI you already use, picks tasks off a priority cascade, runs
> them in isolated worktrees, and lands review-shaped PRs by morning.

Nightly is a Python orchestrator that runs *inside* Claude Code, Codex,
opencode, Cursor, Antigravity, or vanilla Gemini CLI — turning a chat
session into a self-directed, drainable one. It picks work off a
priority cascade (in-flight plans → approved RFCs → ranked GitHub
issues → proposer suite), dispatches specialist sub-agents in isolated
git worktrees, surfaces draft PRs for morning review, and stops
cooperatively — never `SIGKILL`.

**Status:** six host integrations, headless dispatch, pre-commit hooks,
GitHub Actions CI, type-clean tree. The full design lives in
[`.planning/brainstorm.html`](.planning/brainstorm.html); this README is
the operator's view.

---

## Install

The recommended path is two steps: install the binary once, then drop
into each repo with `/nightly-init` from inside the host.

```bash
# 1. install the `nightly` binary + bootstrap uv if missing
curl -fsSL https://raw.githubusercontent.com/ulmentflam/nightly/main/install.sh | bash

# 2. install the host skill globally (default host = claude)
nightly init --scope user
```

That writes a `nightly` shim to `~/.local/bin/nightly` and installs the
main `/nightly` skill plus four companions (`/nightly-init`,
`/nightly-conclude`, `/nightly-update`, `/nightly-bug`) under the host's
user-scope skill directory (e.g. `~/.claude/skills/`,
`~/.codex/skills/`, `~/.gemini/commands/`). From then on, in any repo:

```text
> /nightly-init
```

`/nightly-init` shells out to `nightly init` against the current
directory: creates `.nightly/`, writes `config.yml`, installs the
project-scope skill files, merges the Stop-hook entry, and seeds the
autonomy contract into `AGENTS.md` / `CLAUDE.md`. Idempotent — safe to
re-run.

For other hosts, pass `--host`:

```bash
nightly init --host codex --scope user
nightly init --host opencode --scope user
nightly init --host cursor --scope user
nightly init --host antigravity --scope user
nightly init --host gemini --scope user      # vanilla Gemini CLI
```

The installer is idempotent — re-run it to update. Override defaults
with `NIGHTLY_HOME` (clone target, default `~/.local/share/nightly`),
`NIGHTLY_VERSION` (branch / tag / SHA, default `main`), `NIGHTLY_BIN`
(shim location, default `~/.local/bin`), or `NIGHTLY_REPO` (git URL —
for forks).

### Homebrew (macOS / Linux)

```bash
brew install --HEAD ulmentflam/tap/nightly
```

Same shim shape as `install.sh`: the formula puts a `uv`-driven
binary on PATH. `--HEAD` installs from `main` (the only channel
until a tagged release lands); drop the flag once `v0.0.1` ships.
After install, run `nightly init --scope user` as above.

### From source (development)

```bash
git clone git@github.com:ulmentflam/nightly.git
cd nightly
make install                    # uv sync --all-packages
make check                      # ruff + Pyrefly + pytest
uv run nightly --help           # or `source .venv/bin/activate && nightly --help`
```

### Headless / unattended

For cron, CI, or "drain the backlog while I sleep" runs, skip the host
slash command and drive the cascade directly:

```bash
cd <some-repo>
nightly init                                  # one-time per repo
nightly start                                 # create a session
nightly task add-retry -d "Add retry budget to auth client"
nightly run --concurrency 2 --max-tasks 5     # multi-task headless dispatch
nightly brief                                 # render briefing.html + vault
nightly vault open                            # open the knowledge-graph dashboard
```

### Slash commands

Installed into every host alongside the main skill:

| Command              | Purpose                                                       |
|----------------------|---------------------------------------------------------------|
| `/nightly`           | Start (or continue) a Nightly session — walks the cascade.    |
| `/nightly-init`      | Bootstrap Nightly in the current repo — runs `nightly init`.  |
| `/nightly-conclude`  | Wind down the running session — human-only off-ramp.          |
| `/nightly-update`    | Pull the latest Nightly release; refresh skills + hooks.      |
| `/nightly-bug`       | Bundle run state into a debug report (file as issue).         |

---

## Hosts

Six hosts ship with first-class integrations. The three *primary*
hosts support full headless dispatch; the three *secondary* hosts ship
the launcher only — their headless story is a remote queue, deferred.

| Host           | Tier      | Skill installed at                        | Sub-agent dispatch                 | OS sandbox                |
| -------------- | --------- | ----------------------------------------- | ---------------------------------- | ------------------------- |
| Claude Code    | primary   | `.claude/skills/nightly/SKILL.md`         | Task tool + MCP                    | none (in-proc)            |
| Codex CLI      | primary   | `.codex/skills/nightly/SKILL.md`          | MCP / `codex exec`                 | Seatbelt + Landlock       |
| opencode       | primary   | `.opencode/agents/nightly/SKILL.md`       | `POST /session/:id/fork` + SSE     | none                      |
| Cursor         | secondary | `.cursor/commands/nightly.md`             | Background Agents (cloud VM)       | cloud VM (Background)     |
| Antigravity    | secondary | `.gemini/antigravity/agents/.../SKILL.md` | Agent Manager + `brain/<GUID>/`    | none                      |
| Gemini CLI     | secondary | `.gemini/commands/nightly.toml`           | Headless `gemini --prompt`         | none                      |

Install per host with `nightly init --host <name>`. Switch scopes with
`--scope user` (global) vs the default `--scope project`. Subscription
auth propagates from the host's cached creds (`~/.claude/`,
`~/.codex/`, `~/.local/share/opencode/`, `~/.gemini/`, …) — Nightly
never asks for an API token. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`GEMINI_API_KEY` etc. are env-var fallbacks for sandboxed CI.

`antigravity` and `gemini` are distinct hosts sharing the `.gemini/`
namespace: Antigravity writes managed-agent files under
`.gemini/antigravity/agents/` (desktop IDE); vanilla Gemini CLI writes
custom-command TOML under `.gemini/commands/`. Both register an
`AfterAgent` Stop-style hook against `.gemini/settings.json` — the
merge is idempotent if you co-install them.

---

## What it does

- **Priority cascade** — picks the next task automatically by walking a
  fixed precedence: resume in-flight plans → unblocked-by-approval plans
  → accepted RFCs in `.planning/rfcs/` → highest-ranked open GitHub
  issue (via `gh`) → PR rescue (new review feedback on open Nightly PRs)
  → ideation (proposer suite) → terminal *nothing*.
- **Per-task isolation** — every task lives in its own `git worktree`
  forked from a base branch, so concurrent dispatches cannot stomp on
  each other.
- **Specialists** — four sub-agent roles (`implementer`, `tester`,
  `reviewer`, `researcher`) with their own context windows, dispatched
  through each host's native primitive.
- **Proposer suite** — when the backlog is empty, scans for TODO/FIXME
  audits, autofixable lint debt (ruff), and `Any` type holes; writes
  ranked draft issues to `<run>/proposed/issues/` for human review.
- **Autonomy bar** — proposals are auto-promoted only when *single
  file*, *< 80 LOC*, and category in `{lint_debt, dep_upgrade}`.
  Everything else waits for human approval.
- **Hybrid briefing** — Python owns the deterministic structural
  skeleton (hero counts, task pills, approvals list); the agent owns
  three narrative slots (`briefing.md`, per-task `notes.md`,
  `lessons.md`) that survive context compaction.
- **Headless mode** — `nightly run` drives the cascade in cron / CI
  by spawning `claude -p` / `codex exec` / `opencode run` directly.
  Opt-in `--concurrency N` parallelism via `asyncio.gather` + worktrees.
- **Six-category refusal policy** — destructive git, production state,
  external comms / publishing, network egress to unknown domains, scope
  creep, bypassing test or type safety. Refused operations are recorded
  retro (not blocking) for review in the morning briefing.
- **Cooperative drain** — `nightly conclude` writes a marker the loop
  honours at the next batch boundary. Never `SIGKILL`. Half-finished
  work parks as `status: parked` on a dedicated branch.
- **Cascade PR-awareness** — the cascade skips RFC checkbox items whose
  text appears in an open Nightly PR's title or body, so the agent
  doesn't re-pick work that's already awaiting review. Both signals are
  best-effort substring matches with a bias toward false negatives.
- **Stacked-PR prevention** — RFC 004 §C prevents accidental PR chains
  by forcing each new worktree to branch from `main`. A task can opt
  into a stacked geometry by declaring `depends_on_pr: <N>` in its
  plan frontmatter; the driver then bases the worktree on PR #N's
  branch and instructs the agent to begin the PR body with
  `Depends on #<N>`. The morning briefing renders an all-declared chain
  with a teal "declared dependency chain" panel and any accidental
  geometry with the existing rose "stacked PR geometry" panel (RFC
  001 §B2) so reviewers can distinguish the two at a glance.
- **Worktree readiness** — before any task dispatch, `nightly worktree
  doctor` probes the repo's pre-commit infrastructure. `missing_python_dep`
  and `missing_pre_commit_hook` are auto-remediated; other failures surface
  as a `worktree_remediation` proposal so a broken worktree can't silently
  waste a session turn.
- **Knowledge graph (vault)** — `nightly brief` also builds a navigable
  knowledge graph under `.nightly/vault/`: every run, task, lesson, and PR
  becomes a node; parent/spawned/derived_from edges form a DAG. Open the
  dashboard with `nightly vault open` — it runs in any browser, no server
  needed (sql.js + wasm are vendored). The dashboard surfaces cross-run
  patterns the briefing alone doesn't.

---

## CLI reference

`nightly --help` lists everything; this is the operator-facing subset.

| Group | Command | Purpose |
|---|---|---|
| **Setup** | `nightly init [--host <h>] [--scope project\|user]` | Bootstrap `.nightly/` + install the host launcher. |
| | `nightly status` | Show repo state, installed hosts, current run. |
| | `nightly uninstall [--host <h>] [--scope ...]` | Remove the host launcher. |
| | `nightly doctor` | Repair a drifted install (scaffold, config, rules, skills). |
| | `nightly update [--version <ref>] [--dry-run]` | Self-upgrade Nightly's source + refresh installed hosts. |
| | `nightly version` | Print the installed Nightly version. |
| | `nightly info` | One-liner description + where to start. |
| **Run lifecycle** | `nightly start ["<seed task>"]` | Create a new run; optionally seed `tasks/0001-<slug>/`. |
| | `nightly task <slug> [-d "<desc>"]` | Add a task to the current run. |
| | `nightly conclude` | Mark the current run as concluding (non-blocking drain). |
| | `nightly stop` | Immediate hard-stop request — the next turn boundary ends. |
| | `nightly session start` / `session stop` | Arm / disarm the Stop-hook keep-alive marker. |
| | `nightly brief [--run <id>]` | Render `<run>/briefing.html`. |
| **Cascade** | `nightly next` | Walk the priority cascade; print the next pick + rationale. |
| | `nightly triage [--top N]` | List ranked open GitHub issues (best-effort, needs `gh`). |
| | `nightly plans` | Every plan across runs with status. |
| | `nightly specialist <role>` | Print the system prompt for one of the 4 roles. |
| | `nightly keepalive [--name <s>]` | Show think-harder strategies when the cascade goes empty. |
| **Ideation** | `nightly propose [--top N]` | Dry-run the proposer suite — list candidates. |
| | `nightly ideate` | Run proposers; write draft issues to disk. |
| **Headless** | `nightly headless <prompt> [--host <h>] [--cwd <p>] [--timeout S]` | Single-shot host CLI invocation. |
| | `nightly run [--host <h>] [-n N] [-j K] [--timeout-per-task S]` | Drive the cascade in headless mode; opt-in parallel. |
| **PRs & CI** | `nightly feedback [--branch <b>] [--apply]` | Show PR review feedback; `--apply` lands it on the matching plan. |
| | `nightly rescue` | Preview the next PR-rescue candidate (Nightly-authored, new feedback). |
| | `nightly ci` | Print CI status across open Nightly PRs. |
| | `nightly verify` | Detect & run the repo's linters / formatters / type checkers. |
| | `nightly bug` | Bundle run state into a debug report; optionally file an issue. |

`make help` covers the dev-loop side: `install`, `sync`, `lint`, `fmt`,
`type`, `test`, `check`, `install-hooks`, `pre-commit`, `brief`,
`planning`, `clean`, `nuke`.

---

## How it works

### The `.nightly/` runtime folder

Everything Nightly writes lives in one place:

```
.nightly/
├── config.yml              # refusal policy, hosts, branch prefix, budgets
├── plans/                  # per-task plans (currently empty; reserved)
├── runs/
│   ├── CURRENT             # pointer at the active run id
│   └── <run-id>/
│       ├── tasks/<NNNN>-<slug>/
│       │   ├── plan.md     # YAML frontmatter: status, slug, created
│       │   ├── proposal.md # PR/proposal body
│       │   ├── uncertainty.md
│       │   ├── notes.md    # per-task narrative slot
│       │   └── diff.patch
│       ├── proposed/
│       │   ├── approvals/  # refused-operation records
│       │   ├── planning/   # draft RFCs / ADRs
│       │   └── issues/     # ideation candidates
│       ├── briefing.md     # session narrative slot
│       ├── lessons.md      # lessons-learned slot
│       ├── briefing.html   # rendered morning briefing
│       └── CONCLUDE        # sentinel — drains on next loop iteration
├── atlas/                  # repo wiki (scaffolded; rolling refresh deferred)
└── memory/                 # cross-session memory (scaffolded; reserved)
```

### The `.planning/` design folder

Human-authored design intent (RFCs, ADRs, the brainstorm itself) lives
in `.planning/`. Nightly reads it on every cold start (it's a context
source alongside `AGENTS.md` / `CLAUDE.md`) but **never writes to it**
on its own — with one explicit exception (RFC 005). When the operator
invokes `/nightly` interactively with a feature seed, the host skill
calls `nightly seed-rfc "<title>"` to stub a new accepted RFC under
`.planning/rfcs/` carrying `author: nightly-seed` in the frontmatter.
The cascade then picks the stub's unchecked items via the standard
`accepted_rfc` slot — same shape as hand-authored RFCs 001–004,
distinguishable on retro audit by the `author` field. The
seed-stubbed pathway is opt-in (the skill decides based on seed
shape) and never fires for one-line bugfix seeds, which keep using
the `nightly start <seed>` single-task pathway.

### The priority cascade

```
1. resume_in_flight     — plans with status: in_progress
2. unblocked_approval   — plans with status: blocked: approval + approval_granted
3. accepted_rfc         — RFCs in .planning/rfcs/ with unchecked tasks
4. github_issue         — highest-ranked open issue via `gh`
5. pr_rescue            — Nightly-authored open PR has new feedback (reviews,
                          bot comments, or failed CI) since last reconcile
6. ideate               — proposer suite, top auto-PR-eligible result
7. nothing              — terminal; write narrative + brief + exit
```

Run `nightly next` at any time to see what the cascade would pick.

### Plan status lifecycle

```
ready → in_progress → dispatching → done
                                  ↘ parked
                                  ↘ blocked: approval
```

`dispatching` is a transient sentinel the driver uses to claim a plan
during multi-task parallel dispatch — the cascade explicitly skips it.

### Headless dispatch + parallelism

`nightly run` walks the cascade. For each pick (up to `--concurrency
N` in parallel) it:

1. Claims the plan via `status: dispatching`.
2. Calls `nightly worktree create <slug>` to place an isolated git
   worktree at the config-aware, iCloud-safe location.
3. Spawns the host's headless CLI (`claude -p --output-format json`, etc.)
   with the task prompt + working directory set to the worktree.
4. Reconciles: if the agent updated the plan to `done` / `parked`,
   respect that. Otherwise infers from the headless exit code.
5. Loops until the cascade returns `nothing`, `--max-tasks` is hit,
   or `<run>/CONCLUDE` appears on disk.

Single-process by contract: two concurrent `nightly run` invocations
against the same repo can race on plan-status updates.

---

## Repo layout

```
.
├── Makefile                          # dev-loop entrypoints
├── README.md
├── LICENSE                           # MIT
├── pyproject.toml                    # uv workspace + tool configs
├── packages/
│   ├── nightly-core/                 # loop · cascade · drain · briefing · proposers · headless
│   ├── nightly-host-claude/          # primary host
│   ├── nightly-host-codex/           # primary host
│   ├── nightly-host-opencode/        # primary host
│   ├── nightly-host-cursor/          # secondary host
│   ├── nightly-host-antigravity/     # secondary host (Antigravity IDE)
│   └── nightly-host-gemini/          # secondary host (vanilla Gemini CLI)
├── .planning/                        # human-authored design
│   ├── brainstorm.html               # the full design doc (open with `make brief`)
│   ├── decisions/
│   └── rfcs/
└── .nightly/                         # agent runtime state (gitignored by default)
```

Each host package implements `NightlyHostIntegration` from
`nightly_core.contract` — five methods cover the launcher lifecycle
(`install` / `uninstall` / `is_installed` / `session_id` /
`auth_status`) and three more cover runtime (`run_headless` for the
primaries; `dispatch_sub_agent` / `request_approval` reserved for
future work). Adding another host is one new package + one entry in
`_HOST_LOADERS`.

---

## Development

```bash
make install            # uv sync --all-packages (creates .venv)
make install-hooks      # arm the pre-commit hook (ruff + pyrefly on every commit)
make check              # ruff (lint) + Pyrefly (types) + pytest
make test               # just pytest
make lint               # ruff check
make type               # Pyrefly type-check
make fmt                # ruff format (write)
make pre-commit         # run every pre-commit hook against every file
make brief              # open .planning/brainstorm.html in the browser
make clean              # remove ruff / pyrefly / pytest caches
make nuke               # clean + drop the venv
```

The dev loop is **Python 3.12+ · uv · ruff · Pyrefly · pytest**. Tests
cover all six hosts plus the core (run lifecycle, cascade, proposers,
autonomy bar, headless, worktree, driver, CLI). The full check suite
runs in ~3 seconds.

### Pre-commit hook

`make install-hooks` arms a [pre-commit](https://pre-commit.com/) hook
that runs `ruff check` and `pyrefly check` on every `git commit`. Tests
are deliberately off the hot path — they live in `make check` and CI.
To bypass for an in-progress WIP commit (emergencies only — CI still
enforces the merge gate): `git commit --no-verify`.

### Adding a host integration

1. Scaffold a new package under `packages/nightly-host-<name>/`
   following the pattern of `nightly-host-codex/`.
2. Implement `<Name>HostIntegration(NightlyHostIntegration)` —
   `install` / `uninstall` / `is_installed` / `session_id` /
   `auth_status` are required; `run_headless` is required for
   non-interactive use; `dispatch_sub_agent` / `request_approval`
   may stay `NotImplementedError` until you wire them.
3. Ship a `skill.md` with host-specific dispatch and sandbox notes.
4. Register the loader in `nightly_core.cli._register_host_loaders`.
5. Add tests under `packages/nightly-host-<name>/tests/`.

### Adding a proposer

1. Subclass `Proposer` (`nightly_core.proposers.base`) with a unique
   `id` and a `propose(root)` implementation returning `Iterable[Proposal]`.
2. Choose a category from `ProposerCategory` — only `lint_debt` and
   `dep_upgrade` clear the autonomy bar by default.
3. Add it to `default_proposers()` in `proposers/registry.py`.
4. Add tests.

---

## Design

The full design — architectural decisions, prior-art research, the
refusal-policy rationale, host-comparison matrices, references — lives
in [`.planning/brainstorm.html`](.planning/brainstorm.html). After
cloning, open it with:

```bash
make brief
```

The brainstorm covers the architecture, state machine, host-comparison
matrix, refusal policy, and prior art (Devin · OpenHands · SWE-agent ·
Sweep · AutoCodeRover · Copilot · Factory · Replit · Amp · Cosine and
others) with inline references throughout.

---

## License

[MIT](LICENSE).
