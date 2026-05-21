"""Tests for nightly_core.plans."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightly_core.plans import (
    PLAN_STATUSES,
    PlanRecord,
    list_plans,
    parse_frontmatter,
    read_plan,
    render_frontmatter,
    update_plan_status,
)
from nightly_core.runs import new_task, start_run

# ── parse_frontmatter ──────────────────────────────────────────────────────


def test_parse_frontmatter_empty_returns_no_metadata() -> None:
    metadata, body = parse_frontmatter("# Hello\n\nNo frontmatter here.")
    assert metadata == {}
    assert body == "# Hello\n\nNo frontmatter here."


def test_parse_frontmatter_handles_basic_fields() -> None:
    text = "---\nstatus: ready\nslug: 0001-add-retry\n---\n# Plan\n"
    metadata, body = parse_frontmatter(text)
    assert metadata == {"status": "ready", "slug": "0001-add-retry"}
    assert body == "# Plan\n"


def test_parse_frontmatter_preserves_colons_in_values() -> None:
    """Statuses like `blocked: approval` and ISO timestamps contain colons."""
    text = "---\nstatus: blocked: approval\ncreated: 2026-05-20T22:14:03Z\n---\n\nbody"
    metadata, _ = parse_frontmatter(text)
    assert metadata["status"] == "blocked: approval"
    assert metadata["created"] == "2026-05-20T22:14:03Z"


def test_parse_frontmatter_missing_closing_fence_returns_no_metadata() -> None:
    text = "---\nstatus: ready\n\n# no closing fence\n"
    metadata, body = parse_frontmatter(text)
    assert metadata == {}
    assert body == text


def test_parse_frontmatter_skips_lines_without_colon() -> None:
    text = "---\nstatus: ready\njust a comment\n---\nbody"
    metadata, _ = parse_frontmatter(text)
    assert metadata == {"status": "ready"}


# ── render_frontmatter (round-trip) ───────────────────────────────────────


def test_render_frontmatter_round_trips() -> None:
    original = {"status": "in_progress", "slug": "0002-tighten"}
    body = "# Plan\n\nText.\n"
    text = render_frontmatter(original, body)
    parsed, parsed_body = parse_frontmatter(text)
    assert parsed == original
    assert parsed_body == body


# ── read_plan ─────────────────────────────────────────────────────────────


def test_read_plan_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_plan(tmp_path / "no-such.md")


def test_read_plan_returns_record(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "---\nstatus: in_progress\nslug: 0001-alpha\n---\n# Body\n",
        encoding="utf-8",
    )
    record = read_plan(plan)
    assert isinstance(record, PlanRecord)
    assert record.metadata["slug"] == "0001-alpha"
    assert record.status == "in_progress"
    assert record.body == "# Body\n"


def test_read_plan_without_frontmatter_defaults_to_ready(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Bare plan\n", encoding="utf-8")
    record = read_plan(plan)
    assert record.status == "ready"
    assert record.metadata == {}


def test_plan_status_unrecognized_falls_back_to_ready(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("---\nstatus: weird\n---\nbody", encoding="utf-8")
    assert read_plan(plan).status == "ready"


# ── update_plan_status ────────────────────────────────────────────────────


def test_update_plan_status_rewrites_and_preserves_body(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "---\nstatus: ready\nslug: 0001-alpha\n---\n# Body keep\n",
        encoding="utf-8",
    )
    updated = update_plan_status(plan, "in_progress")
    assert updated.status == "in_progress"
    assert updated.metadata["slug"] == "0001-alpha"  # preserved
    assert updated.body == "# Body keep\n"
    assert "updated" in updated.metadata


def test_update_plan_status_sets_approval_granted(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("---\nstatus: blocked: approval\n---\nbody", encoding="utf-8")
    updated = update_plan_status(plan, "blocked: approval", approval_granted=True)
    assert updated.approval_granted is True
    assert updated.metadata["approval_granted"] == "true"


def test_update_plan_status_accepts_all_canonical_statuses(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("---\nstatus: ready\n---\nbody", encoding="utf-8")
    for status in PLAN_STATUSES:
        updated = update_plan_status(plan, status)
        assert updated.status == status


# ── list_plans across runs ────────────────────────────────────────────────


def test_list_plans_empty(tmp_path: Path) -> None:
    assert list_plans(tmp_path) == []


def test_list_plans_returns_all_plans_in_chronological_order(tmp_path: Path) -> None:
    run_a = start_run(tmp_path)
    new_task(run_a, slug="alpha")
    new_task(run_a, slug="beta")
    # ensure second run sorts after first
    run_b = start_run(tmp_path)
    new_task(run_b, slug="gamma")

    plans = list_plans(tmp_path)
    assert len(plans) == 3
    assert [p.run_id for p in plans] == [run_a.id, run_a.id, run_b.id]
    assert [p.path.parent.name for p in plans] == [
        "0001-alpha",
        "0002-beta",
        "0001-gamma",
    ]
    # All seeded plans start `ready`
    assert all(p.status == "ready" for p in plans)


def test_new_task_writes_frontmatter(tmp_path: Path) -> None:
    """Phase 3: every plan seeded by new_task has YAML frontmatter."""
    run = start_run(tmp_path)
    task = new_task(run, slug="add-retry", description="Add retry budget")
    plan = read_plan(task.path / "plan.md")
    assert plan.status == "ready"
    assert plan.metadata["slug"] == "0001-add-retry"
    assert plan.metadata["task_number"] == "1"
    assert "created" in plan.metadata
    assert "updated" in plan.metadata
    # Body still contains the human-readable sections
    assert "Add retry budget" in plan.body
    assert "Success criteria" in plan.body
