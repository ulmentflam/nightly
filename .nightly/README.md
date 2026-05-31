# .nightly/ — agent runtime

This folder holds Nightly's transient runtime state: plans, run logs, the repo
atlas, summaries, the morning briefing. It is **renewable** — everything here
can in principle be regenerated from the repo plus a cold Nightly start plus
the human-curated artifacts in `.planning/`.

By default this folder's high-volume subdirectories (`runs/`, `atlas/`,
`memory/`) are gitignored at the repo root. Whether you commit anything in
here is entirely your call — see [`config.yml.example`](config.yml.example) and
the design doc in [`../.planning/brainstorm.html`](../.planning/brainstorm.html) §04
("Two folders. Split by audience, not by commit policy").

## Expected layout (created on first Nightly run)

```
.nightly/
├── config.yml              # actual config — copy from config.yml.example
├── atlas/                  # repo wiki — scaffolded; rolling refresh deferred
├── plans/                  # reserved
├── runs/<ts>/              # one folder per session
│   ├── briefing.md         # agent-written session narrative
│   ├── lessons.md          # agent-written cross-session takeaways
│   ├── briefing.html       # the morning report (rendered by `nightly brief`)
│   ├── CONCLUDE            # sentinel written by `nightly conclude`
│   ├── tasks/<n>-<slug>/
│   │   ├── plan.md         # YAML frontmatter + task scope
│   │   ├── notes.md        # per-task director's commentary
│   │   ├── diff.patch
│   │   ├── proposal.md     # PR-shaped artifact
│   │   └── uncertainty.md  # required
│   └── proposed/
│       ├── approvals/      # refused-op records — surfaced in briefing
│       ├── issues/         # ideation candidates for human review
│       └── planning/       # draft RFCs / ADRs for human promotion
├── memory/                 # cross-session memory — scaffolded; reserved
└── prompts/                # reserved
```

## What Nightly will and won't do here

- **Will**: create, update, append to anything under `.nightly/`.
- **Won't**: touch `.gitignore` for you. Won't auto-commit. Won't write back to
  `../.planning/` — that folder is read-only to Nightly.
