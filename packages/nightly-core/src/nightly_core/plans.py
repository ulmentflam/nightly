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

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from nightly_core.paths import runs_dir

__all__ = [
    "PLAN_STATUSES",
    "PlanRecord",
    "PlanStatus",
    "list_plans",
    "parse_frontmatter",
    "read_plan",
    "render_frontmatter",
    "update_plan_status",
]


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
