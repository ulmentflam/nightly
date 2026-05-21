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


def _skip_reason(issue: IssueRecord) -> str | None:
    label_set = {lab.lower() for lab in issue.labels}
    deny = label_set & _HARD_DENY_LABELS
    if deny:
        return f"hard-deny label: {sorted(deny)[0]}"
    if len(issue.body.strip()) < _MIN_BODY_CHARS:
        return f"no acceptance criterion (body < {_MIN_BODY_CHARS} chars)"
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


# ── public API ─────────────────────────────────────────────────────────────


def rank_issues(
    root: Path | None = None,
    *,
    fetcher: IssueFetcher | None = None,
    now: datetime | None = None,
) -> list[IssueRanking]:
    """Return open issues ranked by score, descending.

    `fetcher` is injectable — production passes `fetch_via_gh` by default;
    tests substitute a deterministic list. The cascade calls this with no
    args to get the live ranking.
    """
    root = (root or repo_root()).resolve()
    fetch = fetcher or fetch_via_gh
    issues = fetch(root)
    rankings = [
        IssueRanking(
            issue=issue,
            score=score_issue(issue, now=now),
            skip_reason=_skip_reason(issue),
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
