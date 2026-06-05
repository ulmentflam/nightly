"""Plan files: YAML-frontmatter parsing, status lifecycle, listing helpers.

A "plan" is a `plan.md` inside a per-task directory:
`.nightly/runs/<run-id>/tasks/<n>-<slug>/plan.md`. Every plan begins with a
small YAML frontmatter block carrying its status, so the priority cascade
can find it without re-reading the body each time.

Statuses (the lifecycle the cascade cares about):
- `ready`            — created, not yet started
- `in_progress`      — the agent is actively working on it
- `blocked: approval`— a refused op required; waiting for `nightly approve`
- `done`             — landed (PR opened or proposal written) and verified
- `parked`           — stashed mid-task (drain, ambiguity, manual pause)

We avoid a YAML dependency because plan frontmatter is intentionally flat
(key: value, no nested objects). A small purpose-built parser keeps the
core lightweight and forgiving — missing fields default sensibly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from nightly_core.paths import runs_dir

__all__ = [
    "DEPENDS_ON_PR_KEY",
    "PLAN_STATUSES",
    "PROPOSER_FINGERPRINT_KEY",
    "PR_LAST_RECONCILED_KEY",
    "PlanRecord",
    "PlanStatus",
    "append_pr_feedback",
    "find_rfc_status",
    "list_plans",
    "parse_frontmatter",
    "read_plan",
    "render_frontmatter",
    "update_plan_status",
]


PR_LAST_RECONCILED_KEY = "pr_last_reconciled_at"
"""Frontmatter key recording the most recent PR-feedback reconciliation.
Used by the cascade's `pick_pr_rescue` to skip plans whose PR has had
no new feedback since the timestamp."""

PROPOSER_FINGERPRINT_KEY = "proposer_fingerprint"
"""Frontmatter key recording the originating proposal's stable identity.
Populated by the driver when a plan is materialized from `ideate` or
`ideate_fallback`. The cascade dedupes future proposals by matching
against this value — see `Proposal.fingerprint` (proposers/base.py) and
issue #2 for the failure mode this addresses."""

DEPENDS_ON_PR_KEY = "depends_on_pr"
"""Frontmatter key declaring that a plan's worktree must branch from an
open Nightly PR's head ref rather than `main`. When set to a PR number
(int, or string with optional `#` prefix), the driver resolves the PR
via `gh pr view <N> --json headRefName,state` and bases the worktree on
its branch — preserving cross-task dependencies that would otherwise
produce a conflicted diff at CI time. Without the field, the driver
forces branch-from-`main`. See RFC 004 for the prevention-by-default
semantics; bias is toward false negatives (omitted declarations →
conflicts surface at CI) over false positives (spurious declarations →
stacked PRs the operator must review)."""


PlanStatus = Literal[
    "ready",
    "in_progress",
    "dispatching",
    "blocked: approval",
    "done",
    "parked",
]

PLAN_STATUSES: tuple[PlanStatus, ...] = (
    "ready",
    "in_progress",
    "dispatching",
    "blocked: approval",
    "done",
    "parked",
)


@dataclass(frozen=True)
class PlanRecord:
    """A single plan, parsed from disk.

    `metadata` is the raw frontmatter dict (string keys, string values); the
    typed accessors below (`status`, `slug`, `created`) pull commonly-needed
    fields out with sensible fallbacks.
    """

    path: Path
    """Absolute path to plan.md."""

    metadata: dict[str, str]
    """Raw frontmatter (string key/value). Empty if the file has no frontmatter."""

    body: str
    """Everything after the closing `---` fence."""

    @property
    def run_id(self) -> str:
        """The run id, derived from the directory layout: runs/<id>/tasks/<slug>/plan.md."""
        # path layout: <root>/.nightly/runs/<run-id>/tasks/<slug>/plan.md
        return self.path.parent.parent.parent.name

    @property
    def slug(self) -> str:
        """Task slug, e.g. `0001-fix-login`. Derived from the parent dir name."""
        return self.metadata.get("slug") or self.path.parent.name

    @property
    def status(self) -> PlanStatus:
        """Current status. Pre-frontmatter plans are treated as `ready`."""
        raw = self.metadata.get("status", "ready").strip()
        if raw in PLAN_STATUSES:
            return raw  # type: ignore[return-value]
        return "ready"

    @property
    def approval_granted(self) -> bool:
        """True if a previously-blocked plan has had its approval recorded."""
        return self.metadata.get("approval_granted", "").lower() in {"true", "yes", "1"}

    @property
    def proposer_fingerprint(self) -> str | None:
        """The proposal fingerprint that originated this plan, if any.

        Hand-authored plans (created via `nightly task` rather than the
        ideation cascade) leave this field empty — returning None lets
        the cascade-dedupe filter treat them as "not from a proposer,
        not eligible for dedupe."
        """
        value = self.metadata.get(PROPOSER_FINGERPRINT_KEY, "").strip()
        return value or None

    @property
    def depends_on_pr(self) -> int | None:
        """The open Nightly PR this plan declares a dependency on, if any.

        Accepts a bare integer (`54`) or a hash-prefixed number (`#54`);
        anything else returns None. Cascade and dispatch treat None as
        "no declared dependency → branch from `main`."
        """
        raw = self.metadata.get(DEPENDS_ON_PR_KEY, "").strip().lstrip("#").strip()
        if not raw:
            return None
        try:
            number = int(raw)
        except ValueError:
            return None
        return number if number > 0 else None


