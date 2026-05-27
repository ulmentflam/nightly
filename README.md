# Nightly

> A continuously-running, host-native coding agent. Picks tasks from the
> backlog, opens isolated branches, dispatches specialist sub-agents, and
> lands review-shaped changes by morning.

Nightly is a Python-implemented orchestrator that runs *inside* the
coding-agent CLI you already use — Claude Code, Codex, opencode, Cursor,
or Google Antigravity — and turns it into a self-directed, drainable
session that resumes plans across runs, dispatches specialists in
parallel, and surfaces draft work to humans for review.

**Status:** all eight planned phases implemented. The full design lives
in [`.planning/brainstorm.html`](.planning/brainstorm.html); the README
below is the operator's view.

---

## Install

### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/ulmentflam/nightly/main/install.sh | bash
```

The installer is idempotent (re-run it to update), bootstraps `uv` if
it's missing, clones to `~/.local/share/nightly`, and drops a `nightly`
shim at `~/.local/bin/nightly`. Re-running checks for updates.

Honors a few env vars: `NIGHTLY_HOME` (clone target),
`NIGHTLY_VERSION` (branch/tag/SHA), `NIGHTLY_BIN` (shim location),
`NIGHTLY_REPO` (git URL — useful for forks).

### Global install + `/nightly-init` (drop into any repo)

The recommended workflow for active developers: install the skill once
at user scope, then bootstrap each repo from inside the host with the
`/nightly-init` slash command — no need to drop to a shell.

```bash
# one-time setup — installs the host skill at user scope
nightly init --scope user                  # default host = claude
# or for another host:
nightly init --host codex --scope user
nightly init --host gemini --scope user
```

That writes the main `/nightly` skill plus four companion commands
(`/nightly-init`, `/nightly-conclude`, `/nightly-update`,
`/nightly-bug`) into the host's user-scope skill directory
(`~/.claude/skills/`, `~/.codex/skills/`,
`~/.gemini/commands/`, etc.). From then on, in any repo:

```text
> /nightly-init
```

The skill shells out to `nightly init` against the current working
directory — creates `.nightly/`, writes `config.yml`, installs the
project-scope skill files, merges the Stop-hook entry, and seeds the
autonomy contract into `AGENTS.md` / `CLAUDE.md`. Idempotent: safe to
re-run.

### Or, install from source (for development)

```bash
git clone git@github.com:ulmentflam/nightly.git
cd nightly
make install                    # uv sync --all-packages
make check                      # ruff + Pyrefly + pytest
uv run nightly --help           # or `source .venv/bin/activate && nightly --help`
```

## Quick start

Once installed, point Nightly at the host you use:

```bash
nightly init                    # default = Claude Code, project scope
# or:  nightly init --host codex
# or:  nightly init --host opencode --scope user
# or:  nightly init --host gemini       # vanilla Gemini CLI
```

Then open your host (Claude Code, Codex, etc.) in any repo and ask
Nightly to work on a task — the Skill takes over. For unattended runs:

```bash
cd <some-repo>
nightly init                    # one-time per repo (or `/nightly-init` from inside the host)
nightly start                   # create a session
nightly task add-retry -d "Add retry budget to auth client"
nightly run --concurrency 2 --max-tasks 5   # multi-task headless dispatch
nightly brief                   # render .nightly/runs/<id>/briefing.html
```

### Slash commands

After install, five commands are available inside the host:

| Command              | Purpose                                                    |
|----------------------|------------------------------------------------------------|
| `/nightly`           | Start (or continue) a Nightly session — walks the cascade. |
| `/nightly-init`      | Bootstrap Nightly in the current repo — runs `nightly init`. |
| `/nightly-conclude`  | Wind down the running session — human-only off-ramp.       |
| `/nightly-update`    | Pull the latest Nightly release; refresh skills + hooks.   |
| `/nightly-bug`       | Bundle run state into a debug report (file as issue).      |

---

## Hosts

Nightly ships first-class integrations for six interactive hosts.
Three are *primary* (full headless support); three are *secondary*
(launcher only — their headless story is a remote queue, deferred).

| Host           | Tier      | Skill installed at                        | Sub-agent dispatch                 | OS sandbox                |
| -------------- | --------- | ----------------------------------------- | ---------------------------------- | ------------------------- |
| Claude Code    | primary   | `.claude/skills/nightly/SKILL.md`         | Task tool + MCP                    | none (in-proc)            |
| Codex CLI      | primary   | `.codex/skills/nightly/SKILL.md`          | MCP / `codex exec`                 | Seatbelt + Landlock       |
| opencode       | primary   | `.opencode/agents/nightly/SKILL.md`       | `POST /session/:id/fork` + SSE     | none                      |
| Cursor         | secondary | `.cursor/commands/nightly.md`             | Background Agents (cloud VM)       | cloud VM (Background)     |
| Antigravity    | secondary | `.gemini/antigravity/agents/.../SKILL.md` | Agent Manager + `brain/<GUID>/`    | none                      |
| Gemini CLI     | secondary | `.gemini/commands/nightly.toml`           | Headless `gemini --prompt`         | none                      |

Install per host: `uv run nightly init --host <name>`. Switch scopes
with `--scope user` for a global install vs the default `--scope
project`. Subscription auth propagates from the host's cached creds
(`~/.claude/`, `~/.codex/`, `~/.local/share/opencode/`,
`~/.gemini/`, etc.) — Nightly never asks for an API token.
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / etc. work
as env-var fallbacks for sandboxed CI.

`antigravity` and `gemini` are distinct hosts sharing the `.gemini/`
namespace: Antigravity writes managed-agent files under
`.gemini/antigravity/agents/` (desktop IDE), while vanilla Gemini CLI
writes custom-command TOML under `.gemini/commands/`. Both register an
`AfterAgent` Stop-style hook against `.gemini/settings.json` — that
merge is idempotent if you co-install them.

---

## What it does

- **Priority cascade** — picks the next task automatically by walking a
  fixed precedence: resume in-flight plans → unblocked-by-approval plans
  → accepted RFCs in `.planning/rfcs/` → highest-ranked open GitHub
  issue (via `gh`) → ideation (proposer suite) → terminal *nothing*.
- **Per-task isolation** — every task gets its own `git worktree`
  forked from a base branch. Concurrent dispatches cannot stomp on
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

---

## CLI reference

| Group | Command | Purpose |
|---|---|---|
| **Setup** | `nightly init [--host <h>] [--scope project\|user]` | Bootstrap `.nightly/` + install the host launcher. |
| | `nightly status` | Show repo state, installed hosts, current run. |
| | `nightly uninstall [--host <h>] [--scope ...]` | Remove the host launcher. |
| | `nightly version` / `nightly info` | Identity / phase summary. |
| **Run lifecycle** | `nightly start ["<seed task>"]` | Create a new run; optionally seed `tasks/0001-<slug>/`. |
| | `nightly task <slug> [-d "<desc>"]` | Add a task to the current run. |
| | `nightly conclude` | Mark the current run as concluding (non-blocking drain). |
| | `nightly brief [--run <id>]` | Render `<run>/briefing.html`. |
| **Cascade** | `nightly next` | Walk the priority cascade; print the next pick + rationale. |
| | `nightly triage [--top N]` | List ranked open GitHub issues (best-effort, needs `gh`). |
| | `nightly plans` | Every plan across runs with status. |
| | `nightly specialist <role>` | Print the system prompt for one of the 4 roles. |
| **Ideation** | `nightly propose [--top N]` | Dry-run the proposer suite — list candidates. |
| | `nightly ideate` | Run proposers; write draft issues to disk. |
| **Headless** | `nightly headless <prompt> [--host <h>] [--cwd <p>] [--timeout S]` | Single-shot host CLI invocation. |
| | `nightly run [--host <h>] [-n N] [-j K] [--timeout-per-task S]` | Drive the cascade in headless mode; opt-in parallel. |

`make help` shows the dev-loop side: `install`, `lint`, `type`, `test`,
`check`, `brief`, `clean`, `nuke`.

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
├── atlas/                  # repo wiki (Phase 9+)
└── memory/                 # cross-session memory (Phase 9+)
```

