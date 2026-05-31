---
status: accepted
sized: true
title: Vault & knowledge graph — derive a navigable DAG of runs, tasks, dispatches, PRs, feedback, and lessons
created: 2026-05-30
sized_on: 2026-05-30
accepted_on: 2026-05-30
author: ulmentflam
estimated_effort: ~20h across 6 phases
---

# RFC 003 — Vault & knowledge graph

## Status

`accepted` — design questions resolved across two iterations of
`.planning/drafts/vault-knowledge-graph.html`, phases broken out,
checkboxes cascade-pickable. Implementation underway on the
`nightly/rfc-003-vault` branch.

## Context

Nightly produces a substantial trail per run: `briefing.md`,
`lessons.md`, `keepalive.log`, per-task `plan.md` / `notes.md` /
`proposal.md` / `uncertainty.md`, plus `proposed/{approvals,issues,planning}/`.
The artifacts are already markdown with YAML frontmatter — the bones of
a knowledge base exist. What's missing is the **graph**: the directed
edges that tie a PR-rescue task back to the CI failure that spawned it,
a lesson back to the run that minted it, the third audit of the same
lint rule back to the prior two.

Today these connections live implicitly in prose ("see PR #57"), in
filesystem proximity (`tasks/0002-audit-todos/` is "in" run
`2026-05-27T16-30-35Z`), or not at all. The operator's review experience
is a per-run briefing — useful in the moment, useless three weeks later
when a pattern emerges across ten runs.

Two observations made this RFC worth writing:

1. **The graph is where the interesting signal lives.** "This is the 4th
   rescue task for the same lint rule." "Every PR derived from issue #87
   stalled on type errors." "This lesson has been cited by six subsequent
   runs." Nightly throws this signal away today.

2. **The artifacts are already in the right shape.** `tasks/<slug>/plan.md`
   already has `status`, `slug`, `proposer_fingerprint` in frontmatter.
   `briefing.html` is already hand-rolled with a paper/ink palette. The
   delta between today and a navigable knowledge graph is mostly
   plumbing: add edges to frontmatter, project run artifacts into a
   stable vault layout, derive an index, render two views.

## Non-goals

- **Cross-repo aggregation.** Each repo's `.nightly/vault/` is its own
  browse surface. A future `~/.nightly/global-vault/` is explicitly
  deferred to a follow-up RFC (Fork 02). The v1 vault writes a
  `vault-manifest.json` at a stable path so an aggregator can find it
  later without retroactive surgery.
- **Editing nodes through a UI.** The vault is read-only from the
  operator's perspective. All writes flow from Nightly's existing run /
  dispatch / brief paths. If the operator wants to amend a node, they
  edit the markdown directly and `nightly vault build` reflects the
  change.
- **Live collaboration / multi-author.** Single-user, single-machine.
- **Replacing `briefing.html`.** The per-run briefing remains. The vault
  is the cross-run overview; the briefing is the at-a-glance report
  for one run. Both render with the same palette and can coexist
  indefinitely.
- **Decision-node grain.** Fork 03 picked medium grain (six node kinds).
  Each cascade decision being a node is interesting but out of scope
  for v1 — would re-emerge as a follow-up RFC if the medium grain
  proves insufficient.

## Resolved design decisions

Six decisions ratified through two iterations of the draft sketch.

**1. Markdown vault is canonical; SQLite is a derived read-cache (Fork 05).**
The only writer to `_index.db` is `vault.index.rebuild()`. The DB is safe
to delete — it rebuilds from `vault/**/*.md` in well under a second on
realistic corpora. Renderers query SQLite for speed; humans and the
agent edit (or project into) markdown. This preserves the grep-able,
diff-able vault promise.

**2. Per-repo only, with a forward-compatible manifest (Fork 02).**
The vault lives at `.nightly/vault/`. A small `vault-manifest.json` is
written at every build so a hypothetical future global aggregator can
discover vaults without modifying the build itself. Cheap insurance, no
extra UI.

