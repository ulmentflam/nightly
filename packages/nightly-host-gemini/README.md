# nightly-host-gemini

Nightly host integration for the vanilla Google **Gemini CLI**
(`google-gemini/gemini-cli`).

The Skill installs as a Gemini CLI custom command at
`.gemini/commands/nightly.toml` (project scope) or
`~/.gemini/commands/nightly.toml` (user scope). Companion commands
(`/nightly-conclude`, `/nightly-update`, `/nightly-bug`,
`/nightly-init`) ship alongside.

This host is distinct from `nightly-host-antigravity` — both target
`.gemini/`, but the Antigravity host writes to
`.gemini/antigravity/agents/` (Antigravity IDE's managed-agent
surface) while this one writes to `.gemini/commands/` (Gemini CLI's
custom-command surface). They share the same `.gemini/settings.json`
hook surface (`AfterAgent`).

See `.planning/brainstorm.html` for the design and host comparison
matrix.
