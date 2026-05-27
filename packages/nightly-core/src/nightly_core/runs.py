"""Run lifecycle — create, get current, conclude, list.

A "run" is one Nightly session, identified by an ISO-8601 timestamp. Runs
live under `.nightly/runs/<id>/` and are pointed at by `.nightly/runs/CURRENT`.

Phase 2 ships the minimum needed for the interactive Skill loop:
- `start_run` creates the folder shape and updates CURRENT
- `current_run` reads CURRENT
- `conclude_run` writes the CONCLUDE marker (advance-never-block: the
  agent finishes the current task before exiting)
- `list_runs` returns all runs chronologically
- `new_task` creates a per-task folder in the current run

`.nightly/memory/` and `.nightly/atlas/` are scaffolded by `nightly init`
but not yet populated — rolling cross-session memory (e.g. `lessons.md`
carryover) and a Devin-style per-repo wiki refreshed on cold start are
documented in `.planning/brainstorm.html` §§"Memory is local" and "Steal
01 · Wiki-as-memory" and remain on the roadmap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nightly_core.paths import current_run_pointer, new_run_id, run_dir, runs_dir

__all__ = [
    "Run",
    "TaskDir",
    "conclude_run",
    "current_run",
    "list_runs",
    "new_task",
    "next_task_index",
    "slugify",
    "start_run",
]


# task slugs: lowercase alnum + dashes, ≤ 40 chars
_TASK_SLUG_RE = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class Run:
    """A Nightly run rooted at `.nightly/runs/<id>/`."""

    id: str
    path: Path
    is_concluded: bool


@dataclass(frozen=True)
class TaskDir:
    """A per-task subdirectory under a run: `<run>/tasks/<index>-<slug>/`."""

    index: int
    slug: str
    path: Path


def slugify(text: str) -> str:
    """Reduce `text` to a filesystem-safe task slug (lowercase, dashes, ≤ 40)."""
    lowered = text.strip().lower()
    cleaned = _TASK_SLUG_RE.sub("-", lowered).strip("-")
    return cleaned[:40] or "task"


def _ensure_run_layout(path: Path) -> None:
    """Create the standard subdirectory shape inside a run folder."""
    for sub in ("tasks", "proposed", "proposed/approvals", "proposed/planning"):
        (path / sub).mkdir(parents=True, exist_ok=True)


def start_run(
    root: Path | None = None,
    *,
    task: str | None = None,
    now: datetime | None = None,
) -> Run:
    """Create a new run, update `runs/CURRENT`, optionally seed the first task.

    If `task` is provided, the run also creates `tasks/0001-<slug>/plan.md`
    with a minimal placeholder pointing back to the seed description. The
    Skill picks it up from there.

    `now` is exposed for tests — production calls let it default to the
    real clock. A monotonically-increasing counter is appended when two
    runs happen in the same second so back-to-back calls never collide.
    """
    base_id = new_run_id(now)
    run_id = base_id
    suffix = 1
    while run_dir(run_id, root).exists():
        suffix += 1
        run_id = f"{base_id}-{suffix:02d}"
    path = run_dir(run_id, root)
    path.mkdir(parents=True, exist_ok=True)
    _ensure_run_layout(path)

    pointer = current_run_pointer(root)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(run_id + "\n", encoding="utf-8")

    run = Run(id=run_id, path=path, is_concluded=False)
    if task:
        new_task(run, slug=slugify(task), description=task)
    return run


def current_run(root: Path | None = None) -> Run | None:
    """Read `.nightly/runs/CURRENT`. Return None if no run is active."""
    pointer = current_run_pointer(root)
    if not pointer.is_file():
        return None
    run_id = pointer.read_text(encoding="utf-8").strip()
    if not run_id:
        return None
    path = run_dir(run_id, root)
    if not path.is_dir():
        return None
    return Run(id=run_id, path=path, is_concluded=(path / "CONCLUDE").is_file())


def conclude_run(root: Path | None = None) -> Run | None:
    """Write the CONCLUDE marker on the current run.

    Per the always-advance principle, this is non-blocking: the marker
    signals the agent to drain (finish the current task, no new work). The
    agent reads CONCLUDE between iterations and transitions to nudge / hard
    phases on its own.
    """
    run = current_run(root)
    if run is None:
        return None
    (run.path / "CONCLUDE").write_text("", encoding="utf-8")
    return Run(id=run.id, path=run.path, is_concluded=True)


def list_runs(root: Path | None = None) -> list[Run]:
    """List all runs (chronological order)."""
    runs_root = runs_dir(root)
    if not runs_root.is_dir():
        return []
    out: list[Run] = []
    for entry in sorted(runs_root.iterdir()):
        # CURRENT is a file, not a run directory
        if not entry.is_dir():
            continue
        out.append(
            Run(
                id=entry.name,
                path=entry,
                is_concluded=(entry / "CONCLUDE").is_file(),
            )
        )
    return out


def next_task_index(run: Run) -> int:
    """Return the next 1-based task index for `run`."""
    tasks = run.path / "tasks"
    if not tasks.is_dir():
        return 1
    highest = 0
    for entry in tasks.iterdir():
        if not entry.is_dir():
            continue
        head = entry.name.split("-", 1)[0]
        try:
            highest = max(highest, int(head))
        except ValueError:
            continue
    return highest + 1


def new_task(run: Run, *, slug: str, description: str | None = None) -> TaskDir:
    """Create `tasks/<NNNN>-<slug>/` inside `run` and seed `plan.md`.

    Returns the new `TaskDir`. Idempotent on slug collision: a slug already
    present in the run reuses its existing folder rather than creating a
    duplicate.
    """
    safe_slug = slugify(slug)
    tasks = run.path / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)

    # If the slug already exists, return its folder.
    for entry in tasks.iterdir():
        if entry.is_dir() and entry.name.endswith(f"-{safe_slug}"):
            try:
                idx = int(entry.name.split("-", 1)[0])
            except ValueError:
                continue
            return TaskDir(index=idx, slug=safe_slug, path=entry)

    index = next_task_index(run)
    name = f"{index:04d}-{safe_slug}"
    path = tasks / name
    path.mkdir(parents=True, exist_ok=True)

    plan = path / "plan.md"
    if not plan.exists():
        # Avoid importing nightly_core.plans at module load to keep startup
        # cheap and side-effect-free; the lazy import is fine for one-off
        # plan creation.
        from nightly_core.plans import render_frontmatter  # noqa: PLC0415

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata = {
            "status": "ready",
            "slug": f"{index:04d}-{safe_slug}",
            "task_number": str(index),
            "created": now,
            "updated": now,
        }
        body = (
            f"# Task {index:04d} — {safe_slug}\n\n"
            "_Plan seeded by `nightly start` / `nightly task`. The "
            "Nightly skill in Claude Code is expected to flesh this out._\n\n"
            "## Source\n\n"
            f"{description or '(no description supplied)'}\n\n"
            "## Success criteria\n\n_TODO_\n\n"
            "## File scope\n\n_TODO — declare which files this task may "
            "touch. Edits outside this list trigger refusal-policy scope "
            "creep._\n\n"
            "## Known risks\n\n_TODO_\n"
        )
        plan.write_text(render_frontmatter(metadata, body), encoding="utf-8")

    return TaskDir(index=index, slug=safe_slug, path=path)
