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
from nightly_core.plans import PlanRecord, list_plans, parse_frontmatter
from nightly_core.pr_feedback import FeedbackFetcher, PRFeedback, fetch_feedback
from nightly_core.proposers.base import Proposal
from nightly_core.runs import current_run
from nightly_core.triage import IssueRanking, rank_issues

__all__ = [
    "CASCADE_SOURCES",
    "CascadeChoice",
    "CascadeSource",
    "PRRescueCandidate",
    "count_open_nightly_prs",
    "next_task",
    "pick_accepted_rfc",
    "pick_github_issue",
    "pick_ideated",
    "pick_ideated_fallback",
    "pick_in_flight",
    "pick_pr_rescue",
    "pick_unblocked",
]


CascadeSource = Literal[
    "concluded",
    "resume_in_flight",
    "unblocked_approval",
    "accepted_rfc",
    "github_issue",
    "pr_rescue",
    "ideate",
    "ideate_fallback",
    "nothing",
]
"""Which cascade rule fired.

- `concluded` — `nightly conclude` was called (CONCLUDE marker present).
  No new work; drain and render the briefing. Takes precedence over
  every other step in the cascade.
- `nothing` — no work was found *and* no fallback proposal was available
  (or the session wasn't armed). Distinct from `concluded` because it
  may transition to ideate_fallback if the session arms later."""

CASCADE_SOURCES: tuple[CascadeSource, ...] = (
    "concluded",
    "resume_in_flight",
    "unblocked_approval",
    "accepted_rfc",
    "github_issue",
    "pr_rescue",
    "ideate",
    "ideate_fallback",
    "nothing",
)


# Marker filename mirrored from runs.conclude_run.
_CONCLUDE_FILENAME = "CONCLUDE"


def _conclude_requested(root: Path | None = None) -> bool:
    """True iff the current run has a CONCLUDE marker.

    `nightly conclude` writes this file; `nightly next` must honor it
    by halting the cascade before any cascade step runs. Without this
    check the agent keeps picking up new ideate work after the human
    has signaled drain.
    """
    run = current_run(root)
    if run is None:
        return False
    return (run.path / _CONCLUDE_FILENAME).is_file()


# Marker filename mirrored from keepalive_hook.SESSION_ACTIVE_FILENAME.
# Inlined rather than imported because keepalive_hook imports from this
# module — we'd otherwise have a circular import.
_SESSION_ACTIVE_FILENAME = "SESSION_ACTIVE"


def _session_armed(root: Path | None = None) -> bool:
    """True iff the current run has a SESSION_ACTIVE marker.

    Mirrors the keep-alive contract: when armed, the Stop hook will
    force-continue and the cascade should never return `nothing`. When
    disarmed, returning `nothing` is fine — no one's force-continuing
    the session anyway.
    """
    run = current_run(root)
    if run is None:
        return False
    return (run.path / _SESSION_ACTIVE_FILENAME).is_file()


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
    """Return the first unchecked task-list item from an `accepted` RFC.

    Uses `parse_frontmatter` to read the `status:` field exactly —
    earlier versions did a substring match against `"status: accepted"`
    anywhere in the file body, which mis-picked draft RFCs whose prose
    or checklist *discussed* the accepted status (e.g. RFC 002's
    sizing checkbox `[ ] Promote RFC frontmatter to status: accepted`
    matched the substring even though the frontmatter was draft).
    Frontmatter parsing makes the contract exact: only an RFC whose
    *parsed* `status` field equals `accepted` is eligible.
    """
    planning = planning_dir(root)
    rfcs = planning / "rfcs"
    if not rfcs.is_dir():
        return None
    for entry in sorted(rfcs.iterdir()):
        if not entry.is_file() or entry.suffix != ".md":
            continue
        text = entry.read_text(encoding="utf-8")
        metadata, body = parse_frontmatter(text)
        if metadata.get("status", "").strip().lower() != "accepted":
            continue
        # Only search the body for unchecked items — checkboxes inside
        # the frontmatter (or in front of the first `---` fence) don't
        # count. Matches the brainstorm's intent: the RFC's *task list*
        # is the scope, not its YAML metadata.
        match = _RFC_UNCHECKED_RE.search(body)
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


