# nightly-host-codex

[Nightly](../../README.md) host integration for **OpenAI Codex CLI** — primary
host. Registers Nightly as a Codex skill + slash command. Sub-agent dispatch
uses MCP for in-session work and spawns `codex exec --json` when a fresh
Seatbelt / Landlock sandbox is needed.

**Status:** Phase 0 stub. Real implementation lands in Phase 4.
