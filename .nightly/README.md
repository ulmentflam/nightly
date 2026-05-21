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
├── atlas/                  # Devin-style "wiki" — refreshed on cold start
├── plans/                  # per-task scoped plans, append-only
├── runs/<ts>/              # one folder per session
│   ├── session.jsonl       # normalized event log
│   ├── summary.md          # human-readable narrative
│   ├── briefing.html       # the morning report (rendered on drain)
│   ├── tasks/<n>-<slug>/
│   │   ├── plan.md
│   │   ├── walkthrough.md
│   │   ├── diff.patch
│   │   ├── proposal.md     # PR-shaped artifact
│   │   └── uncertainty.md  # required
│   └── proposed/
│       ├── approvals/      # refused-op records — surfaced in briefing
│       └── planning/       # draft RFCs / ADRs for human promotion
├── memory/
│   ├── lessons.md
│   └── conventions.local.md
└── prompts/                # reusable system prompts per host
```

## What Nightly will and won't do here

- **Will**: create, update, append to anything under `.nightly/`.
- **Won't**: touch `.gitignore` for you. Won't auto-commit. Won't write back to
  `../.planning/` — that folder is read-only to Nightly.
