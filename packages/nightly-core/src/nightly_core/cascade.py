"""The priority cascade — Nightly's autonomous task picker.

The cascade walks a fixed ordered chain of "is there work of type X?"
questions and stops at the first hit. The riskiest behavior (ideation) is
intentionally the last resort. The brainstorm section 03 enumerates the
cascade; this module implements steps 1-5 (ideation is Phase 5).

The whole thing is decomposed into discrete functions so each step is
independently testable and the agent can `nightly next` to see exactly
which step fired and why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nightly_core.ideation import run_proposers, top_auto_pr
from nightly_core.paths import planning_dir, repo_root
from nightly_core.plans import PlanRecord, list_plans
from nightly_core.proposers.base import Proposal
from nightly_core.triage import IssueRanking, rank_issues

__all__ = [
    "CASCADE_SOURCES",
    "CascadeChoice",
    "CascadeSource",
    "next_task",
    "pick_accepted_rfc",
    "pick_github_issue",
    "pick_ideated",
    "pick_in_flight",
    "pick_unblocked",
]


CascadeSource = Literal[
    "resume_in_flight",
    "unblocked_approval",
    "accepted_rfc",
    "github_issue",
    "ideate",
    "nothing",
]
"""Which cascade rule fired. `nothing` means no work was found."""

CASCADE_SOURCES: tuple[CascadeSource, ...] = (
    "resume_in_flight",
    "unblocked_approval",
    "accepted_rfc",
    "github_issue",
    "ideate",
    "nothing",
)


@dataclass(frozen=True)
class CascadeChoice:
    """What to do next, and why.

    `target_path` may point at a plan.md, an RFC, or be None when the
    target is a remote artifact (GitHub issue). `summary` is a single
    human-readable line; `rationale` is a fuller explanation suitable for
    the briefing.
    """

    source: CascadeSource
    summary: str
    target_path: Path | None = None
    rationale: str | None = None
    score: float | None = None


# ── individual cascade steps ──────────────────────────────────────────────


def pick_in_flight(root: Path | None = None) -> PlanRecord | None:
    """Return the first plan with `status: in_progress`, if any.

    Order: by run id ascending, then by task index. The oldest in-flight
    work is resumed first — this avoids context-switching mid-stream.
    """
    for plan in list_plans(root):
        if plan.status == "in_progress":
            return plan
    return None


def pick_unblocked(root: Path | None = None) -> PlanRecord | None:
    """Return the first `blocked: approval` plan whose approval is granted."""
    for plan in list_plans(root):
        if plan.status == "blocked: approval" and plan.approval_granted:
            return plan
    return None


# Matches a markdown task list item: `- [ ] do something` (unchecked) and
# captures the item text. Indented checkboxes (nested lists) are intentionally
# excluded — only top-level RFC items count as cascade candidates.
_RFC_UNCHECKED_RE = re.compile(r"^- \[ \] (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class _RFCMatch:
    rfc_path: Path
    item_text: str


def _find_accepted_rfc(root: Path | None = None) -> _RFCMatch | None:
    planning = planning_dir(root)
    rfcs = planning / "rfcs"
    if not rfcs.is_dir():
        return None
    for entry in sorted(rfcs.iterdir()):
        if not entry.is_file() or entry.suffix != ".md":
            continue
        text = entry.read_text(encoding="utf-8")
        if "status: accepted" not in text.lower():
            continue
        match = _RFC_UNCHECKED_RE.search(text)
        if match:
            return _RFCMatch(rfc_path=entry, item_text=match.group(1).strip())
    return None


def pick_accepted_rfc(root: Path | None = None) -> _RFCMatch | None:
    """Return the first unchecked task-list item from an accepted RFC."""
    return _find_accepted_rfc(root)


def pick_github_issue(root: Path | None = None) -> IssueRanking | None:
    """Return the highest-ranked nightly-eligible GitHub issue, if any."""
    rankings = rank_issues(root)
    eligible = [r for r in rankings if r.skip_reason is None]
    if not eligible:
        return None
    return eligible[0]


def pick_ideated(root: Path | None = None) -> Proposal | None:
    """Run the proposer suite and return an auto-PR-eligible proposal, if any.

    This is the cascade's last-resort productive step: when no human work
    exists, run the ideation suite and see if any proposal clears the
    conservative autonomy bar. If yes, the cascade returns it as work the
    agent can execute autonomously. If no, the cascade falls through to
    `nothing` and the agent should write narrative + brief + exit.
    """
    proposals = run_proposers(root or repo_root())
    return top_auto_pr(proposals)


# ── the cascade itself ────────────────────────────────────────────────────


def next_task(root: Path | None = None) -> CascadeChoice:
    """Walk the cascade and return the first hit.

    The order is fixed:
    1. resume an `in_progress` plan
    2. resume a `blocked: approval` plan whose approval has been granted
    3. start the next unchecked item from an accepted RFC
    4. pick the highest-ranked open GitHub issue
    5. nothing — caller decides whether to ideate (Phase 5) or stop
    """
    root = (root or repo_root()).resolve()

    in_flight = pick_in_flight(root)
    if in_flight is not None:
        return CascadeChoice(
            source="resume_in_flight",
            summary=f"resume {in_flight.slug} (status: in_progress)",
            target_path=in_flight.path,
            rationale=(
                "An in-flight plan was found across runs. Finishing what's "
                "started outranks picking new work."
            ),
        )

    unblocked = pick_unblocked(root)
    if unblocked is not None:
        return CascadeChoice(
            source="unblocked_approval",
            summary=f"retry {unblocked.slug} (approval granted)",
            target_path=unblocked.path,
            rationale=(
                "A previously parked task has had its approval granted. "
                "Resuming it before reaching for fresh work."
            ),
        )

    rfc = pick_accepted_rfc(root)
    if rfc is not None:
        return CascadeChoice(
            source="accepted_rfc",
            summary=f"work on accepted RFC item: {rfc.item_text}",
            target_path=rfc.rfc_path,
            rationale=(
                "An accepted RFC has unstarted task list items. RFCs are "
                "human-blessed scope and outrank issue triage."
            ),
        )

    issue = pick_github_issue(root)
    if issue is not None:
        return CascadeChoice(
            source="github_issue",
            summary=f"pick #{issue.number}: {issue.title}",
            target_path=None,
            rationale=(
                f"Highest-ranked open issue (score {issue.score:.2f}). "
                "No in-flight work, no RFC items, no unblocked tasks."
            ),
            score=issue.score,
        )

    ideated = pick_ideated(root)
    if ideated is not None:
        return CascadeChoice(
            source="ideate",
            summary=f"work on proposed: {ideated.title}",
            target_path=None,
            rationale=(
                f"Proposer '{ideated.proposer}' surfaced an auto-PR-eligible "
                f"proposal (category {ideated.category}, score {ideated.score:.2f}, "
                f"{ideated.estimated_loc} LOC, scope {list(ideated.file_scope)}). "
                "Cleared the autonomy bar; safe to execute without human approval."
            ),
            score=ideated.score,
        )

    return CascadeChoice(
        source="nothing",
        summary="no work — backlog is empty",
        rationale=(
            "No in-flight plans, no unblocked tasks, no accepted RFC items, "
            "no nightly-eligible issues, no auto-PR-eligible proposals. "
            "Run `nightly ideate` to write draft proposals for human review, "
            "then write narrative + brief + exit."
        ),
    )
