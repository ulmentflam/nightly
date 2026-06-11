"""GitHub issue triage.

Phase 3 ships a simplified version of the brainstorm section 06 ranking
formula: just `score = w_label * w_age`, since blast-radius / coverage /
recent-activity require deeper repo inspection (a coverage report, a
change-set graph) that isn't plumbed yet. The simpler score is enough to
surface the most-likely candidates for the cascade.

Three hard gates apply independently of score:
1. Issue must not be tagged `do-not-automate`.
2. Issue must have at least *some* acceptance criterion (a body — even a
   short one). Empty-body issues are skipped with a clear reason.
3. Issue must not require credentials Nightly doesn't have. (Phase 3
   approximates this with a `needs-secrets` tag check; the real version
   awaits Phase 4 sandboxing.)

Issues are fetched via the `gh` CLI when available, falling back to an
empty list (and a clear log line) when `gh` is missing or the repo has no
GitHub remote. The fetcher is injectable so tests don't shell out.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nightly_core.paths import repo_root

__all__ = [
    "IssueFetcher",
    "IssueRanking",
    "IssueRecord",
    "OpenPRRefFetcher",
    "OpenPRRefs",
    "fetch_open_pr_issue_refs_via_gh",
    "fetch_via_gh",
    "rank_issues",
    "score_issue",
]


_GH_FIELDS = "number,title,body,labels,createdAt,updatedAt,url,author"
_GH_LIMIT_DEFAULT = 50

# Hard-gate labels — skipped regardless of score
_HARD_DENY_LABELS = frozenset({"do-not-automate", "needs-human", "needs-secrets"})

# Label weights (multiplicative; first match wins, default 1.0)
_LABEL_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("nightly-ready", 1.5),
    ("good-first-issue", 1.2),
    ("bug", 1.1),
)

# Min body length for "has acceptance criterion" heuristic
_MIN_BODY_CHARS = 40

# Soft cap on age weight — log1p grows slowly so this rarely matters in
# practice. A 10-year-old issue tops out around 4.4 with the formula below.
_MAX_AGE_WEIGHT = 5.0

# Skip-reason strings. Distinct strings so post-mortems (and the keepalive
# livelock backstop) can tell the two in-flight signals apart: an explicit
# closing-keyword claim vs. a bare mention inside a Nightly-authored PR.
_SKIP_REASON_CLOSING_REF = "open PR already addresses this issue"
_SKIP_REASON_NIGHTLY_MENTION = "open Nightly PR references this issue (in-flight)"


@dataclass(frozen=True)
class IssueRecord:
    """One issue from `gh issue list --json ...` (normalized)."""

    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    url: str
    author: str


@dataclass(frozen=True)
class IssueRanking:
    """An issue plus its score and (if relevant) the reason to skip it."""

    issue: IssueRecord
    score: float
    skip_reason: str | None = None

    @property
    def number(self) -> int:
        return self.issue.number

    @property
    def title(self) -> str:
        return self.issue.title


IssueFetcher = Callable[[Path], list[IssueRecord]]
"""A function that returns issues for a given repo root (injectable for tests)."""


# ── scoring ────────────────────────────────────────────────────────────────


def _label_weight(labels: tuple[str, ...]) -> float:
    label_set = {lab.lower() for lab in labels}
    for label, weight in _LABEL_WEIGHTS:
        if label in label_set:
            return weight
    return 1.0


def _age_weight(created_at: datetime, *, now: datetime | None = None) -> float:
    """Logarithmic growth: monotonic forever, but slow enough that a
    year-old issue is only ~3.5x a fresh one (not 13x as linear growth
    would give). Caps at `_MAX_AGE_WEIGHT` as a defence against absurd
    inputs.
    """
    moment = now or datetime.now(UTC)
    age_days = max((moment - created_at).total_seconds() / 86400.0, 0.0)
    return min(_MAX_AGE_WEIGHT, 1.0 + math.log1p(age_days / 30.0))


def _skip_reason(
    issue: IssueRecord,
    *,
    open_pr_refs: OpenPRRefs = None,  # type: ignore[assignment]
) -> str | None:
    # Default sentinel resolved here (frozen dataclass can't be a mutable
    # default at the param site); an empty OpenPRRefs means "no overlap".
    if open_pr_refs is None:
        open_pr_refs = OpenPRRefs()
    label_set = {lab.lower() for lab in issue.labels}
    deny = label_set & _HARD_DENY_LABELS
    if deny:
        return f"hard-deny label: {sorted(deny)[0]}"
    if len(issue.body.strip()) < _MIN_BODY_CHARS:
        return f"no acceptance criterion (body < {_MIN_BODY_CHARS} chars)"
    # Closing-ref check takes precedence over the weaker nightly-mention
    # signal: a closing keyword in ANY open PR is an explicit claim, so it
    # wins when both branches match the same issue number.
    if issue.number in open_pr_refs.closing_refs:
        # Issue #10 §bug-3: cascade re-picked an issue forever because
        # an open PR fixing it didn't appear in the ranker's skip list.
        # We now scan open PR titles + bodies for closing references
        # (`fixes #N` / `closes #N` / `resolves #N`) and skip matching
        # issues. RFC 001 §A2's same-shape logic for RFC checklist items
        # is the inspiration; this extends it to GitHub issues.
        return _SKIP_REASON_CLOSING_REF
    if issue.number in open_pr_refs.nightly_mention_refs:
        # Issue #27: the §bug-3 closing-keyword guard missed the epic
        # livelock. An epic (#125) was referenced by per-item PRs as
        # "(#125)" in the title and "Addresses item #5 of #125" in the
        # body — deliberately NO closing keyword (a per-item PR must not
        # auto-close the whole epic). The closing-ref scan never matched,
        # so the cascade re-picked #125 for 300+ turns. We now ALSO treat
        # a bare `#N` mention as an in-flight signal, but only when the
        # mention appears in a Nightly-authored PR (headRefName starts
        # with `nightly/`): a bare mention from an arbitrary contributor's
        # PR is too weak, but in a PR the orchestrator itself opened it
        # means "Nightly is already working this issue" — re-picking it
        # before that PR merges is exactly the livelock.
        return _SKIP_REASON_NIGHTLY_MENTION
    return None


def score_issue(issue: IssueRecord, *, now: datetime | None = None) -> float:
    """Return the multiplicative score for one issue.

    Skip-eligible issues still get a score (the cascade may want to surface
    them with a strikethrough), but `IssueRanking.skip_reason` indicates the
    issue should not be picked.
    """
    return _label_weight(issue.labels) * _age_weight(issue.created_at, now=now)


# ── fetching via `gh` ──────────────────────────────────────────────────────


def fetch_via_gh(
    root: Path,
    *,
    state: str = "open",
    limit: int = _GH_LIMIT_DEFAULT,
) -> list[IssueRecord]:
    """Default issue fetcher — shells out to `gh issue list --json ...`.

    Returns an empty list (no exception) when `gh` is missing, the repo has
    no GitHub remote, or the command fails. Triage is best-effort.
    """
    if shutil.which("gh") is None:
        return []
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                state,
                "--limit",
                str(limit),
                "--json",
                _GH_FIELDS,
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    return _parse_gh_json(result.stdout)


def _parse_gh_json(payload: str) -> list[IssueRecord]:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return []
    out: list[IssueRecord] = []
    for entry in raw:
        try:
            labels = tuple(lab.get("name", "") for lab in entry.get("labels", []))
            author = entry.get("author", {}).get("login", "") or ""
            out.append(
                IssueRecord(
                    number=int(entry["number"]),
                    title=str(entry.get("title", "")),
                    body=str(entry.get("body", "") or ""),
                    labels=labels,
                    created_at=_parse_iso(entry["createdAt"]),
                    updated_at=_parse_iso(entry.get("updatedAt", entry["createdAt"])),
                    url=str(entry.get("url", "")),
                    author=author,
                )
            )
        except (KeyError, ValueError):
            continue
    return out


def _parse_iso(value: str) -> datetime:
    # gh emits ISO 8601 with `Z`; Python wants `+00:00`.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ── open-PR overlap detection (issue #10 §bug-3, issue #27) ───────────────


@dataclass(frozen=True)
class OpenPRRefs:
    """Issue numbers that open PRs signal are already being worked.

    Two channels with different evidentiary strength:

    - `closing_refs` — issue numbers any open PR (regardless of author or
      branch) claims to close via GitHub's documented closing keywords
      (`fixes #N` / `closes #N` / `resolves #N`). A closing keyword is an
      explicit machine-readable claim, so it counts from ALL open PRs.
      This is the issue #10 §bug-3 signal, unchanged.
    - `nightly_mention_refs` — issue numbers mentioned as a bare `#N` in
      a Nightly-authored PR (one whose `headRefName` starts with
      `nightly/`). A bare mention is too weak a signal from an arbitrary
      PR, but inside a PR the orchestrator itself opened it means
      "Nightly is already working this issue." This is the issue #27
      epic-livelock signal: per-item PRs reference an epic without a
      closing keyword (so they don't auto-close it), which slipped past
      the §bug-3 closing-keyword scan and re-picked the epic forever.

    Empty (the default) means "no overlap" — the ranker degrades cleanly
    to pre-fix behavior.
    """

    closing_refs: frozenset[int] = frozenset()
    nightly_mention_refs: frozenset[int] = frozenset()


OpenPRRefFetcher = Callable[[Path], "OpenPRRefs | frozenset[int]"]
"""Returns the open-PR issue-overlap signals for a repo root.

Production passes `fetch_open_pr_issue_refs_via_gh`, which returns an
`OpenPRRefs`. Injectable for tests; for backward compatibility `rank_issues`
also accepts a bare `frozenset[int]` and treats it as `closing_refs` (the
pre-issue-#27 return shape that existing injected fakes still use)."""


_NIGHTLY_BRANCH_PREFIX = "nightly/"
"""Head-branch prefix that marks a PR as Nightly-authored. Only PRs whose
`headRefName` starts with this prefix contribute `nightly_mention_refs`."""


_CLOSE_KEYWORD_RE = re.compile(
    r"(?:close[ds]?|fix(?:e[ds])?|resolve[ds]?)\s*:?\s*#(\d+)",
    re.IGNORECASE,
)
"""Match GitHub's documented closing-keyword grammar in PR titles + bodies.

Covers: close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved.
The optional colon (`closes: #93`) and optional whitespace before `#`
accommodate the most common operator-typed variants. We don't try to
match cross-repo refs (`closes owner/repo#93`) — that's a v2 concern;
the same-repo case is the load-bearing one."""


_BARE_MENTION_RE = re.compile(r"#(\d+)")
"""Match any bare `#N` issue/PR reference in a PR's title + body.

Used ONLY for Nightly-authored PRs (see `_NIGHTLY_BRANCH_PREFIX`). This
deliberately over-matches: a Nightly PR's body may cross-reference OTHER
PRs ("supersedes PR #123", "see (#108)") as well as the issue it works.
That over-match is acceptable and largely self-resolving: GitHub draws
issue and PR numbers from one shared sequence, so a number that belongs
to a PR can never also belong to an issue — a `#N` that is really a PR
reference therefore can't collide with any issue `#N` in the ranker's
input set. We do not try to strip PR self-references; it would be extra
complexity for a collision that cannot occur."""


def fetch_open_pr_issue_refs_via_gh(root: Path) -> OpenPRRefs:
    """Scan open PRs for issue-overlap signals.

    Returns an `OpenPRRefs` carrying two channels (see that class):

    - `closing_refs` — issue numbers any open PR claims to close via
      `fixes #N` / `closes #N` / `resolves #N` (and the inflected
      variants the GitHub docs accept). From ALL open PRs.
    - `nightly_mention_refs` — issue numbers mentioned as a bare `#N`
      in a Nightly-authored PR (`headRefName` starts with `nightly/`).

    Best-effort: empty `OpenPRRefs` when `gh` is missing, the repo has
    no GitHub remote, the call fails, or the output isn't parseable. The
    ranker treats an empty result as "no overlap" — i.e. degrades
    cleanly to pre-fix behavior, never as a hard failure.
    """
    if shutil.which("gh") is None:
        return OpenPRRefs()
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "title,body,headRefName",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return OpenPRRefs()
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return OpenPRRefs()
    closing: set[int] = set()
    nightly_mentions: set[int] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        text = f"{entry.get('title') or ''}\n{entry.get('body') or ''}"
        for match in _CLOSE_KEYWORD_RE.finditer(text):
            try:
                closing.add(int(match.group(1)))
            except ValueError:
                continue
        head_ref = str(entry.get("headRefName") or "")
        if head_ref.startswith(_NIGHTLY_BRANCH_PREFIX):
            for match in _BARE_MENTION_RE.finditer(text):
                try:
                    nightly_mentions.add(int(match.group(1)))
                except ValueError:
                    continue
    return OpenPRRefs(
        closing_refs=frozenset(closing),
        nightly_mention_refs=frozenset(nightly_mentions),
    )


# ── public API ─────────────────────────────────────────────────────────────


def _coerce_open_pr_refs(value: OpenPRRefs | frozenset[int] | set[int]) -> OpenPRRefs:
    """Normalize a `pr_ref_fetcher` return value to `OpenPRRefs`.

    Production fetchers return `OpenPRRefs` directly. Pre-issue-#27 tests
    (and any caller still using the old shape) inject a bare set of
    closing-ref issue numbers; treat that as `closing_refs` so the
    injectable-fetcher pattern keeps working without a rewrite.
    """
    if isinstance(value, OpenPRRefs):
        return value
    return OpenPRRefs(closing_refs=frozenset(value))


def rank_issues(
    root: Path | None = None,
    *,
    fetcher: IssueFetcher | None = None,
    pr_ref_fetcher: OpenPRRefFetcher | None = None,
    now: datetime | None = None,
) -> list[IssueRanking]:
    """Return open issues ranked by score, descending.

    `fetcher` is injectable — production passes `fetch_via_gh` by default;
    tests substitute a deterministic list. The cascade calls this with no
    args to get the live ranking.

    `pr_ref_fetcher` returns the open-PR overlap signals (`OpenPRRefs`).
    Production passes `fetch_open_pr_issue_refs_via_gh`; tests inject
    empty/explicit values. Two skip channels:

    - issue number in `closing_refs` → `_SKIP_REASON_CLOSING_REF` ("open
      PR already addresses this issue"). The issue #10 §bug-3 fix: the
      cascade used to re-pick #93 forever after fix PR #95 was open
      because the ranker had no in-flight-PR guard.
    - issue number in `nightly_mention_refs` → `_SKIP_REASON_NIGHTLY_MENTION`
      ("open Nightly PR references this issue (in-flight)"). The issue
      #27 fix: an epic referenced (without a closing keyword) by Nightly
      per-item PRs re-picked forever; a bare `#N` mention in a `nightly/*`
      PR now counts as in-flight.

    For backward compatibility a `pr_ref_fetcher` returning a bare
    `frozenset[int]` (the pre-#27 shape) is coerced to `closing_refs`.
    """
    root = (root or repo_root()).resolve()
    fetch = fetcher or fetch_via_gh
    pr_refs_fn = pr_ref_fetcher or fetch_open_pr_issue_refs_via_gh
    issues = fetch(root)
    open_pr_refs = _coerce_open_pr_refs(pr_refs_fn(root))
    rankings = [
        IssueRanking(
            issue=issue,
            score=score_issue(issue, now=now),
            skip_reason=_skip_reason(issue, open_pr_refs=open_pr_refs),
        )
        for issue in issues
    ]
    # Eligible first (ordered by score desc), then skip-eligible at the
    # bottom (preserves their score for display but they never get picked).
    eligible = sorted(
        (r for r in rankings if r.skip_reason is None),
        key=lambda r: r.score,
        reverse=True,
    )
    skipped = sorted(
        (r for r in rankings if r.skip_reason is not None),
        key=lambda r: r.score,
        reverse=True,
    )
    return [*eligible, *skipped]