# ── frontmatter parsing ───────────────────────────────────────────────────


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a markdown document into (metadata, body).

    Recognises a leading `---` fence, a body of `key: value` lines, and a
    closing `---` fence. Returns `({}, text)` if no frontmatter is present.

    Values are kept verbatim (incl. embedded colons — useful for statuses
    like `blocked: approval` and ISO timestamps like `2026-05-20T22:14:03Z`).
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    header = text[4:end]
    body = text[end + 5 :]
    metadata: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip()
    return metadata, body


_STATUS_LINE_RE = re.compile(
    r"^\s*(?:\*\*\s*)?status\s*(?:\*\*)?\s*:\s*(?:\*\*\s*)?([^\s*\n][^*\n]*?)(?:\s*\*\*)?\s*$",
    re.IGNORECASE,
)
"""Match a `Status: <value>` body line with tolerance for markdown bold
decoration around either the key or the value.

Cases handled:
  - `status: accepted`
  - `Status: accepted`
  - `**Status**: accepted`
  - `**Status:** accepted`
  - `Status: **accepted**`
  - `**Status**: **accepted**`

Anything past the value (a trailing comment, punctuation) is captured
verbatim; callers `.strip()` before comparing. The regex is anchored
at line boundaries; multi-line scanning is the caller's job (we only
search the head of the document to avoid matching `Status:`-shaped
text deep in the body)."""

_STATUS_BODY_SCAN_LINES = 50
"""How many leading lines of the document `find_rfc_status` scans
when no frontmatter status is present. 50 is generous enough to find
the directive in any RFC convention (corpus-forge puts theirs in the
first 5 lines; we just want to avoid scanning a 1000-line RFC body
looking for `Status: accepted` matches in code samples or quoted
text further down)."""


def find_rfc_status(text: str) -> str | None:
    """Best-effort RFC status extractor (issue #10 §1).

    Two-tier lookup:
    1. **Frontmatter path.** `parse_frontmatter` runs first; any key
       that normalizes to `status` (after stripping markdown bold
       decoration and case-folding) yields its value. Tolerates the
       `**Status**: accepted` shape that corpus-forge's RFCs used
       inside `---` fences.
    2. **Body fallback.** When the frontmatter pass returns nothing,
       scan the first `_STATUS_BODY_SCAN_LINES` lines for a
       `Status: <value>` directive line via `_STATUS_LINE_RE`.
       Catches the corpus-forge convention of `Status: accepted`
       written as a bare body line outside any frontmatter fence —
       the case that left the cascade blind to 4 P0 RFCs all night
       (issue #10's regression).

    Returns the raw status string (stripped) or None.
    """
    metadata, _body = parse_frontmatter(text)
    for key, value in metadata.items():
        normalized = key.strip().strip("*").strip().lower()
        if normalized == "status" and value:
            return value.strip()
    for line in text.splitlines()[:_STATUS_BODY_SCAN_LINES]:
        match = _STATUS_LINE_RE.match(line)
        if match:
            return match.group(1).strip()
    return None


def render_frontmatter(metadata: dict[str, str], body: str) -> str:
    """Inverse of `parse_frontmatter`. Always emits a leading + closing fence.

    The body is concatenated verbatim after the closing `---\n` — callers
    decide whether they want a blank line between the frontmatter and the
    body content.
    """
    header_lines = [f"{k}: {v}" for k, v in metadata.items()]
    header = "\n".join(header_lines)
    return f"---\n{header}\n---\n{body}"


# ── reading & updating ────────────────────────────────────────────────────