def count_open_nightly_prs(root: Path | None = None) -> int:
    """Count open `nightly/*` PRs against the current repo.

    Best-effort: returns 0 when `gh` is missing, the remote has no PRs,
    or the listing fails. Wraps `_nightly_open_pr_branches` so callers
    outside the cascade module (e.g. the Stop hook) don't have to reach
    for an underscore-prefixed helper.
    """
    return len(_nightly_open_pr_branches(root))


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


_DEDUPED_STATUSES: frozenset[str] = frozenset({"done", "in_progress", "blocked: approval"})
"""Plan statuses that signal "this proposal's work is already in flight
or shipped" — the dedupe filter excludes future proposals matching their
fingerprint. `parked` is intentionally OUT (re-proposing makes sense),
as is `dispatching` (transient sentinel — the next-step plan it claims
will land at `in_progress` immediately). See issue #2."""


def _proposed_fingerprints_in_use(root: Path | None = None) -> set[str]:
    """Fingerprints of every plan whose work the cascade should not re-propose.

    Walks every plan across every run (cheap — frontmatter only), keeping
    those whose status is in `_DEDUPED_STATUSES` and which carry a
    `proposer_fingerprint`. Hand-authored plans (no fingerprint) and
    `parked` / `ready` plans don't contribute. Returning a set means
    membership checks are O(1) inside the cascade hot path.
    """
    out: set[str] = set()
    for plan in list_plans(root):
        if plan.status not in _DEDUPED_STATUSES:
            continue
        fp = plan.proposer_fingerprint
        if fp is not None:
            out.add(fp)
    return out


def _dedupe_proposals(proposals: list[Proposal], root: Path | None = None) -> list[Proposal]:
    """Filter out proposals whose fingerprint matches a `done` /
    `in_progress` / `blocked: approval` plan from any run.

    Preserves the input ordering. Proposals without a fingerprint
    (theoretically impossible — every Proposal has the property — but
    defensive against future shape drift) are always kept.
    """
    blocked = _proposed_fingerprints_in_use(root)
    if not blocked:
        return proposals
    return [p for p in proposals if p.fingerprint not in blocked]


def pick_ideated(root: Path | None = None) -> Proposal | None:
    """Run the proposer suite and return an auto-PR-eligible proposal, if any.

    The strict step: run the ideation suite and see if any proposal clears
    the conservative autonomy bar. If yes, return it as work the agent can
    execute autonomously and land as a real PR. If no, the cascade either
    falls through to `pick_ideated_fallback` (armed sessions) or to
    `nothing` (disarmed sessions).

    Re-proposals are filtered out before the autonomy bar runs — a
    proposal whose fingerprint matches a `done` plan from this or a
    prior run is skipped (see issue #2 — `type_holes` re-detected the
    same `Any` usages because nothing landed on `main`).
    """
    proposals = _dedupe_proposals(run_proposers(root or repo_root()), root)
    return top_auto_pr(proposals)


def pick_ideated_fallback(root: Path | None = None) -> Proposal | None:
    """Highest-scoring proposal regardless of autonomy-bar eligibility.

    The "make a recommendation, just go" lever. Only fires when the
    session is armed (SESSION_ACTIVE present) and the strict cascade
    came up empty — turning the cascade's bottom rung from "give up"
    into "ship the best idea, even if it's a local proposal branch
    rather than an auto-PR." The driver downgrades non-eligible
    proposals to a local proposal branch automatically.

    Same dedupe as `pick_ideated`: a re-proposal of already-landed work
    returns None instead of re-dispatching the duplicate.
    """
    proposals = _dedupe_proposals(run_proposers(root or repo_root()), root)
    if not proposals:
        return None
    return proposals[0]  # already score-sorted desc by run_proposers


# ── the cascade itself ────────────────────────────────────────────────────


