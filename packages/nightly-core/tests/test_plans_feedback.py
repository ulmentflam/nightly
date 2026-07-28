"""`append_pr_feedback` — the pr_rescue write path into a plan.

Cascade slot 5 reads PR feedback and this writes it into `plan.md`. It
was the largest uncovered block in `plans.py` (74% before these tests),
which is uncomfortable for a function that *mutates the operator's task
state*: get the round numbering wrong and rounds overwrite each other,
get the frontmatter wrong and the cascade loses the plan's status.

Feedback objects are duck-typed by the function (`getattr(f,
"is_blocking", False)`, `f.author_is_bot`, ...), so the fake below only
needs those attributes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nightly_core.plans import PR_LAST_RECONCILED_KEY, append_pr_feedback, read_plan

FIXED = datetime(2026, 7, 28, 21, 30, tzinfo=UTC)

PLAN = """\
---
status: in_progress
slug: 0001-example
---

# Task

Original body text.
"""


@dataclass
class FakeFeedback:
    author_login: str = "reviewer"
    author_is_bot: bool = False
    is_blocking: bool = False
    kind: str = "review"
    state: str = "CHANGES_REQUESTED"
    body: str = "please fix"
    url: str = "https://example.test/1"
    file_ref: str | None = None
    line_ref: int | None = None


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    p = tmp_path / "plan.md"
    p.write_text(PLAN, encoding="utf-8")
    return p


# ── round numbering ───────────────────────────────────────────────────────


def test_first_call_writes_round_one(plan_path: Path) -> None:
    append_pr_feedback(plan_path, [FakeFeedback()], now=FIXED)
    assert "## Feedback round 1" in plan_path.read_text(encoding="utf-8")


def test_second_call_increments_rather_than_overwriting(plan_path: Path) -> None:
    """Rounds accumulate. Reusing round 1 would silently destroy the
    previous reviewer's comments."""
    append_pr_feedback(plan_path, [FakeFeedback(body="first")], now=FIXED)
    append_pr_feedback(plan_path, [FakeFeedback(body="second")], now=FIXED)
    text = plan_path.read_text(encoding="utf-8")
    assert "## Feedback round 1" in text
    assert "## Feedback round 2" in text
    assert "first" in text
    assert "second" in text


# ── state preservation ────────────────────────────────────────────────────


def test_frontmatter_and_body_survive(plan_path: Path) -> None:
    """Losing `status` would drop the plan out of the cascade entirely."""
    append_pr_feedback(plan_path, [FakeFeedback()], now=FIXED)
    plan = read_plan(plan_path)
    assert plan.metadata["status"] == "in_progress"
    assert plan.metadata["slug"] == "0001-example"
    assert "Original body text." in plan.body


def test_reconciliation_timestamp_is_stamped(plan_path: Path) -> None:
    """`pick_pr_rescue` skips plans whose PR has had no feedback since
    this stamp — without it the same feedback is re-applied forever."""
    append_pr_feedback(plan_path, [FakeFeedback()], now=FIXED)
    plan = read_plan(plan_path)
    assert plan.metadata[PR_LAST_RECONCILED_KEY] == "2026-07-28T21:30:00Z"
    assert plan.metadata["updated"] == plan.metadata[PR_LAST_RECONCILED_KEY]


def test_returned_record_matches_what_was_written(plan_path: Path) -> None:
    returned = append_pr_feedback(plan_path, [FakeFeedback()], now=FIXED)
    assert returned.metadata == read_plan(plan_path).metadata


# ── grouping ──────────────────────────────────────────────────────────────


def test_groups_render_blocking_then_humans_then_bots(plan_path: Path) -> None:
    """Order is the point: a blocking review is why the agent was routed
    here, so it must not be buried under bot chatter."""
    append_pr_feedback(
        plan_path,
        [
            FakeFeedback(author_login="botty", author_is_bot=True, body="nit"),
            FakeFeedback(author_login="human", body="thought"),
            FakeFeedback(
                author_login="ci", is_blocking=True, kind="check_failure", state="FAILURE"
            ),
        ],
        now=FIXED,
    )
    text = plan_path.read_text(encoding="utf-8")
    assert text.index("### Blocking") < text.index("### Human reviewers")
    assert text.index("### Human reviewers") < text.index("### Bot reviewers")


def test_empty_groups_are_omitted(plan_path: Path) -> None:
    append_pr_feedback(plan_path, [FakeFeedback(author_login="human")], now=FIXED)
    text = plan_path.read_text(encoding="utf-8")
    assert "### Human reviewers" in text
    assert "### Blocking" not in text
    assert "### Bot reviewers" not in text


def test_no_feedback_says_so_explicitly(plan_path: Path) -> None:
    """An empty section with no explanation reads as a rendering bug."""
    append_pr_feedback(plan_path, [], now=FIXED)
    assert "_(no feedback returned)_" in plan_path.read_text(encoding="utf-8")


# ── per-item rendering ────────────────────────────────────────────────────


def test_check_failure_renders_its_state(plan_path: Path) -> None:
    append_pr_feedback(
        plan_path,
        [FakeFeedback(kind="check_failure", state="FAILURE", author_login="ci")],
        now=FIXED,
    )
    assert "(check: FAILURE)" in plan_path.read_text(encoding="utf-8")


def test_review_state_is_lowercased(plan_path: Path) -> None:
    append_pr_feedback(plan_path, [FakeFeedback(state="APPROVED")], now=FIXED)
    assert "(approved)" in plan_path.read_text(encoding="utf-8")


def test_file_and_line_render_as_a_locator(plan_path: Path) -> None:
    append_pr_feedback(plan_path, [FakeFeedback(file_ref="src/a.py", line_ref=42)], now=FIXED)
    assert "on `src/a.py:42`" in plan_path.read_text(encoding="utf-8")


def test_file_without_line_omits_the_colon(plan_path: Path) -> None:
    append_pr_feedback(plan_path, [FakeFeedback(file_ref="src/a.py")], now=FIXED)
    text = plan_path.read_text(encoding="utf-8")
    assert "on `src/a.py`" in text
    assert "src/a.py:" not in text


def test_multiline_body_is_quoted_on_every_line(plan_path: Path) -> None:
    """A half-quoted body breaks the markdown blockquote and the rest of
    the comment renders as plan prose."""
    append_pr_feedback(plan_path, [FakeFeedback(body="line one\nline two")], now=FIXED)
    text = plan_path.read_text(encoding="utf-8")
    assert "  > line one" in text
    assert "  > line two" in text


def test_empty_body_still_emits_a_quote_line(plan_path: Path) -> None:
    """`"".splitlines()` is `[]`, so the `or [""]` fallback is what stops
    the entry rendering as a bare bullet with no content."""
    append_pr_feedback(plan_path, [FakeFeedback(body="")], now=FIXED)
    assert "  > " in plan_path.read_text(encoding="utf-8")


def test_link_is_included_for_every_entry(plan_path: Path) -> None:
    append_pr_feedback(plan_path, [FakeFeedback(url="https://example.test/9")], now=FIXED)
    assert "[link](https://example.test/9)" in plan_path.read_text(encoding="utf-8")


def test_plan_remains_parseable_after_append(plan_path: Path) -> None:
    """The whole point of writing through `render_frontmatter`: the next
    `read_plan` must still see a valid plan."""
    append_pr_feedback(plan_path, [FakeFeedback(body="a\nb")], now=FIXED)
    append_pr_feedback(plan_path, [FakeFeedback(author_is_bot=True)], now=FIXED)
    plan = read_plan(plan_path)
    assert plan.status == "in_progress"
    assert plan.slug == "0001-example"
