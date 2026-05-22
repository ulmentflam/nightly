"""`nightly ci` — poll CI status across open Nightly PRs.

The point of this module is *not* to block: Nightly's contract is
monotonic forward progress. We never wait synchronously for CI. We
read the current status, surface it, and let the cascade do the rest
— a failed check naturally bubbles into `pick_pr_rescue` because
check-failures are already a `PRFeedback` kind.

`nightly ci` is meant for the agent to glance at between tasks. The
agent's loop is roughly:

    while session_armed:
        next_task = nightly next
        execute(next_task)
        nightly ci         # if anything red, the next `nightly next`
                            # routes to pr_rescue automatically

We do not poll continuously — that's the host's Stop hook's job. This
module just renders a snapshot when the agent (or operator) asks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nightly_core.cascade import _nightly_open_pr_branches

__all__ = [
    "CHECK_STATUS_RANK",
    "CICheck",
    "PRCIStatus",
    "fetch_pr_checks",
    "list_ci_status",
    "summarize_status",
]


CheckBucket = Literal["pass", "fail", "pending", "skipping", "cancel", "unknown"]


@dataclass(frozen=True)
class CICheck:
    """One CI check on one PR — name, current state, conclusion."""

    name: str
    bucket: CheckBucket
    state: str  # raw "state" from gh: SUCCESS / FAILURE / IN_PROGRESS / QUEUED / …
    workflow: str = ""
    url: str = ""


# Order matters: when computing overall PR status we pick the worst
# bucket present. `fail` > `pending` > `pass` etc. Lower index = worse.
CHECK_STATUS_RANK: tuple[CheckBucket, ...] = (
    "fail",
    "cancel",
    "pending",
    "unknown",
    "skipping",
    "pass",
)


@dataclass(frozen=True)
class PRCIStatus:
    """Aggregate CI status for one open Nightly PR."""

    branch: str
    pr_number: int
    pr_url: str
    overall: CheckBucket
    checks: tuple[CICheck, ...] = field(default_factory=tuple)

    @property
    def is_failing(self) -> bool:
        return self.overall in ("fail", "cancel")

    @property
    def is_pending(self) -> bool:
        return self.overall == "pending"

    @property
    def failed_checks(self) -> tuple[CICheck, ...]:
        return tuple(c for c in self.checks if c.bucket in ("fail", "cancel"))


# ── gh adapter ────────────────────────────────────────────────────────────


def _bucket_for(state: str, conclusion: str) -> CheckBucket:
    """Map gh's `state`/`conclusion` strings to one of our six buckets."""
    s = (state or "").lower()
    c = (conclusion or "").lower()
    if s in {"in_progress", "queued", "pending", "waiting"}:
        return "pending"
    if c in {"success"}:
        return "pass"
    if c in {"failure", "timed_out", "action_required", "stale", "startup_failure"}:
        return "fail"
    if c in {"cancelled"}:
        return "cancel"
    if c in {"neutral", "skipped"}:
        return "skipping"
    return "unknown"


def fetch_pr_checks(
    branch: str,
    root: Path | None = None,
) -> tuple[CICheck, ...]:
    """Shell out to `gh pr checks <branch> --json …` and return parsed rows.

    Returns `()` when `gh` is missing, the call fails, or the PR has
    zero checks (a PR with no required workflows is silently zero).
    Best-effort throughout — CI inspection must never raise.
    """
    if shutil.which("gh") is None:
        return ()
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "checks",
                branch,
                "--json",
                "name,state,bucket,workflow,link",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ()
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ()
    out: list[CICheck] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not name:
            continue
        bucket_raw = str(row.get("bucket") or "").lower()
        state = str(row.get("state") or "")
        bucket: CheckBucket = (
            bucket_raw  # type: ignore[assignment]
            if bucket_raw in {"pass", "fail", "pending", "skipping", "cancel"}
            else _bucket_for(state, state)
        )
        out.append(
            CICheck(
                name=name,
                bucket=bucket,
                state=state,
                workflow=str(row.get("workflow") or ""),
                url=str(row.get("link") or ""),
            )
        )
    return tuple(out)


# ── aggregation ───────────────────────────────────────────────────────────


def summarize_status(checks: tuple[CICheck, ...]) -> CheckBucket:
    """Reduce per-check buckets to one overall bucket via `CHECK_STATUS_RANK`.

    Empty input is `unknown` — a PR with no checks reports no signal,
    not a passing-by-default. The cascade still treats `unknown` as
    non-blocking.
    """
    if not checks:
        return "unknown"
    present = {c.bucket for c in checks}
    for bucket in CHECK_STATUS_RANK:
        if bucket in present:
            return bucket
    return "unknown"


def list_ci_status(root: Path | None = None) -> list[PRCIStatus]:
    """Return CI status for every open Nightly PR in `root`.

    Uses the same `_nightly_open_pr_branches` helper the cascade's
    `pick_pr_rescue` uses, so the set of inspected PRs is exactly the
    set the cascade can react to.
    """
    branches = _nightly_open_pr_branches(root)
    if not branches:
        return []
    out: list[PRCIStatus] = []
    for branch, number, url in branches:
        checks = fetch_pr_checks(branch, root=root)
        out.append(
            PRCIStatus(
                branch=branch,
                pr_number=number,
                pr_url=url,
                overall=summarize_status(checks),
                checks=checks,
            )
        )
    return out
