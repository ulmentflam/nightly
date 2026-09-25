// nightly-keepalive v__NIGHTLY_VERSION__ — do not edit; managed by `nightly init` / `nightly doctor`
//
// Nightly's keep-alive for pi. Installed to
// ~/.pi/agent/extensions/nightly/index.ts by `nightly init --host pi`.
//
// WHY AN EXTENSION AND NOT A HOOK
// Every other Nightly host registers a Stop-equivalent hook: a subprocess
// the host spawns at each turn boundary. Claude Code overrides such a hook
// after 8 consecutive blocks without progress, which is the single largest
// cause of stranded overnight runs. pi has no cap on this path — a queued
// follow-up is a queue insertion, not a hook veto, so nothing overrides it.
//
// WHY IT IS INSTALLED GLOBALLY
// pi gates project-local extensions behind project trust, and its
// non-interactive modes (-p, --mode json, --mode rpc) never prompt: without
// a saved decision they fall back to `defaultProjectTrust`, whose default
// ("ask") means *ignore project resources*. A keep-alive in .pi/extensions/
// would load when tested interactively and silently fail in every headless
// run. So it lives in the global directory and gates itself instead — see
// `shouldContinue` below, which returns early unless the session's cwd
// belongs to a repo with an armed Nightly run.
//
// WHY IT OWNS NO DECISION LOGIC
// The Stop decision governs whether an unattended session continues or
// dies. Two implementations of that would drift, and the drift would
// surface at 03:00 on the host nobody tested. So this file spawns
// `nightly hook stop --format pi` and forwards the answer. Cascade changes,
// livelock reroutes, context-diet blocks, telemetry, and digest writes all
// reach pi automatically because they live in Python.
//
// FAILURE POLICY
// Every failure path releases the session. Trapping a user who never asked
// for Nightly is far worse than occasionally failing to continue.

import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { promisify } from "node:util";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const execFileAsync = promisify(execFile);

/** How long `nightly hook stop` may take before we give up and release. */
const HOOK_TIMEOUT_MS = 60_000;

/** Marker written by `nightly session start`. Absent = not our session. */
const SESSION_ACTIVE = "SESSION_ACTIVE";

interface HookDecision {
  /** Present only when Nightly wants the session continued. */
  deliver_as?: "followUp" | "steer";
  message?: string;
}

/**
 * Walk up from `start` looking for a directory containing `.nightly/`.
 *
 * Not a git-root lookup: Nightly state is what matters, and a worktree can
 * sit outside the main checkout. Bounded by the filesystem root.
 */
function findNightlyRoot(start: string): string | null {
  let current = resolve(start);
  for (;;) {
    if (existsSync(join(current, ".nightly"))) return current;
    const parent = dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

/**
 * Resolve `.nightly/runs/CURRENT` to a run directory, or null.
 */
function currentRunPath(root: string): string | null {
  const pointer = join(root, ".nightly", "runs", "CURRENT");
  let runId: string;
  try {
    runId = readFileSync(pointer, "utf-8").trim();
  } catch {
    return null;
  }
  if (!runId) return null;
  const runPath = join(root, ".nightly", "runs", runId);
  return existsSync(runPath) ? runPath : null;
}

/**
 * The guard. Order is load-bearing and must not be reordered — it is the
 * whole reason a globally-installed extension is acceptable.
 *
 * 1. cwd is not inside a Nightly repo    -> release
 * 2. no CURRENT run                      -> release
 * 3. run is not armed (SESSION_ACTIVE)   -> release
 *
 * Only past all three do we spend a subprocess asking Nightly what to do.
 */
function armedRunPath(cwd: string): string | null {
  const root = findNightlyRoot(cwd);
  if (root === null) return null;
  const runPath = currentRunPath(root);
  if (runPath === null) return null;
  if (!existsSync(join(runPath, SESSION_ACTIVE))) return null;
  return runPath;
}

/**
 * Ask Nightly whether to continue. Returns null to release the session.
 *
 * `{}` on stdout is the universal allow-stop payload shared by every host
 * wire format; a `deliver_as` field is the pi-specific continue shape.
 */
async function askNightly(cwd: string): Promise<HookDecision | null> {
  let stdout: string;
  try {
    const pending = execFileAsync("nightly", ["hook", "stop", "--format", "pi"], {
      cwd,
      timeout: HOOK_TIMEOUT_MS,
      encoding: "utf-8",
    });
    // The hook reads stdin to EOF before deciding. Close the pipe so it
    // can process an empty host payload instead of waiting for our timeout.
    pending.child.stdin?.end("{}");
    const result = await pending;
    stdout = result.stdout;
  } catch {
    // Binary missing, non-zero exit, or timeout. Release — never trap the
    // session because our own tooling failed.
    return null;
  }

  const trimmed = stdout.trim();
  if (!trimmed) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;

  const decision = parsed as HookDecision;
  if (!decision.deliver_as || !decision.message) return null;
  return decision;
}

export default function (pi: ExtensionAPI) {
  // `agent_settled` fires when pi will not continue running automatically —
  // the same moment a Stop hook fires on other hosts. `agent_end` is too
  // early: pi may still auto-retry, auto-compact, or drain a follow-up
  // queue, and continuing there would inject a prompt mid-recovery.
  pi.on("agent_settled", async (_event, ctx) => {
    const cwd = process.cwd();

    if (armedRunPath(cwd) === null) return;

    const decision = await askNightly(cwd);
    if (decision === null) return;

    // Another extension (or a queued user message) started work while we
    // were shelling out. Injecting now would stack a second prompt on top
    // of live work.
    if (typeof ctx.isIdle === "function" && !ctx.isIdle()) return;

    pi.sendMessage(
      {
        customType: "nightly",
        content: decision.message as string,
        display: true,
      },
      {
        deliverAs: decision.deliver_as === "steer" ? "steer" : "followUp",
        triggerTurn: true,
      },
    );
  });
}
