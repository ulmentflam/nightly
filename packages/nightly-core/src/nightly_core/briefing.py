"""Render `briefing.html` for a Nightly run.

The renderer is hybrid by design: the Python side owns the *structural*
skeleton (hero counts, task pills, approvals list, planning proposals —
facts derived from disk) and the *agent* writes the editorial pieces as
markdown files on disk:

- `.nightly/runs/<id>/briefing.md`           — session-level narrative
- `.nightly/runs/<id>/tasks/<n>-<slug>/notes.md` — per-task narrative
- `.nightly/runs/<id>/lessons.md`            — lessons to carry forward

The template embeds those narrative slots as rendered HTML when present,
and degrades to placeholder copy when absent. The agent never authors the
HTML directly — that's the Python template's job — so the briefing always
has a consistent shape even if the agent's context compacts at drain.

Phase 3+ extends with event-log replay, atlas deltas, and richer task
metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from nightly_core.runs import Run

__all__ = [
    "BriefingContext",
    "build_context",
    "render_briefing",
    "write_briefing",
]


_ENV = Environment(
    loader=PackageLoader("nightly_core", "templates"),
    autoescape=select_autoescape(["html", "j2"]),
    keep_trailing_newline=True,
)

# CommonMark with raw-HTML pass-through disabled — agent-authored markdown
# is trusted to *describe* the run, not to inject arbitrary HTML.
_MD = MarkdownIt("commonmark", {"html": False, "breaks": False})


def _render_markdown_file(path: Path) -> Markup | None:
    """Render `path` as CommonMark to HTML, or return None if absent/empty.

    Returns `Markup` so Jinja's autoescape leaves it alone (the markdown
    rendering has already produced safe HTML). Raw `<script>` / `<style>` in
    the markdown source is escaped, not passed through.
    """
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return Markup(_MD.render(text))


@dataclass(frozen=True)
class BriefingContext:
    """The flattened view of a run that the template consumes."""

    run_id: str
    is_concluded: bool
    tasks: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    planning: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    ready_count: int
    generated_at: str
    session_narrative: Markup | None
    lessons: Markup | None


def _load_tasks(run: Run) -> list[dict[str, Any]]:
    tasks_dir = run.path / "tasks"
    if not tasks_dir.is_dir():
        return []
    tasks: list[dict[str, Any]] = []
    for entry in sorted(tasks_dir.iterdir()):
        if not entry.is_dir():
            continue
        tasks.append(
            {
                "slug": entry.name,
                "has_plan": (entry / "plan.md").is_file(),
                "has_proposal": (entry / "proposal.md").is_file(),
                "has_uncertainty": (entry / "uncertainty.md").is_file(),
                "notes": _render_markdown_file(entry / "notes.md"),
            }
        )
    return tasks


def _load_approvals(run: Run) -> list[dict[str, Any]]:
    approvals_dir = run.path / "proposed" / "approvals"
    if not approvals_dir.is_dir():
        return []
    return [{"id": entry.stem, "path": str(entry)} for entry in sorted(approvals_dir.glob("*.md"))]


def _load_planning(run: Run) -> list[dict[str, Any]]:
    planning_dir = run.path / "proposed" / "planning"
    if not planning_dir.is_dir():
        return []
    return [{"id": entry.stem, "path": str(entry)} for entry in sorted(planning_dir.glob("*.md"))]


def _load_issues(run: Run) -> list[dict[str, Any]]:
    """Read proposed/issues/<NNN>-<slug>.md drafts written by `nightly ideate`.

    Each draft has YAML frontmatter (proposer, category, score,
    auto_pr_eligible, etc.) that we expose to the template so the briefing
    can show category badges and the autonomy verdict.
    """
    from nightly_core.plans import parse_frontmatter  # noqa: PLC0415 - lazy

    issues_dir = run.path / "proposed" / "issues"
    if not issues_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(issues_dir.glob("[0-9][0-9][0-9]-*.md")):
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        metadata, body = parse_frontmatter(text)
        out.append(
            {
                "id": entry.stem,
                "path": str(entry),
                "proposer": metadata.get("proposer", "?"),
                "category": metadata.get("category", "?"),
                "score": metadata.get("score", "0.000"),
                "auto_pr_eligible": metadata.get("auto_pr_eligible", "false") == "true",
                "estimated_loc": metadata.get("estimated_loc", "0"),
                "title": _extract_title(body) or entry.stem,
            }
        )
    return out


def _extract_title(body: str) -> str | None:
    """Pull the first H1 from a proposal body, if present."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def _is_ready(task: dict[str, Any]) -> bool:
    """A task counts as 'ready' once plan + proposal + uncertainty exist."""
    return bool(task["has_plan"] and task["has_proposal"] and task["has_uncertainty"])


def build_context(run: Run, *, now: datetime | None = None) -> BriefingContext:
    """Walk the run directory and build the renderer context."""
    tasks = _load_tasks(run)
    approvals = _load_approvals(run)
    planning = _load_planning(run)
    issues = _load_issues(run)
    ready = sum(1 for t in tasks if _is_ready(t))
    generated = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    return BriefingContext(
        run_id=run.id,
        is_concluded=run.is_concluded,
        tasks=tasks,
        approvals=approvals,
        planning=planning,
        issues=issues,
        ready_count=ready,
        generated_at=generated,
        session_narrative=_render_markdown_file(run.path / "briefing.md"),
        lessons=_render_markdown_file(run.path / "lessons.md"),
    )


def render_briefing(run: Run, *, now: datetime | None = None) -> str:
    """Return rendered briefing HTML for `run`."""
    ctx = build_context(run, now=now)
    template = _ENV.get_template("briefing.html.j2")
    return template.render(
        run_id=ctx.run_id,
        is_concluded=ctx.is_concluded,
        tasks=ctx.tasks,
        approvals=ctx.approvals,
        planning=ctx.planning,
        issues=ctx.issues,
        ready_count=ctx.ready_count,
        generated_at=ctx.generated_at,
        session_narrative=ctx.session_narrative,
        lessons=ctx.lessons,
    )


def write_briefing(run: Run, *, now: datetime | None = None) -> Path:
    """Render briefing.html into `<run>/briefing.html`; return the path."""
    target = run.path / "briefing.html"
    target.write_text(render_briefing(run, now=now), encoding="utf-8")
    return target
