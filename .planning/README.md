# .planning/ — human-curated planning

Long-lived, low-volume design intent that survives Nightly being uninstalled.
Nightly reads from here on every cold start; it never writes back. See
[`brainstorm.html`](brainstorm.html) §04 ("How the two folders interact") for
the full ingestion contract.

## Layout

```
.planning/
├── brainstorm.html         # the design (this is the seed document)
├── rfcs/                   # proposals before they become code
├── decisions/              # short ADRs — what we chose, why
├── conventions.md          # (optional) house style, branching, review norms
└── nightly.scope.md        # (optional) hard scope limits for the agent
```

## Conventions

- One markdown (or html) file per artifact.
- RFCs and ADRs use a YAML front-matter block with `status:` so Nightly can
  filter (`accepted` RFCs become a cascade entry per brainstorm §03).
- File names start with a number for chronological order:
  `001-multi-harness-adapter.md`, `001-pick-uv-over-poetry.md`.
- This folder is **not** auto-gitignored. Whether you commit any of it is your
  preference; the brainstorm.html author commits `.planning/` and ignores
  `.nightly/runs/`, but any combination is valid.
