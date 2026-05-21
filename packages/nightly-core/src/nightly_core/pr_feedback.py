"""PR feedback fetching — review comments, CI failures, bot summaries.

This module is the cascade's input for `pick_pr_rescue`. It shells out
to `gh` to pull every kind of feedback a Nightly-authored PR can collect
(human reviews, inline review comments, issue comments, CI/check
failures), normalizes them into one `PRFeedback` shape, and lets the
caller filter by:

- **author class** — humans vs. bots (so the cascade can rescue on
  bot-only feedback without waiting for a human, or skip noisy bots
  entirely)
- **kind** — `review` / `review_comment` / `issue_comment` / `check_failure`
  (so the driver can pick a different specialist prompt: CI failures
  want the implementer; review comments often want the reviewer or
  researcher)
- **freshness** — feedback newer than the plan's `pr_last_reconciled_at`
  stamp, so a plan that's already addressed all prior rounds doesn't
  get picked again until *new* feedback lands

Bot detection: GitHub marks bot accounts with `author.type == "Bot"` in
the API. We also accept a configurable allowlist of known review bots
(CodeRabbit, Cursor BugBot, Copilot reviewer, Greptile, Amp) so a
real-human account that happens to be named "ci-bot" still counts as
human if the user has flagged it as such in config. Default config
treats *all* bot-flagged accounts as bots.

Everything is best-effort: if `gh` is missing or the branch has no PR,
the fetcher returns `[]` rather than raising.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

__all__ = [
    "DEFAULT_REVIEW_BOTS",
    "FeedbackFetcher",
    "FeedbackKind",
    "PRFeedback",
    "PRReference",
    "fetch_feedback",
    "fetch_via_gh",
]


FeedbackKind = Literal[
    "review",  # PR-level review (approve / request_changes / comment)
    "review_comment",  # inline review comment with file:line context
    "issue_comment",  # PR-thread comment with no file context
    "check_failure",  # failed CI / status check
]

# Well-known review-agent logins that we treat as bots even if GitHub
# doesn't flag them (some are user accounts with bot-shaped behavior).
# The set is conservative — users can extend via .nightly/config.yml.
DEFAULT_REVIEW_BOTS: frozenset[str] = frozenset(
    {
        "coderabbitai",
        "coderabbitai[bot]",
        "github-copilot[bot]",
        "copilot-pull-request-reviewer[bot]",
        "cursor[bot]",
        "cursoragent",
        "greptile-apps[bot]",
        "amp-app[bot]",
        "devin-ai[bot]",
        "renovate[bot]",
        "dependabot[bot]",
    }
)


@dataclass(frozen=True)
class PRReference:
    """Minimal PR identity — branch + number + URL."""

    branch: str
    number: int
    url: str
    state: str  # "OPEN" / "CLOSED" / "MERGED"
    title: str


class PRFeedback(BaseModel):
    """One piece of feedback attached to a PR — comment, review, or check."""

    pr: PRReference
    kind: FeedbackKind
    author_login: str
    author_is_bot: bool
    body: str
    state: str | None = None
    """For `review` kind: APPROVED · CHANGES_REQUESTED · COMMENTED · DISMISSED.
    For `check_failure` kind: the failing check name."""

    file_ref: str | None = None
    line_ref: int | None = None
    created_at: datetime
    url: str

    @property
    def is_blocking(self) -> bool:
        """Heuristic: would a reasonable maintainer expect the agent to act on this?"""
        return self.kind == "check_failure" or (
            self.kind == "review" and self.state == "CHANGES_REQUESTED"
        )


FeedbackFetcher = Callable[[str, Path | None], list[PRFeedback]]
"""(branch, repo_root) → feedback list. Injectable for tests."""


# ── default gh fetcher ────────────────────────────────────────────────────

_GH_PR_FIELDS = "number,title,url,state,headRefName,reviews,comments,statusCheckRollup"


def fetch_via_gh(
    branch: str,
    root: Path | None = None,
) -> list[PRFeedback]:
    """Production fetcher — shells out to `gh pr view` + `gh api`.

    Returns `[]` (no exception) when `gh` is missing, the branch has no
    open PR, the command fails, or the output is unparseable.
    """
    if shutil.which("gh") is None:
        return []

    pr_payload = _gh_pr_view(branch, root)
    if pr_payload is None:
        return []

    pr_ref = PRReference(
        branch=str(pr_payload.get("headRefName") or branch),
        number=int(pr_payload.get("number") or 0),
        url=str(pr_payload.get("url") or ""),
        state=str(pr_payload.get("state") or "OPEN"),
        title=str(pr_payload.get("title") or ""),
    )

    out: list[PRFeedback] = []

    # ── reviews (approve / request-changes / comment) ────────────────────
    for review in pr_payload.get("reviews") or []:
        body = (review.get("body") or "").strip()
        state = str(review.get("state") or "")
        if not body and state == "COMMENTED":
            # A bare COMMENTED review with no body is just metadata noise.
            continue
        author = review.get("author") or {}
        out.append(
            PRFeedback(
                pr=pr_ref,
                kind="review",
                author_login=str(author.get("login") or "?"),
                author_is_bot=_is_bot(author),
                body=body or f"({state.lower()} with no body)",
                state=state,
                created_at=_parse_iso(review.get("submittedAt") or review.get("createdAt") or ""),
                url=str(review.get("url") or pr_ref.url),
            )
        )

    # ── issue comments (PR-thread, not file-anchored) ────────────────────
    for comment in pr_payload.get("comments") or []:
        body = (comment.get("body") or "").strip()
        if not body:
            continue
        author = comment.get("author") or {}
        out.append(
            PRFeedback(
                pr=pr_ref,
                kind="issue_comment",
                author_login=str(author.get("login") or "?"),
                author_is_bot=_is_bot(author),
                body=body,
                created_at=_parse_iso(comment.get("createdAt") or ""),
                url=str(comment.get("url") or pr_ref.url),
            )
        )

    # ── inline review comments (file:line anchored) ──────────────────────
    inline_payload = _gh_pr_review_comments(pr_ref.number, root)
    for c in inline_payload:
        body = (c.get("body") or "").strip()
        if not body:
            continue
        user = c.get("user") or {}
        out.append(
            PRFeedback(
                pr=pr_ref,
                kind="review_comment",
                author_login=str(user.get("login") or "?"),
                author_is_bot=user.get("type") == "Bot",
                body=body,
                file_ref=c.get("path"),
                line_ref=c.get("line") or c.get("original_line"),
                created_at=_parse_iso(c.get("created_at") or ""),
                url=str(c.get("html_url") or pr_ref.url),
            )
        )

    # ── failing CI / status checks ───────────────────────────────────────
    for check in pr_payload.get("statusCheckRollup") or []:
        if not _is_failed_check(check):
            continue
        out.append(
            PRFeedback(
                pr=pr_ref,
                kind="check_failure",
                author_login=str(check.get("workflowName") or check.get("name") or "ci"),
                author_is_bot=True,
                body=str(
                    check.get("summary")
                    or check.get("description")
                    or f"check failed: {check.get('name') or 'unnamed'}"
                ),
                state=str(check.get("name") or "check"),
                created_at=_parse_iso(check.get("completedAt") or check.get("startedAt") or ""),
                url=str(check.get("detailsUrl") or check.get("url") or pr_ref.url),
            )
        )

    return out


def _is_bot(author: dict) -> bool:
    """GitHub uses `type == 'Bot'` on the user object. The PR-view JSON
    schema doesn't always include `type`; we also fall back to the
    `is_bot` field gh sometimes synthesizes, and to the known login set."""
    if not isinstance(author, dict):
        return False
    if author.get("type") == "Bot":
        return True
    if author.get("is_bot") is True:
        return True
    login = str(author.get("login") or "")
    return login in DEFAULT_REVIEW_BOTS


def _is_failed_check(check: dict) -> bool:
    """Different check kinds use different fields. Cover the common shapes."""
    if not isinstance(check, dict):
        return False
    conclusion = (check.get("conclusion") or "").upper()
    if conclusion in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        return True
    state = (check.get("state") or check.get("status") or "").upper()
    return state in {"FAILURE", "ERROR"}


def _gh_pr_view(branch: str, root: Path | None) -> dict | None:
    """Fetch the PR for `branch` via `gh pr view --json ...`. None if no PR."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                branch,
                "--json",
                _GH_PR_FIELDS,
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    if not result.stdout.strip():
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _gh_pr_review_comments(pr_number: int, root: Path | None) -> list[dict]:
    """Inline review comments need the v3 API; `gh pr view` doesn't include them."""
    if pr_number <= 0:
        return []
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments",
                "--paginate",
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
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_iso(value: str) -> datetime:
    """Parse `gh`'s ISO-8601 timestamps; tolerate trailing Z and empty strings."""
    if not value:
        # Sentinel value when an upstream record lacked a timestamp. Using
        # epoch keeps comparisons deterministic without raising.
        return datetime.fromtimestamp(0, tz=_utc())
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc():  # tiny helper so the module's only datetime import stays at the top
    from datetime import UTC  # noqa: PLC0415

    return UTC


# ── public API ────────────────────────────────────────────────────────────


def fetch_feedback(
    branch: str,
    *,
    root: Path | None = None,
    fetcher: FeedbackFetcher | None = None,
    since: datetime | None = None,
) -> list[PRFeedback]:
    """Return feedback for `branch`, optionally filtered to entries newer than `since`.

    `fetcher` is injectable for tests. Production passes `fetch_via_gh`.
    `since` defaults to None (return everything); the cascade uses the
    plan's `pr_last_reconciled_at` stamp.
    """
    runner = fetcher or fetch_via_gh
    items = runner(branch, root)
    if since is None:
        return items
    return [item for item in items if item.created_at > since]
