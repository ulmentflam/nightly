"""Session key-state digest — the "compact equivalent" for a Nightly run.

Nothing can programmatically trigger Claude Code's `/compact` from inside
an interactive session (no model-invocable compaction tool, hook block
`reason` text is not interpreted as a slash command, the auto-compact
threshold is not configurable). So Nightly cannot *force* compaction. What
it CAN do is make compaction lossless: render the handful of facts an agent
needs to keep working after losing context to a compact, and re-inject them
via the host's sanctioned `SessionStart(compact)` hook.

`render_digest` produces that compact markdown (~30-60 lines). `write_digest`
persists it to `.nightly/runs/<id>/digest.md` so two readers can pick it up:

- The `SessionStart(compact)` hook (`nightly hook session-start`) renders the
  digest fresh and injects it as `additionalContext` right after any
  compaction — auto or manual.
- The keepalive hook refreshes the on-disk copy on an interval and always
  before routing the agent to the planning phase (an ideate boundary is the
  natural compaction point: nothing in-flight is lost there).

Everything is best-effort. Each sub-section is wrapped so a failure renders
a one-line "unavailable" note rather than raising — a digest that is missing
its PR list is far more useful than no digest at all, and this code runs
inside a hook that must never crash the model's turn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from nightly_core._version import __version__
from nightly_core.runs import current_run

__all__ = [
    "render_digest",
    "write_digest",
]


_DIGEST_FILENAME = "digest.md"
"""Per-run file the digest is written to. Lives next to keepalive.log; both
are audit/continuity artifacts, neither is on the cascade hot path."""

_AUTONOMY_ONE_LINER = "if you can name a recommendation, execute it"
"""The whole Nightly autonomy contract reduced to one rule (see CLAUDE.md /
AGENTS.md). Closing every digest with it means the single most important
behavioral constraint survives a compaction even if nothing else does."""

# Marker files surfaced in the digest. Mirrors the keepalive_hook constants
# but kept local to avoid coupling the digest to that module's import graph
# (the keepalive hook imports the digest, not the reverse).
_MARKER_FILES: tuple[str, ...] = (
    "SESSION_ACTIVE",
    "CONCLUDE",
    "STOP",
    "RESPAWN_REQUESTED",
)

_TURN_COUNTER_FILE = "keepalive.turns"
_BLOCK_COUNTER_FILE = "keepalive.blocks"
_HISTORY_FILE = "keepalive.history"


def render_digest(root: Path | None = None) -> str:
    """Render the run's key-state digest as compact markdown.

    Returns a short markdown document (header + a handful of one-line
    sections). When there is no active run the digest still renders — it
    just reports "no active run", because the `SessionStart` handler decides
    whether to inject based on its own armed/unarmed check, and a degraded
    digest is more useful to a post-mortem than an empty string.

    Every section is independently fault-isolated: an exception while
    gathering plans, PRs, the branch, etc. degrades that one line to an
    "unavailable" note instead of aborting the whole render.
    """
    run = current_run(root)
    lines: list[str] = []
    lines.append("# Nightly session digest")
    lines.append("")

    # ── identity & counters ───────────────────────────────────────────
    run_id = run.id if run is not None else "(none)"
    lines.append(f"- **nightly** {__version__} · **run** `{run_id}`")
    if run is not None:
        lines.append(f"- {_safe(_render_counters, run.path, fallback='counters: unavailable')}")
        lines.append(
            f"- markers: {_safe(_render_markers, run.path, fallback='unavailable')}"
        )
    else:
        lines.append("- no active run (`.nightly/runs/CURRENT` absent).")

    # ── git branch ────────────────────────────────────────────────────
    lines.append(f"- branch: `{_render_branch(root)}`")

    # ── last cascade pick ─────────────────────────────────────────────
    if run is not None:
        lines.append(
            f"- last cascade pick: {_safe(_render_last_pick, run.path, fallback='unavailable')}"
        )

    # ── plans ─────────────────────────────────────────────────────────
    lines.append("")
    lines.append("## Active plans")
    lines.append("")
    lines.extend(_render_plans(root))

    # ── open PRs ──────────────────────────────────────────────────────
    lines.append("")
    lines.append("## Open Nightly PRs")
    lines.append("")
    lines.extend(_render_open_prs(root))

    # ── closing autonomy reminder ─────────────────────────────────────
    lines.append("")
    lines.append("---")
    lines.append(
        f"Autonomy contract: **{_AUTONOMY_ONE_LINER}.** Full state and audit "
        f"trail live under `.nightly/runs/{run_id}/`; the behavioral contract "
        "is in AGENTS.md / CLAUDE.md. Do not stop the session over context "
        "size — practice context hygiene and keep advancing."
    )
    return "\n".join(lines) + "\n"


def write_digest(root: Path | None = None) -> Path | None:
    """Render the digest and write it to `.nightly/runs/<id>/digest.md`.

    Returns the path written, or None when there is no active run (nowhere
    to write) or the write fails. Best-effort: OSError is suppressed so a
    full disk or a permissions glitch never crashes the caller (the
    keepalive hook calls this on every interval boundary).
    """
    run = current_run(root)
    if run is None:
        return None
    path = run.path / _DIGEST_FILENAME
    try:
        path.write_text(render_digest(root), encoding="utf-8")
    except OSError:
        return None
    return path


# ── section renderers (each fault-isolated by the caller's try/except) ─────


def _safe(fn: object, *args: object, fallback: str) -> str:
    """Call a one-line section renderer, returning `fallback` on any error.

    The digest's contract is that a failing sub-section degrades to a note
    rather than aborting the whole render, so this swallows *everything* —
    not just OSError — around the per-line renderers."""
    try:
        return fn(*args)  # type: ignore[operator]
    except Exception:
        return fallback


def _render_counters(run_path: Path) -> str:
    """One-line turn / chain-block counter summary, best-effort."""
    try:
        turns = _read_int(run_path / _TURN_COUNTER_FILE)
        blocks = _read_int(run_path / _BLOCK_COUNTER_FILE)
        return f"keepalive turns: {turns} · current chain blocks: {blocks}"
    except OSError:
        return "keepalive counters: unavailable"


def _render_markers(run_path: Path) -> str:
    """Render the present/absent state of every lifecycle marker."""
    try:
        present = [name for name in _MARKER_FILES if (run_path / name).is_file()]
        return ", ".join(present) if present else "(none present)"
    except OSError:
        return "unavailable"


def _render_branch(root: Path | None) -> str:
    """Current git branch via `git branch --show-current`, best-effort.

    Returns `(unknown)` on detached HEAD, missing git, or any subprocess
    failure — a digest must never raise just because git is unhappy.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # git missing, timeout, monkeypatched failure — never raise
        return "(unknown)"
    branch = (result.stdout or "").strip()
    return branch or "(detached or unknown)"