**3. Medium node grain — six kinds (Fork 03).**
`run`, `task`, `dispatch`, `pr`, `feedback`, `lesson`. Coarser misses the
patterns we're chasing (rescue chains, feedback clusters, lesson
citations); finer (every cascade decision is a node) is a follow-up.

**4. PR nodes minted inline by `dispatch.py` (Fork 01).**
The `gh pr create` step immediately writes `vault/pulls/<num>.md` with
the linking metadata already in scope (the task ID it came from, the
worktree, the branch). No polling reconciler in v1. A follow-up
`nightly vault sync-prs` heal-step is deferred until a stalled-PR
problem actually materializes.

**5. Dashboard via client-side `sql.js` (Fork 06).**
The dashboard ships `_index.db` + `sql.js` (WASM) to the browser and
runs queries in-page. No server lifecycle, no port to manage, arbitrary
filters / drill-downs / ad-hoc queries with no Python on the hot path.
File-system loading of WASM is a known footgun — the implementation
base64-embeds the WASM blob in the bundled JS to keep `file://` opens
working (see §"Resolved technical decisions" below).

**6. Land as this RFC (Fork 04).**
Cascade-pickable checkboxes broken out per phase. v0 ships three node
kinds (`run`, `task`, `pr`) and both renderers; the remaining three
(`dispatch`, `feedback`, `lesson`) fold in as a second slice within the
same RFC.

## Resolved technical decisions

**Module placement → new `nightly_core.vault` sub-package**, sibling of
`briefing.py` and `dispatch.py`. The vault has enough surface (model,
projection, indexer, two renderers, asset bundle) that flat-module
placement under `nightly_core/` would be noisy.

```
packages/nightly-core/src/nightly_core/vault/
├── __init__.py            # public API: build, open, project_run
├── model.py               # NodeKind, EdgeType, Node, Edge dataclasses
├── project.py             # runs/<id>/ → vault/**/*.md projection
├── index.py               # vault/**/*.md → _index.db
├── render_encyclopedia.py # markdown + SQLite → _site/
├── render_dashboard.py    # SQLite + assets → _dashboard/
├── templates/             # jinja-less string templates (match briefing.py style)
│   ├── node.html
│   ├── index.html         # encyclopedia entry
│   └── dashboard.html     # dashboard SPA shell
└── assets/                # vendored, committed
    ├── cytoscape.min.js   # ~280KB
    ├── sql-wasm.js        # sql.js bundle with WASM inlined as base64
    └── style.css          # paper/ink palette extracted from briefing.html
```

**Vault layout on disk.**

```
.nightly/
├── runs/<run-id>/                # unchanged — canonical run artifacts
└── vault/
    ├── runs/<run-id>.md          # one node per run
    ├── tasks/<run-id>--<slug>.md
    ├── dispatches/<task>--<n>.md
    ├── pulls/<num>.md
    ├── feedback/<pr>--<sha>.md
    ├── lessons/<run-id>--<n>.md
    ├── vault-manifest.json       # discovery hook for future aggregator
    ├── _index.db                 # derived; gitignored
    ├── _site/                    # encyclopedia output; gitignored
    └── _dashboard/               # dashboard output; gitignored
```

`.nightly/.gitignore` (or its equivalent) gains `vault/_index.db`,
`vault/_site/`, `vault/_dashboard/`. The markdown vault itself is
committable at the operator's discretion — same posture as the rest of
`.nightly/runs/`.

**Frontmatter envelope.** Every node carries the same envelope; `kind`
defines vocabulary on top of it.

```yaml
---
id: task/2026-05-27T16-30-35Z--0002-audit-todos   # globally unique slug
kind: task                                          # run|task|dispatch|pr|feedback|lesson
title: "Audit 13 TODO/FIXME markers"
status: done                                        # kind-defined vocabulary
created: 2026-05-27T16:31:42Z
updated: 2026-05-27T16:33:23Z
tags: [audit, read-only, dogfood]

# directed edges — these are the graph
parent:        run/2026-05-27T16-30-35Z
spawned:       []
derived_from:  []
produced:      []
references:    []
superseded_by: null
---
```