### The `.planning/` design folder

Human-authored design intent (RFCs, ADRs, the brainstorm itself) lives
in `.planning/`. Nightly reads it on every cold start (it's a context
source alongside `AGENTS.md` / `CLAUDE.md`) but **never writes to it**.

### The priority cascade

```
1. resume_in_flight     — plans with status: in_progress
2. unblocked_approval   — plans with status: blocked: approval + approval_granted
3. accepted_rfc         — RFCs in .planning/rfcs/ with unchecked tasks
4. github_issue         — highest-ranked open issue via `gh`
5. ideate               — proposer suite, top auto-PR-eligible result
6. nothing              — terminal; write narrative + brief + exit
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
2. Creates `git worktree add ../nightly-<slug>-<ts> -b nightly/<slug>-<ts>`.
3. Spawns the host's headless CLI (`claude -p --output-format json`, etc.)
   with the task prompt + working directory set to the worktree.
4. Reconciles: if the agent updated the plan to `done` / `parked`,
   respect that. Otherwise infer from the headless exit code.
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

The brainstorm covers all twelve sections of the original design and
includes ~40 source references (Devin · OpenHands · SWE-agent · Sweep ·
AutoCodeRover · Copilot · Factory · Replit · Amp · Cosine and others).

---

## License

[MIT](LICENSE).