def _render_last_pick(run_path: Path) -> str:
    """Last line of keepalive.history — the most recent cascade fingerprint.

    Deliberately reads the persisted history rather than calling
    `next_task()`: the cascade walk is too slow and side-effectful for a
    digest render (it can spawn proposers / `gh`). The fingerprint shape is
    `source|target|summary` (see keepalive_hook._cascade_fingerprint)."""
    history = run_path / _HISTORY_FILE
    if not history.is_file():
        return "(no history yet)"
    try:
        lines = [ln for ln in history.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return "unavailable"
    return f"`{lines[-1]}`" if lines else "(no history yet)"


def _render_plans(root: Path | None) -> list[str]:
    """Active plans grouped in_progress → blocked → (done count).

    `ready` / `parked` plans are folded into neither group and only the
    done count is summarized — the digest's job is to show what is *live*,
    not to enumerate every historical task."""
    try:
        from nightly_core.plans import list_plans  # noqa: PLC0415 - lazy, hook hot path

        plans = list_plans(root)
    except Exception:  # any failure → one-line note, never raise
        return ["- _plans unavailable._"]

    in_progress = [p for p in plans if p.status == "in_progress"]
    blocked = [p for p in plans if p.status == "blocked: approval"]
    done_count = sum(1 for p in plans if p.status == "done")

    out: list[str] = []
    for plan in in_progress:
        out.append(f"- **in_progress** `{plan.slug}`")
    for plan in blocked:
        out.append(f"- **blocked** `{plan.slug}` (awaiting approval)")
    out.append(f"- done this/earlier runs: {done_count}")
    if not in_progress and not blocked:
        out.insert(0, "- _no in-progress or blocked plans._")
    return out


def _render_open_prs(root: Path | None) -> list[str]:
    """Open `nightly/*` PRs as `#<n> <branch>` lines, best-effort.

    Reuses the cascade's PR-listing helper so the digest reflects exactly
    what `pr_rescue` sees. Returns a single note line when `gh` is missing
    or the listing fails — never raises."""
    try:
        from nightly_core.cascade import open_nightly_pr_branches  # noqa: PLC0415 - lazy

        branches = open_nightly_pr_branches(root)
    except Exception:
        return ["- _PR listing unavailable (gh missing or errored)._"]
    if not branches:
        return ["- _none open._"]
    return [f"- #{num} `{branch}`" for branch, num, _url in branches]


def _read_int(path: Path) -> int:
    """Read a small integer counter file. 0 on absence / parse failure."""
    if not path.is_file():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
