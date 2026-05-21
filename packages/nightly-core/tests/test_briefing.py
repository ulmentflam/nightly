"""Tests for nightly_core.briefing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from nightly_core.briefing import build_context, render_briefing, write_briefing
from nightly_core.runs import conclude_run, new_task, start_run


def test_build_context_empty_run(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    ctx = build_context(run, now=datetime(2026, 5, 20, 22, 14, tzinfo=UTC))
    assert ctx.run_id == run.id
    assert ctx.is_concluded is False
    assert ctx.tasks == []
    assert ctx.approvals == []
    assert ctx.planning == []
    assert ctx.ready_count == 0
    assert "2026-05-20" in ctx.generated_at


def test_build_context_counts_ready_tasks(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    # task 1 — fully ready
    task1 = new_task(run, slug="alpha")
    (task1.path / "proposal.md").write_text("# proposal", encoding="utf-8")
    (task1.path / "uncertainty.md").write_text("# uncertainty", encoding="utf-8")
    # task 2 — missing proposal
    task2 = new_task(run, slug="beta")
    (task2.path / "uncertainty.md").write_text("# uncertainty", encoding="utf-8")

    ctx = build_context(run)
    assert len(ctx.tasks) == 2
    assert ctx.ready_count == 1


def test_build_context_lists_approvals_and_planning(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    (run.path / "proposed" / "approvals" / "001-prod-deploy.md").write_text("...")
    (run.path / "proposed" / "approvals" / "002-secret-edit.md").write_text("...")
    (run.path / "proposed" / "planning" / "rfc-multi-host.md").write_text("...")

    ctx = build_context(run)
    assert [a["id"] for a in ctx.approvals] == ["001-prod-deploy", "002-secret-edit"]
    assert [p["id"] for p in ctx.planning] == ["rfc-multi-host"]


def test_render_briefing_returns_html(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    html = render_briefing(run)
    assert html.startswith("<!doctype html>")
    assert run.id in html
    assert "Nightly" in html


def test_render_briefing_marks_concluded(tmp_path: Path) -> None:
    start_run(tmp_path)
    concluded = conclude_run(tmp_path)
    assert concluded is not None
    html = render_briefing(concluded)
    assert "concluded" in html


def test_render_briefing_lists_task_pills(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="add-retry")
    (task.path / "proposal.md").write_text("...", encoding="utf-8")
    (task.path / "uncertainty.md").write_text("...", encoding="utf-8")
    html = render_briefing(run)
    assert "0001-add-retry" in html
    # all three pills present and 'ok' for this fully-ready task
    assert html.count("pill ok") >= 3


def test_write_briefing_creates_file_under_run(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    target = write_briefing(run)
    assert target == run.path / "briefing.html"
    assert target.is_file()
    content = target.read_text(encoding="utf-8")
    assert run.id in content


def test_render_briefing_escapes_run_id(tmp_path: Path) -> None:
    """Defense-in-depth: the template uses autoescape, so HTML-shaped data
    in any context field must come out escaped."""
    run = start_run(tmp_path)
    (run.path / "proposed" / "approvals" / "<script>.md").write_text("x")
    html = render_briefing(run)
    assert "<script>.md" not in html
    assert "&lt;script&gt;" in html or "&lt;script&gt;.md" in html


# ── hybrid narrative slots ──────────────────────────────────────────────


def test_session_narrative_renders_when_present(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    (run.path / "briefing.md").write_text(
        "## what I tried\n\nFixed the **flaky** auth retry.\n\n"
        "- Touched: `auth/client.py`\n"
        "- Tests added: 3\n",
        encoding="utf-8",
    )
    html = render_briefing(run)
    assert "session narrative" in html
    assert "what I tried" in html
    assert "<strong>flaky</strong>" in html
    assert "<code>auth/client.py</code>" in html
    # placeholder copy is gone when real narrative is present
    assert "No <code>briefing.md</code> on disk" not in html


def test_session_narrative_falls_back_to_placeholder(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    html = render_briefing(run)
    assert "session narrative" in html  # the section is always rendered
    assert "No <code>briefing.md</code> on disk" in html


def test_empty_briefing_md_treated_as_missing(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    (run.path / "briefing.md").write_text("   \n\n   ", encoding="utf-8")
    html = render_briefing(run)
    assert "No <code>briefing.md</code> on disk" in html


def test_session_narrative_does_not_pass_through_raw_html(tmp_path: Path) -> None:
    """markdown-it-py is configured with html=False — raw script tags in the
    markdown source must be escaped, not embedded."""
    run = start_run(tmp_path)
    (run.path / "briefing.md").write_text(
        "Look at this:\n\n<script>alert(1)</script>\n",
        encoding="utf-8",
    )
    html = render_briefing(run)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_task_notes_render_inline(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    task = new_task(run, slug="add-retry")
    (task.path / "notes.md").write_text(
        "I picked exponential backoff because the upstream service _hates_ tight loops.",
        encoding="utf-8",
    )
    html = render_briefing(run)
    assert "show notes" in html
    assert "<em>hates</em>" in html
    assert "exponential backoff" in html
    # notes pill appears when notes.md exists
    assert "pill notes" in html


def test_task_without_notes_omits_details(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    new_task(run, slug="alpha")
    html = render_briefing(run)
    assert "show notes" not in html
    # task row still renders
    assert "0001-alpha" in html


def test_lessons_render_when_present(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    (run.path / "lessons.md").write_text(
        "- Always run the test suite before declaring done.\n"
        "- The auth client retries are quieter than they look.\n",
        encoding="utf-8",
    )
    html = render_briefing(run)
    assert "lessons" in html.lower()
    assert "Always run the test suite" in html
    assert "narrative lessons" in html  # the lessons section's CSS class


def test_lessons_absent_section_not_rendered(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    html = render_briefing(run)
    # the lessons SECTION is omitted entirely when no lessons.md exists
    assert "narrative lessons" not in html


def test_full_narrative_pass_all_three_slots(tmp_path: Path) -> None:
    """When all three slots are populated, the briefing has session
    narrative, per-task notes, and lessons all visible."""
    run = start_run(tmp_path)
    (run.path / "briefing.md").write_text(
        "## summary\n\nThree tasks landed; one stashed.\n", encoding="utf-8"
    )
    (run.path / "lessons.md").write_text(
        "- Watch for the migration retry path.\n", encoding="utf-8"
    )
    task = new_task(run, slug="alpha")
    (task.path / "notes.md").write_text("Used a feature flag for safety.", encoding="utf-8")

    html = render_briefing(run)
    assert "Three tasks landed" in html
    assert "Used a feature flag" in html
    assert "Watch for the migration retry path" in html


def test_build_context_exposes_narrative_fields(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    (run.path / "briefing.md").write_text("hello world", encoding="utf-8")
    (run.path / "lessons.md").write_text("be careful", encoding="utf-8")
    ctx = build_context(run)
    assert ctx.session_narrative is not None
    assert "hello world" in str(ctx.session_narrative)
    assert ctx.lessons is not None
    assert "be careful" in str(ctx.lessons)


# ── Phase 5: ideated issues section ───────────────────────────────────────


def _write_issue_draft(
    run,  # type: ignore[no-untyped-def]
    *,
    rank: int,
    slug: str,
    title: str,
    proposer: str = "lint_debt",
    category: str = "lint_debt",
    score: float = 3.0,
    auto_pr_eligible: bool = True,
) -> None:
    issues_dir = run.path / "proposed" / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    metadata = "\n".join(
        [
            f"proposer: {proposer}",
            f"category: {category}",
            f"score: {score:.3f}",
            f"auto_pr_eligible: {'true' if auto_pr_eligible else 'false'}",
            "estimated_loc: 4",
            "file_scope: src/a.py",
        ]
    )
    path = issues_dir / f"{rank:03d}-{slug}.md"
    path.write_text(f"---\n{metadata}\n---\n\n# {title}\n\nbody\n", encoding="utf-8")


def test_briefing_renders_proposed_issues_when_present(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    _write_issue_draft(run, rank=1, slug="apply-f401", title="Apply F401 cleanup", score=4.5)
    _write_issue_draft(
        run,
        rank=2,
        slug="audit-todos",
        title="Audit 7 TODOs across 3 files",
        proposer="todo_fixme",
        category="todo_audit",
        score=2.1,
        auto_pr_eligible=False,
    )

    html = render_briefing(run)
    assert "Proposed issues" in html
    assert "Apply F401 cleanup" in html
    assert "Audit 7 TODOs across 3 files" in html
    # auto-PR-eligible row gets the ok pill; the human-review one gets missing
    assert "auto-PR eligible" in html
    assert "human review" in html
    # The two glance counts reflect the issue count
    assert html.count("ideated issues") >= 1


def test_briefing_renders_placeholder_when_no_issues(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    html = render_briefing(run)
    assert "No issues ideated this run" in html


def test_briefing_skips_non_numbered_issue_files(tmp_path: Path) -> None:
    """Files like `human-notes.md` (no NNN- prefix) are author scratch
    and should not appear in the proposed-issues list."""
    run = start_run(tmp_path)
    issues_dir = run.path / "proposed" / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "human-notes.md").write_text("# Just a human note\n", encoding="utf-8")
    html = render_briefing(run)
    assert "Just a human note" not in html
    assert "No issues ideated this run" in html


def test_build_context_loads_issue_metadata(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    _write_issue_draft(
        run,
        rank=1,
        slug="apply-f401",
        title="Apply F401 cleanup",
        score=4.5,
    )
    ctx = build_context(run)
    assert len(ctx.issues) == 1
    issue = ctx.issues[0]
    assert issue["title"] == "Apply F401 cleanup"
    assert issue["proposer"] == "lint_debt"
    assert issue["category"] == "lint_debt"
    assert issue["auto_pr_eligible"] is True
    assert issue["score"] == "4.500"