def read_plan(path: Path) -> PlanRecord:
    """Load `plan.md` from `path` (a file path, not a directory)."""
    if not path.is_file():
        msg = f"plan not found: {path}"
        raise FileNotFoundError(msg)
    text = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    return PlanRecord(path=path, metadata=metadata, body=body)


def update_plan_status(
    path: Path,
    new_status: PlanStatus,
    *,
    approval_granted: bool | None = None,
) -> PlanRecord:
    """Rewrite `plan.md` with a new status (and optionally toggle approval).

    Preserves all other frontmatter fields and the body verbatim. Always
    updates the `updated` timestamp.
    """
    plan = read_plan(path)
    metadata = dict(plan.metadata)
    metadata["status"] = new_status
    metadata["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if approval_granted is not None:
        metadata["approval_granted"] = "true" if approval_granted else "false"
    path.write_text(render_frontmatter(metadata, plan.body), encoding="utf-8")
    return PlanRecord(path=path, metadata=metadata, body=plan.body)


# ── listing across runs ───────────────────────────────────────────────────


def append_pr_feedback(
    path: Path,
    feedback: list,
    *,
    now: datetime | None = None,
) -> PlanRecord:
    """Append a `## Feedback round N` section to the plan body and stamp
    `pr_last_reconciled_at` in the frontmatter.

    Idempotency: each call adds a new section labeled with the next round
    number (counted by scanning the body for prior `## Feedback round`
    headings). Body and other frontmatter fields are preserved.

    `feedback` is `list[PRFeedback]` — typed loosely to avoid a circular
    import; the cascade passes the typed list.
    """
    plan = read_plan(path)
    moment = now or datetime.now(UTC)

    # Count existing feedback rounds in the body.
    prior_rounds = len(re.findall(r"^## Feedback round \d+", plan.body, re.MULTILINE))
    round_n = prior_rounds + 1

    lines: list[str] = []
    lines.append("")
    lines.append(f"## Feedback round {round_n}")
    lines.append("")
    lines.append(f"*Collected {moment.strftime('%Y-%m-%d %H:%M UTC')}.*")
    lines.append("")
    if not feedback:
        lines.append("_(no feedback returned)_")
    else:
        # Group: blocking first, then humans, then bots.
        blocking = [f for f in feedback if getattr(f, "is_blocking", False)]
        non_blocking_humans = [
            f for f in feedback if not getattr(f, "is_blocking", False) and not f.author_is_bot
        ]
        bots = [f for f in feedback if not getattr(f, "is_blocking", False) and f.author_is_bot]
        for label, group in (
            ("Blocking", blocking),
            ("Human reviewers", non_blocking_humans),
            ("Bot reviewers", bots),
        ):
            if not group:
                continue
            lines.append(f"### {label}")
            lines.append("")
            for f in group:
                head_bits = [f"**{f.author_login}**"]
                if f.kind == "review" and f.state:
                    head_bits.append(f"({f.state.lower()})")
                elif f.kind == "check_failure":
                    head_bits.append(f"(check: {f.state})")
                if f.file_ref:
                    locator = f.file_ref
                    if f.line_ref:
                        locator = f"{f.file_ref}:{f.line_ref}"
                    head_bits.append(f"on `{locator}`")
                lines.append(f"- {' '.join(head_bits)}")
                # Indent quoted body.
                for body_line in f.body.splitlines() or [""]:
                    lines.append(f"  > {body_line}")
                lines.append(f"  · [link]({f.url})")
            lines.append("")

    new_body = plan.body.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n"
    new_metadata = dict(plan.metadata)
    new_metadata[PR_LAST_RECONCILED_KEY] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    new_metadata["updated"] = new_metadata[PR_LAST_RECONCILED_KEY]
    path.write_text(render_frontmatter(new_metadata, new_body), encoding="utf-8")
    return PlanRecord(path=path, metadata=new_metadata, body=new_body)


def list_plans(root: Path | None = None) -> list[PlanRecord]:
    """Return every plan across every run, oldest first.

    Walks `.nightly/runs/*/tasks/*/plan.md`. Skips files that don't exist or
    fail to parse — listing should never raise.
    """
    runs_root = runs_dir(root)
    if not runs_root.is_dir():
        return []
    out: list[PlanRecord] = []
    for run_dir_entry in sorted(runs_root.iterdir()):
        if not run_dir_entry.is_dir():
            continue
        tasks = run_dir_entry / "tasks"
        if not tasks.is_dir():
            continue
        for task in sorted(tasks.iterdir()):
            plan = task / "plan.md"
            if not plan.is_file():
                continue
            try:
                out.append(read_plan(plan))
            except (OSError, FileNotFoundError):
                continue
    return out