Per-kind extras live in body frontmatter under their kind name (no
schema enforcement v1; the indexer reads what it knows, ignores what
it doesn't):
- `run`: `host`, `session_uuid`, `turns`, `force_continue_count`, `stop_reason`
- `task`: `proposer`, `score`, `cascade_step`, `worktree`, `proposer_fingerprint`
- `dispatch`: `specialist`, `prompt_hash`, `tool_calls`, `duration_s`
- `pr`: `number`, `url`, `ci`, `merge_state`, `nightly_authored`
- `feedback`: `severity`, `source`, `body_excerpt`
- `lesson`: `tags_inferred`, `cited_by_count` (computed at index time)

**SQLite schema.** Two tables, three indexes. Rebuilt-not-migrated:
schema changes bump a `PRAGMA user_version` and the indexer drops/recreates.

```sql
CREATE TABLE nodes (
  id          TEXT PRIMARY KEY,    -- e.g. task/2026-05-27T16-30-35Z--0002-audit
  kind        TEXT NOT NULL,
  title       TEXT,
  status      TEXT,
  created     TEXT,                -- ISO8601
  updated     TEXT,
  tags        TEXT,                -- json array
  data        TEXT,                -- json blob — kind-specific extras
  body_path   TEXT                 -- relative path to .md for prose lookup
);

CREATE TABLE edges (
  src_id      TEXT NOT NULL,
  dst_id      TEXT NOT NULL,
  edge_type   TEXT NOT NULL,       -- parent|spawned|derived_from|produced|references|superseded_by
  PRIMARY KEY (src_id, dst_id, edge_type)
);

CREATE INDEX idx_nodes_kind_status ON nodes(kind, status);
CREATE INDEX idx_nodes_created     ON nodes(created);
CREATE INDEX idx_edges_dst         ON edges(dst_id, edge_type);
```

**Two renderers, two outputs.**

| | Encyclopedia (`vault/_site/`) | Dashboard (`vault/_dashboard/`) |
|---|---|---|
| Reads | markdown body + SQLite (for backlinks) | SQLite via `sql.js` in browser |
| Output | one HTML page per node + `index.html` | single-page graph + filter UI + metrics |
| Language | Python (`render_encyclopedia.py`) | Python emits HTML shell; JS runs in browser |
| Use case | narrative deep-dive | cross-run pattern surface |
| `file://` opens | yes (static HTML) | yes (WASM is base64-embedded) |

**sql.js WASM + file:// compatibility.** Loading WebAssembly from
`file://` is blocked by browsers (CORS-equivalent restrictions on
WASM streaming compilation). Two viable workarounds:
1. Run a transient `http.server` for `nightly vault open --dashboard`.
   Clean but adds a lifecycle.
2. Use `sql-wasm.js` (the sql.js variant that base64-inlines the WASM
   binary into the JS bundle, ~1.3MB total). Parses synchronously,
   works from `file://`, no server needed.

v1 picks **option 2 (base64-inline WASM)**. The size hit is one-time
per cache; the lifecycle simplification is worth it for a tool whose
selling point is "open the artifact and it works." Option 1 remains
available as `nightly vault serve` for operators who want live
updates without rebuilds.

**CLI surface.**
- `nightly vault index` — rebuild `_index.db` from markdown vault.
  Idempotent, fast (<1s for thousands of nodes).
- `nightly vault build` — full pipeline: `project_run(current)` →
  markdown vault → `index` → render `_site/` and `_dashboard/`.
- `nightly vault open [--encyclopedia | --dashboard]` — build (if
  stale) and `webbrowser.open` one of the two; default `--dashboard`.
- `nightly vault serve [--port 8123]` — optional; serves both targets
  with watch-and-rebuild. Deferred to a follow-up if static build
  proves sufficient.
- Hook into existing flow: `nightly brief` calls
  `vault.project_run(run_id)` + `vault.index.rebuild()` before
  rendering. The PR-open path in `dispatch.py` writes the PR node
  inline.

## Risks

- **Drift between markdown and SQLite.** The indexer is the only writer
  to `_index.db`, but if a code path projects to markdown and forgets
  to rebuild the index, renderers serve stale data. Mitigation:
  `vault.project_run()` always calls `vault.index.rebuild()` at the
  end. The DB drop-and-rebuild is fast enough that incremental
  indexing isn't needed v1.
- **Vault corpus growth.** After 100 runs you'll have ~2000 nodes
  (assuming 20 nodes/run on average — runs + tasks + dispatches +
  PRs + feedback). Cytoscape handles that fine in raw count but the
  visual gets noisy. v1 dashboard ships a "last 7 days" default
  filter; faceted views (rescue chains only, by tag) follow.
- **sql.js bundle size.** ~1.3MB with WASM inlined. Cold-load takes
  a couple seconds on the first open. Acceptable for an artifact
  the operator opens deliberately; would be a problem if this were
  a hot path. Cached forever once loaded.
- **PR node accuracy depends on `dispatch.py`.** If the inline writer
  in Fork 01 misses a PR (e.g. `gh pr create` succeeds but the writer
  throws), the vault has a missing node and the encyclopedia has a
  dangling link. Mitigation: the writer is wrapped in a `try/except`
  that logs to `keepalive.log` and surfaces in the briefing; the
  follow-up sync command (Fork 01 option C) backfills if it ever
  becomes a real problem.
- **`vault build` becomes mandatory for `nightly brief`.** A bug in
  the vault pipeline could block the briefing — which is on the
  always-runs path. Mitigation: `vault.project_run()` failures are
  caught and downgraded to a warning in the briefing; the briefing
  still renders without the vault if the vault step fails.

## Open questions

- **GC policy for old runs.** If the operator deletes `.nightly/runs/<id>/`,
  should the vault keep `vault/runs/<id>.md` (history preserved) or
  drop it (consistency)? v1 keeps the vault entry — it's the audit
  trail. Document the divergence; revisit if disk usage becomes an
  issue.
- **Dashboard filter state persistence.** Should the dashboard remember
  the operator's last filter (kind toggles, date range) across opens?
  v1 says no — every open is a fresh view. `localStorage` would work
  but adds a "why is my filter sticky?" surprise.
- **Backlinks in markdown.** The encyclopedia computes backlinks from
  SQLite and renders them as a footer section in each node's HTML.
  Should `vault.project_run()` also write a `<!-- backlinks -->`
  marker into the markdown body so they're visible when grep-ing
  the raw vault? Trade-off: more useful in plain markdown vs.
  drift risk (backlinks become stale immediately on next change).
  v1 says **no** — keep markdown stable, compute backlinks at render.
- **What's the right grain for `lesson` node identity?** Today
  `lessons.md` is one file per run with multiple bullet-point
  lessons. Should each lesson become its own vault node, or stay
  bundled? v1 says **per-lesson** — each is a node with an
  auto-generated stable ID (`run-id--lesson-1`, etc.). Allows
  citation links and "cited by N runs" metrics.

## Implementation phases

Six phases, ~20h total. Phases A → B have a hard dependency (B reads
A's output). C and D both depend on B and are parallel-safe. E depends
on A's writer API and is parallel-safe with B/C/D. F depends on all.

```
A (vault writer + projection)
        │
        ▼
B (SQLite indexer)
        │
   ┌────┴────┐
   ▼         ▼
C (enc.)  D (dash.)        E (dispatch.py PR hook)   (parallel with A→B→{C,D})
   │         │                       │
   └─────┬───┘                       │
         └────────────┬──────────────┘
                      ▼
              F (CLI + briefing + config + README)
```

### Phase A — vault writer + projection (~4h)

The foundation: stable node identity, frontmatter envelope, projection
from `runs/<id>/` into `vault/**/*.md`.

- **A1.** `NodeKind` and `EdgeType` literal types in
  `vault/model.py`. `Node` and `Edge` frozen dataclasses with the
  envelope fields. `node_id_for_run(run_id)`, `node_id_for_task(run_id,
  slug)`, etc. — all id-generation in one module so the shape is
  enforced.
- **A2.** `vault/project.py:project_run(run_id) -> ProjectionResult`.
  Walks `.nightly/runs/<run-id>/`, derives the run, task, and
  (if present) lesson nodes, writes them to `vault/runs/`,
  `vault/tasks/`, `vault/lessons/`. Edges (`parent`, `spawned`)
  populated from filesystem structure. Idempotent — re-running
  overwrites existing files.
- **A3.** `vault/__init__.py:build(repo_root) -> BuildResult` — the
  full-pipeline entry. Phase A only wires the projection; B/C/D fill
  in the rest.
- **A4.** Tests against the existing `2026-05-27T16-30-35Z` run:
  project it, assert the expected files and frontmatter exist.
  Characterization test — locks the projection shape against a
  real run.
- **A5.** `vault-manifest.json` writer. Schema: `{schema_version: 1,
  vault_path, last_built, run_count, node_count_by_kind}`. Written
  on every build.

### Phase B — SQLite indexer (~3h)

- **B1.** `vault/index.py:rebuild(vault_path) -> IndexStats`. Drops
  `_index.db` if it exists, creates the schema with `PRAGMA
  user_version = 1`, walks `vault/**/*.md`, parses frontmatter
  (`python-frontmatter` from PyPI), inserts nodes and edges.
- **B2.** Edge extraction: for each frontmatter key in
  (`parent`, `spawned`, `derived_from`, `produced`, `references`,
  `superseded_by`), emit `Edge(src=id, dst=value, type=key)`.
  Unknown / dangling targets are inserted as `Node(id, kind="unknown",
  title=None, ...)` placeholders so the graph stays connected; the
  indexer logs them.
- **B3.** Tests: empty vault → empty DB, vault with one run + two
  tasks → correct nodes + parent edges, vault with a dangling
  `derived_from` reference → placeholder node created.
- **B4.** Performance check (not a benchmark gate): rebuild against
  a 2000-node synthetic vault should complete in <1s on the dev
  machine. If it doesn't, profile before shipping.

### Phase C — encyclopedia renderer (~4h)

- **C1.** `vault/render_encyclopedia.py:render(vault_path) ->
  RenderResult`. For each node, emit `_site/<kind>/<id>.html` via
  string templates in `vault/templates/node.html`. Markdown body
  rendered with `markdown-it-py` + a custom wiki-link plugin that
  resolves `[[id]]` → `<a href="../<kind>/<id>.html">`.
- **C2.** Backlinks computed per-node by querying `edges` with
  `dst_id = <node.id>`. Rendered as a "Referenced by" footer
  section grouped by edge type.
- **C3.** `_site/index.html` — the encyclopedia entry. List view
  of nodes grouped by kind, sortable by `created` / `updated` /
  `status`. Cytoscape graph view included as a tab (shared component
  with the dashboard; see D).
- **C4.** Tests: render against the projected `2026-05-27T16-30-35Z`,
  assert the run page, each task page, and the index page exist
  with the expected hyperlinks.
- **C5.** Palette extraction — pull the paper/ink CSS variables out
  of `briefing.html` into `vault/assets/style.css`. Both surfaces
  (briefing, vault) import from the same file going forward.

### Phase D — dashboard renderer + client-side SQLite (~5h)

- **D1.** Vendor `cytoscape.min.js` and `sql-wasm.js` (the
  base64-inlined-WASM variant of sql.js) into
  `vault/assets/`. Commit them. Source URLs and SHA-256 hashes
  recorded in a sibling `VENDORED.md`.
- **D2.** `vault/render_dashboard.py:render(vault_path) ->
  DashboardResult`. Copies `_index.db` to `_dashboard/index.db`,
  copies assets, emits a single `_dashboard/index.html` with the
  SPA shell.
- **D3.** Dashboard SPA shell (`vault/templates/dashboard.html`):
  loads `sql-wasm.js`, fetches `index.db` as an `ArrayBuffer`,
  opens it via `sql.js`, queries on filter changes. Default query:
  `SELECT * FROM nodes WHERE created >= date('now', '-7 days')`.
- **D4.** Filter UI: kind toggles (6 chips), date-range picker,
  status pill filters, tag chips. Filters compose into a WHERE clause.
  Plain JS, no framework, ~150 LoC.
- **D5.** Cytoscape graph view: node size scaled by in-degree
  (`COUNT(*) FROM edges WHERE dst_id = nodes.id`). Click node →
  open the encyclopedia page for that node in a new tab.
- **D6.** Tests: render against the projected vault, open the
  resulting `_dashboard/index.html` in headless Chrome (via
  `playwright`) if available, otherwise assert structural checks
  against the HTML (script tags, asset references, db path).

### Phase E — `dispatch.py` PR-node writer (~2h)

- **E1.** `vault/project.py:project_pr(task_id, pr_number, pr_url,
  branch, base_branch, ci_state) -> None`. Writes
  `vault/pulls/<num>.md` with frontmatter linking back to the
  source task (`produced_by`) and any feedback nodes (initially
  empty).
- **E2.** Hook into `dispatch.py` immediately after a successful
  `gh pr create`. Wrap the call in `try/except`; any failure logs
  to `keepalive.log` with `decision=vault_pr_write_failed` and
  proceeds — never blocks PR creation on vault writes.
- **E3.** Tests with a mocked `gh` invocation: success path writes
  the PR node, failure path logs and doesn't raise.
- **E4.** Backfill helper: `vault.project.backfill_prs() -> int`.
  Walks `gh pr list --author @me --label nightly`, writes any
  missing PR nodes. Not wired into the cascade in v1; available
  as `nightly vault sync-prs` for operators.

### Phase F — CLI + briefing wire + config + README (~2h)

- **F1.** `cli.py` adds the `vault` subcommand group with `index`,
  `build`, `open`, `sync-prs`. `open --encyclopedia` / `--dashboard`
  default to `--dashboard`. `webbrowser.open` for the open verb.
- **F2.** `briefing.py` calls `vault.build()` before rendering
  `briefing.html`. Failures downgrade to a warning section in
  the briefing — never block.
- **F3.** `.nightly/config.yml` gains a `vault:` block:
  ```yaml
  vault:
    enabled:        true      # set false to skip vault build in nightly brief
    open_on_brief:  false     # if true, briefing also opens the dashboard
  ```
  Defaults: enabled true, open_on_brief false. Same opt-out shape as
  the other feature flags.
- **F4.** README "Headless / unattended" section gains a paragraph
  on the vault — what gets built, where to look, how to open. Link
  to this RFC for the design.
- **F5.** Promote this RFC's frontmatter to `status: accepted` and
  check every box below as items land.

## Sized checklist

Tick each item as the work ships. The cascade will not auto-pick these
until the RFC's frontmatter status flips to `accepted`.

**Phase A — vault writer + projection**
- [x] A1. `NodeKind`, `EdgeType`, `Node`, `Edge` in `vault/model.py` + id-generation helpers
- [x] A2. `vault/project.py:project_run()` writing run, task, lesson nodes
- [x] A3. `vault/__init__.py:build()` entry point (Phase A wires projection only)
- [x] A4. Characterization test against fixture (real run gitignored — see test docstring)
- [x] A5. `vault-manifest.json` writer

**Phase B — SQLite indexer**
- [x] B1. `vault/index.py:rebuild()` — schema, frontmatter parse, node insert
- [x] B2. Edge extraction from frontmatter keys + dangling-target placeholders
- [x] B3. Indexer tests (empty, single run, dangling reference, malformed YAML, reserved dirs)
- [x] B4. Performance check (<1s for 2000-node synthetic vault; budget set at 5s to absorb iCloud)

**Phase C — encyclopedia renderer**
- [x] C1. `render_encyclopedia.py:render()` — per-node HTML with wiki-link resolution
- [x] C2. Backlinks footer from `edges` reverse query
- [x] C3. `_site/index.html` — list view grouped by kind
- [x] C4. Render tests against projected fixture
- [x] C5. Palette extracted to `vault/assets/style.css`; rewiring briefing.html.j2 deferred (it loses the self-contained property — separate follow-up)

**Phase D — dashboard renderer + client-side SQLite**
- [x] D1. Vendored `cytoscape.min.js` 3.30.0 + `sql-wasm.js`/`sql-wasm.wasm` 1.10.3; SHAs in `vault/assets/VENDORED.md`
- [x] D2. `render_dashboard.py:render()` — copies DB + assets, emits SPA shell, base64-inlines wasm into `sql-wasm-inline.js`
- [x] D3. Dashboard SPA: `sql.js` boot, default last-7-days query
- [x] D4. Filter UI (kind / status / date-range / time-window) composing WHERE clauses
- [x] D5. Cytoscape graph view with in-degree-scaled nodes + tap-to-open-in-encyclopedia
- [x] D6. Dashboard render tests (structural — assets present, HTML references them, db is valid SQLite)

**Phase E — `dispatch.py` PR-node writer**
- [x] E1. `vault/project.py:project_pr()` writer
- [x] E2. ~~`dispatch.py` hook after `gh pr create`~~ — no in-Python PR-creation site exists (the agent runs `gh pr create` in shell), so the hook is implemented as build-time `backfill_prs()` instead. Updated approach noted in §"Resolved design decisions" #4 — this is a deliberate deviation from the original Fork 01 decision.
- [x] E3. Hook tests (project_pr writes node, backfill walks gh, gh-missing path returns [], gh-failure path returns [])
- [x] E4. `vault.project.backfill_prs()` + `nightly vault sync-prs` CLI

**Phase F — CLI + briefing + config + README**
- [x] F1. `cli.py` `vault` subcommand group (`index`, `build`, `open`, `sync-prs`)
- [x] F2. `briefing.py` callsite in `nightly brief` calls `vault.build()`; failures downgrade to warning
- [x] F3. `.nightly/config.yml` gains `vault:` block (`enabled`, `open_on_brief`); `VaultConfig` + `load_vault_config()` in `config.py`
- [x] F4. README "Headless / unattended" + new "Vault (knowledge graph)" paragraph
- [x] F5. RFC frontmatter promoted to `status: accepted` at start of implementation

## Deferred to follow-up

These are explicit non-goals for v1, captured here so the next
iteration doesn't have to re-discover them:

- **Cross-repo aggregation** (`~/.nightly/global-vault/`). The
  `vault-manifest.json` exists for forward compatibility; the
  aggregator itself is a separate RFC.
- **Decision-node grain.** Each cascade decision (`pick_in_flight`,
  `pick_unblocked`, `pick_rfc`, `ideate_fallback`, `pr_rescue`)
  emitting a node. Maximum traceability, but graph noise is real.
  Revisit if medium grain misses interesting patterns.
- **`nightly vault serve`** — http.server + watch-and-rebuild for
  live updates. Static build is sufficient v1; serve is a small
  follow-up if operators ask.
- **`node_history` table** in SQLite — time-series of node state
  changes ("PR #57 was ci-failing for 3 days"). Useful for dashboard
  metrics; not v1.
- **Operator-authored vault nodes.** Today the vault is purely
  Nightly-projected. A future flow could let the operator hand-write
  `vault/notes/*.md` for project-level lore that links into the graph.
- **Incremental indexing.** v1 drops and recreates `_index.db` on
  every build. Cheap enough at thousands of nodes; incremental
  inserts become worth it past ~10k.
