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

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from nightly_core.ideation import run_proposers, top_auto_pr
from nightly_core.paths import planning_dir, repo_root
from nightly_core.plans import PlanRecord, list_plans
from nightly_core.pr_feedback import FeedbackFetcher, PRFeedback, fetch_feedback
from nightly_core.proposers.base import Proposal
from nightly_core.triage import IssueRanking, rank_issues

__all__ = [
    "CASCADE_SOURCES",
    "CascadeChoice",
    "CascadeSource",
    "PRRescueCandidate",
    "next_task",
    "pick_accepted_rfc",
    "pick_github_issue",
    "pick_ideated",
    "pick_in_flight",
    "pick_pr_rescue",
    "pick_unblocked",
]


CascadeSource = Literal[
    "resume_in_flight",
    "unblocked_approval",
    "accepted_rfc",
    "github_issue",
    "pr_rescue",
    "ideate",
    "nothing",
]
"""Which cascade rule fired. `nothing` means no work was found."""

CASCADE_SOURCES: tuple[CascadeSource, ...] = (
    "resume_in_flight",
    "unblocked_approval",
    "accepted_rfc",
    "github_issue",
    "pr_rescue",
    "ideate",
    "nothing",
)


@dataclass(frozen=True)
class CascadeChoice:
    """What to do next, and why.

    `target_path` may point at a plan.md, an RFC, or be None when the
    target is a remote artifact (GitHub issue) or an orphan PR with no
    matching plan. `summary` is a single human-readable line;
    `rationale` is a fuller explanation suitable for the briefing.

    `pr_feedback` is populated when `source == "pr_rescue"` — the driver
    uses it to append a "Feedback round N" section to the plan body
    before dispatch.
    """

    source: CascadeSource
    summary: str
    target_path: Path | None = None
    rationale: str | None = None
    score: float | None = None
    pr_feedback: tuple[PRFeedback, ...] | None = None


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


@dataclass(frozen=True)
class PRRescueCandidate:
    """One Nightly-authored PR that has unaddressed feedback to act on."""

    branch: str
    pr_url: str
    pr_number: int
    feedback: tuple[PRFeedback, ...] = field(default_factory=tuple)
    plan_path: Path | None = None
    """The matched task plan, if Nightly can identify which task this PR
    belongs to. `None` means we couldn't tie the branch back to a plan —
    the agent has to read the PR + feedback fresh."""

    @property
    def has_blocking(self) -> bool:
        return any(f.is_blocking for f in self.feedback)

    @property
    def summary(self) -> str:
        n = len(self.feedback)
        blocking = sum(1 for f in self.feedback if f.is_blocking)
        bot = sum(1 for f in self.feedback if f.author_is_bot)
        human = n - bot
        return (
            f"#{self.pr_number} ({self.branch}): "
            f"{n} new feedback item(s) — {blocking} blocking · "
            f"{human} human · {bot} bot"
        )


# Plans record the last time they were reconciled against PR feedback so
# the cascade can skip them when nothing new has landed since.
_PR_LAST_RECONCILED_KEY = "pr_last_reconciled_at"


def _nightly_open_pr_branches(
    root: Path | None = None,
    *,
    branch_prefix: str = "nightly/",
) -> list[tuple[str, int, str]]:
    """List `(branch, pr_number, pr_url)` for open PRs on Nightly branches.

    Uses `gh pr list --json` if `gh` is available; returns `[]` otherwise.
    """
    if shutil.which("gh") is None:
        return []
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,headRefName,url",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    out: list[tuple[str, int, str]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        branch = str(entry.get("headRefName") or "")
        if not branch.startswith(branch_prefix):
            continue
        try:
            num = int(entry.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if num <= 0:
            continue
        out.append((branch, num, str(entry.get("url") or "")))
    return out


def _match_plan_to_branch(branch: str, root: Path | None = None) -> PlanRecord | None:
    """Best-effort: find a plan whose slug appears in `branch`.

    Nightly branches follow `nightly/<slug>-<ts>`, so the plan's slug
    (`0001-add-retry`) is usually a substring of the branch name. We
    match conservatively: longest matching plan slug wins.
    """
    candidates: list[tuple[int, PlanRecord]] = []
    for plan in list_plans(root):
        slug_core = re.sub(r"^\d+-", "", plan.slug)  # drop NNNN- prefix
        if slug_core and slug_core in branch:
            candidates.append((len(slug_core), plan))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: -pair[0])
    return candidates[0][1]


def _last_reconciled(plan: PlanRecord | None) -> datetime | None:
    if plan is None:
        return None
    raw = plan.metadata.get(_PR_LAST_RECONCILED_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def pick_pr_rescue(
    root: Path | None = None,
    *,
    fetcher: FeedbackFetcher | None = None,
) -> PRRescueCandidate | None:
    """Find a Nightly-authored open PR with feedback newer than the plan's
    last reconcile stamp. Returns the most urgent candidate (blocking
    feedback first, then by feedback count), or `None`.

    Best-effort: a missing `gh`, no GitHub remote, or no open Nightly PRs
    all return `None` without raising.
    """
    branches = _nightly_open_pr_branches(root)
    if not branches:
        return None

    candidates: list[PRRescueCandidate] = []
    for branch, number, url in branches:
        plan = _match_plan_to_branch(branch, root)
        since = _last_reconciled(plan)
        new_feedback = fetch_feedback(branch, root=root, fetcher=fetcher, since=since)
        if not new_feedback:
            continue
        candidates.append(
            PRRescueCandidate(
                branch=branch,
                pr_url=url,
                pr_number=number,
                feedback=tuple(new_feedback),
                plan_path=plan.path if plan else None,
            )
        )

    if not candidates:
        return None
    # Most urgent first: blocking feedback before non-blocking, then by count.
    candidates.sort(key=lambda c: (-int(c.has_blocking), -len(c.feedback)))
    return candidates[0]


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


def next_task(root: Path | None = None) -> CascadeChoice:  # noqa: PLR0911 - one return per cascade step is the whole point
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

    rescue = pick_pr_rescue(root)
    if rescue is not None:
        return CascadeChoice(
            source="pr_rescue",
            summary=rescue.summary,
            target_path=rescue.plan_path,
            rationale=(
                f"Nightly-authored PR {rescue.pr_url} has new feedback since "
                "the last reconcile — finishing beats starting. "
                f"{'Blocking' if rescue.has_blocking else 'Non-blocking'}: "
                f"{len(rescue.feedback)} item(s). "
                "Driver will append the feedback to the plan body before dispatch."
            ),
            pr_feedback=rescue.feedback,
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
