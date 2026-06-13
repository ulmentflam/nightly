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

Future extensions documented in the brainstorm: event-log replay,
`.nightly/atlas/` deltas, and richer per-task metadata. The current
template degrades cleanly when those slots are absent.
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
    issues_by_strategic_category: list[dict[str, Any]]
    """RFC 009 §B4 — `issues` grouped by strategic category for the
    template's category-headed rendering. Categories with no items
    are omitted; ordering follows the operator-stated priority
    sequence (cleaning → refactoring → housekeeping → convenience →
    capability → static_analysis). Identical content to `issues`,
    just bucketed."""
    ready_count: int
    generated_at: str
    session_narrative: Markup | None
    lessons: Markup | None
    stacked_geometry: list[dict[str, Any]]
    """RFC 001 §B2 — open Nightly PRs HEAD currently coincides with. Empty
    when HEAD is `main` or a non-`nightly/` branch. Each entry is a dict
    with `number`, `branch`, `url` so the template can render a panel."""
    current_branch: str
    """The branch HEAD points at, for the geometry panel header. Empty
    string if git wasn't reachable."""
    compacted: str | None = None
    """RFC 006 §B2 — "yes" if session compaction fired, "no" if it did not,
    None if default omitted (e.g. keepalive.log absent)."""


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
                "strategic_category": metadata.get("strategic_category", "housekeeping"),
                "score": metadata.get("score", "0.000"),
                "auto_pr_eligible": metadata.get("auto_pr_eligible", "false") == "true",
                "estimated_loc": metadata.get("estimated_loc", "0"),
                "title": _extract_title(body) or entry.stem,
            }
        )
    return out


def _group_issues_by_strategic_category(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """RFC 009 §B4 — group `proposed/issues/` by strategic category for
    the briefing's "Proposed issues" section.

    Synthesis proposals (proposer `synthesis`) and Phase-5 narrow
    proposals (todo_fixme / lint_debt / type_holes) are both grouped
    here; the template renders synthesis output under the five
    category sub-headers and the narrow output under a
    "Static-analysis hits" sub-section.

    Returns an ordered list of `{strategic_category, label, issues}`
    dicts; categories with no issues are omitted from the output.
    The list order is the operator-stated priority (cleaning →
    refactoring → housekeeping → convenience → capability) plus a
    final `"static_analysis"` pseudo-category for non-synthesis
    proposals that share `strategic_category="housekeeping"` but are
    deterministic nits rather than synthesized strategy.
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        "cleaning": [],
        "refactoring": [],
        "housekeeping": [],
        "convenience": [],
        "capability": [],
        "static_analysis": [],
    }
    for issue in issues:
        proposer = issue.get("proposer", "")
        strategic = issue.get("strategic_category", "housekeeping")
        if proposer != "synthesis":
            # The three Phase-5 narrow proposers collapse into their own
            # sub-section to keep linter nits visually separate from the
            # synthesis layer's strategic recommendations.
            buckets["static_analysis"].append(issue)
            continue
        if strategic in buckets:
            buckets[strategic].append(issue)
        else:
            buckets["housekeeping"].append(issue)
    labels = {
        "cleaning": "Cleaning",
        "refactoring": "Refactoring",
        "housekeeping": "Housekeeping",
        "convenience": "Convenience",
        "capability": "Capability",
        "static_analysis": "Static-analysis hits",
    }
    return [
        {"strategic_category": cat, "label": labels[cat], "issues": items}
        for cat, items in buckets.items()
        if items
    ]


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


def _load_stacked_geometry() -> tuple[str, list[dict[str, Any]]]:
    """RFC 001 §B2 — detect open Nightly PRs HEAD stacks on.

    Returns `(current_branch, [{number, branch, url, declared}, ...])`.
    `declared` (RFC 004 §C) is `True` when the current branch's plan
    declared this PR via `depends_on_pr` — the briefing renderer picks
    a teal border for an all-declared chain and rose otherwise. Wrapped
    in try/except so a broken cascade import (e.g. in test fixtures
    that monkeypatch cascade) never crashes the briefing — the panel
    just degrades to "empty"."""
    try:
        from nightly_core.cascade import detect_stacked_geometry  # noqa: PLC0415 - lazy

        geo = detect_stacked_geometry()
    except Exception:
        return "", []
    chain = [{"number": n, "branch": b, "url": u, "declared": d} for n, b, u, d in geo.chain]
    return geo.current_branch, chain


def build_context(run: Run, *, now: datetime | None = None) -> BriefingContext:
    """Walk the run directory and build the renderer context."""
    tasks = _load_tasks(run)
    approvals = _load_approvals(run)
    planning = _load_planning(run)
    issues = _load_issues(run)
    issues_by_strategic_category = _group_issues_by_strategic_category(issues)
    ready = sum(1 for t in tasks if _is_ready(t))
    generated = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    current_branch, stacked = _load_stacked_geometry()

    compacted: str | None = None
    log_path = run.path / "keepalive.log"
    if log_path.is_file():
        try:
            log_content = log_path.read_text(encoding="utf-8")
            compacted = "yes" if "digest_reinject" in log_content else "no"
        except OSError:
            pass

    return BriefingContext(
        run_id=run.id,
        is_concluded=run.is_concluded,
        tasks=tasks,
        approvals=approvals,
        planning=planning,
        issues=issues,
        issues_by_strategic_category=issues_by_strategic_category,
        ready_count=ready,
        generated_at=generated,
        session_narrative=_render_markdown_file(run.path / "briefing.md"),
        lessons=_render_markdown_file(run.path / "lessons.md"),
        stacked_geometry=stacked,
        current_branch=current_branch,
        compacted=compacted,
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
        issues_by_strategic_category=ctx.issues_by_strategic_category,
        ready_count=ctx.ready_count,
        generated_at=ctx.generated_at,
        session_narrative=ctx.session_narrative,
        lessons=ctx.lessons,
        stacked_geometry=ctx.stacked_geometry,
        current_branch=ctx.current_branch,
        compacted=ctx.compacted,
    )


def write_briefing(run: Run, *, now: datetime | None = None) -> Path:
    """Render briefing.html into `<run>/briefing.html`; return the path."""
    target = run.path / "briefing.html"
    target.write_text(render_briefing(run, now=now), encoding="utf-8")
    return target
