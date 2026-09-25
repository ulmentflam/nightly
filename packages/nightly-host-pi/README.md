# nightly-host-pi

[Nightly](../../README.md) host integration for **[pi](https://github.com/earendil-works/pi)**
— the only host whose keep-alive is not a hook script.

Every other Nightly host exposes a Stop-equivalent *hook*: Nightly writes a
command into the host's settings, the host spawns it as a subprocess at each
turn boundary, and the script answers on stdout. That mechanism is what
Claude Code's without-progress cap overrides, and it is why the RFC 010
respawn supervisor exists.

pi instead exposes an **extension API** — in-process TypeScript modules with a
lifecycle. Nightly subscribes to `agent_settled` (documented as firing when
"Pi will not continue running automatically") and continues the session with:

```typescript
pi.sendMessage(
  { customType: "nightly", content: reason, display: true },
  { deliverAs: "followUp", triggerTurn: true },
);
```

Nothing overrides a queued follow-up, because nothing is a hook. There is no
consecutive-block cap in this path.

The extension owns **no decision logic**. It shells out to
`nightly hook stop --format pi` and forwards the answer, so the cascade walk,
livelock reroute, context diet, digest refresh, and telemetry all stay in
`nightly_core.keepalive_hook` where the other six hosts already read them.

## Install layout

`nightly init --host pi` writes to **two** places, and this is deliberate:

| Artifact | Location | Why |
|---|---|---|
| Skill | `.pi/skills/nightly/SKILL.md` (project) or `~/.pi/agent/skills/nightly/SKILL.md` (user) | Normal per-scope install |
| Keep-alive extension | `~/.pi/agent/extensions/nightly/index.ts` — **always global** | See below |

The extension is global even at `--scope project` because pi gates
project-local extensions behind project trust, and **non-interactive modes
(`-p`, `--mode json`, `--mode rpc`) never prompt** — they fall back to
`defaultProjectTrust`, whose default (`ask`) means *ignore project resources*.
A project-local keep-alive would load when you tested it by hand and silently
fail in every headless run.

Because it is global, the extension no-ops immediately unless the session's
cwd resolves to a repo with an armed Nightly run (`SESSION_ACTIVE`). Sessions
that have nothing to do with Nightly are untouched.

See `.planning/rfcs/013-pi-first-class-host.md` for the full rationale.