def next_task(root: Path | None = None) -> CascadeChoice:  # noqa: PLR0911 - one return per cascade step is the whole point
    """Walk the cascade and return the first hit.

    The order is fixed:
    0. CONCLUDE marker present → return `concluded` (drain, no new work)
    1. resume an `in_progress` plan
    2. resume a `blocked: approval` plan whose approval has been granted
    3. start the next unchecked item from an accepted RFC
    4. pick the highest-ranked open GitHub issue
    5. nothing — caller decides whether to ideate (Phase 5) or stop
    """
    root = (root or repo_root()).resolve()

    # CONCLUDE wins absolutely. Once the human asks the run to wind
    # down, the cascade must not hand out new work — neither in-flight
    # resumes nor ideate fallbacks. Drain the current task only.
    if _conclude_requested(root):
        return CascadeChoice(
            source="concluded",
            summary="conclude requested — drain in-flight task only",
            target_path=None,
            rationale=(
                "`nightly conclude` was invoked (CONCLUDE marker is present "
                "under the current run). Do not pick up new work from any "
                "cascade step. Finish the task currently in flight (if any), "
                "render the morning briefing with `nightly brief`, and let "
                "the Stop hook allow the session to end. The human is back; "
                "monotonic forward progress is no longer the contract."
            ),
        )

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

    # Armed-session fallback: when nothing else is left, ship the best
    # proposal we can find regardless of whether it clears the auto-PR
    # bar. Non-eligible ones land as a local proposal branch instead of
    # an automatic PR — the agent + driver decide on landing strategy.
    # This is the "if you can recommend, execute" lever expressed in the
    # cascade: the recommendation IS the top-scoring proposal.
    #
    # When reaching the bottom of the cascade we also need to distinguish
    # *why* there's nothing to do, because the hook prompts the agent
    # differently for "proposer suite returned 0" vs "every proposal was
    # a duplicate of completed work." Without this signal the hook
    # would falsely claim "the proposer suite is empty" when actually
    # the dedupe filter caught every candidate (dogfooding Issue #11).
    armed = _session_armed(root)
    if armed:
        fallback = pick_ideated_fallback(root)
        if fallback is not None:
            return CascadeChoice(
                source="ideate_fallback",
                summary=f"work on proposed (fallback): {fallback.title}",
                target_path=None,
                rationale=(
                    f"Session is armed and the strict cascade came up empty. "
                    f"Top proposal from '{fallback.proposer}' "
                    f"(category {fallback.category}, score {fallback.score:.2f}, "
                    f"{fallback.estimated_loc} LOC, "
                    f"scope {list(fallback.file_scope)}) does not clear the "
                    "auto-PR autonomy bar — dispatching anyway as a local "
                    "proposal branch. The agent should land it locally (no "
                    "automatic PR) and let the morning briefing surface it for "
                    "human review."
                ),
                score=fallback.score,
            )

    # Distinguish "no proposals at all" from "every proposal deduped" so
    # the hook can prompt the agent accordingly. `run_proposers` is the
    # raw output; `_dedupe_proposals` filters against done/in_progress
    # plans. We recompute the count here (cheap — the proposers already
    # ran inside `pick_ideated_fallback`; this hits cached file reads on
    # the same turn).
    all_proposals = run_proposers(root)
    surviving = _dedupe_proposals(all_proposals, root)
    if all_proposals and not surviving:
        # Every proposal was a duplicate of completed / in-flight work.
        nothing_rationale = (
            f"The cascade returned `nothing` because every proposal the "
            f"suite surfaced ({len(all_proposals)}) matched the fingerprint "
            "of a completed or in-flight plan. Don't ask the proposers for "
            "the same thing again — look at sources the proposers don't "
            "cover (`.planning/` open questions, README gaps, uncertainty.md "
            "from recent runs, closed-PR review threads). "
            + (
                "Session is armed."
                if armed
                else "Session is not armed — graceful exit is appropriate."
            )
        )
    elif not all_proposals:
        nothing_rationale = (
            "No in-flight plans, no unblocked tasks, no accepted RFC items, "
            "no nightly-eligible issues, no PR-rescue candidates, no "
            "proposals at any tier — the proposer suite came up empty. "
            + (
                "Session is armed; the auto-ideate fallback also returned "
                "nothing."
                if armed
                else "Session is not armed — graceful exit is appropriate. "
                "Arm with `nightly session start` and re-run `nightly next` "
                "to enable the auto-ideate fallback path."
            )
        )
    else:
        # Proposals existed but none reached fallback because session
        # disarmed AND none cleared the strict autonomy bar.
        nothing_rationale = (
            f"The cascade returned `nothing` because the {len(all_proposals)} "
            "proposal(s) surfaced by the suite didn't clear the auto-PR "
            "autonomy bar and the session is not armed (so the fallback "
            "path is gated off). Arm with `nightly session start` to enable "
            "the fallback, or accept the strict-cascade-only mode."
        )

    return CascadeChoice(
        source="nothing",
        summary="no work — backlog is empty",
        rationale=nothing_rationale,
    )
