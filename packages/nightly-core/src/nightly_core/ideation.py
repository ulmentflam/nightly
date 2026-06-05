"""Ideation orchestrator — run proposers, write drafts, surface auto-PRs.

`run_proposers(root)` invokes every registered proposer (or an injected
subset), merges their output, and returns the combined list sorted by
score descending. A misbehaving proposer cannot break the run — its
exceptions are caught and that proposer simply yields nothing.

`write_drafts(run, proposals)` persists each proposal as a markdown file
under `<run>/proposed/issues/<NNN>-<slug>.md`, with YAML frontmatter
carrying score, category, file scope, estimated LOC, and the autonomy
bar's verdict. The morning briefing reads these files back.

`top_auto_pr(proposals)` returns the highest-scoring proposal that clears
the autonomy bar (or `None`). The cascade calls this on its `ideate`
step.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nightly_core.autonomy import auto_pr_rejection_reason, can_auto_pr
from nightly_core.plans import render_frontmatter
from nightly_core.proposers.base import Proposal, Proposer
from nightly_core.proposers.registry import default_proposers
from nightly_core.runs import Run

__all__ = ["run_proposers", "top_auto_pr", "write_drafts"]


_log = logging.getLogger(__name__)


def run_proposers(
    root: Path,
    proposers: list[Proposer] | None = None,
    *,
    force_synthesis: bool = False,
) -> list[Proposal]:
    """Run every proposer; merge + sort their output by score (desc).

    `proposers` is injectable for tests. None ⇒ use `default_proposers()`.
    A proposer that raises is logged and skipped; the rest still run.

    `force_synthesis=True` (RFC 009 §C2) propagates to the default
    proposer registry, which constructs the `SynthesisProposer` with
    `force=True` so its `synthesis.json` cache lookup is bypassed.
    Ignored when `proposers` is explicitly passed — callers controlling
    the proposer list also control their own force behavior.
    """
    chosen = (
        proposers if proposers is not None else default_proposers(force_synthesis=force_synthesis)
    )
    out: list[Proposal] = []
    for proposer in chosen:
        try:
            out.extend(list(proposer.propose(root)))
        except Exception as exc:
            _log.warning("proposer %s failed: %s", proposer.id, exc)
            continue
    out.sort(key=lambda p: p.score, reverse=True)
    return out


def write_drafts(run: Run, proposals: list[Proposal]) -> list[Path]:
    """Persist each proposal under `<run>/proposed/issues/<NNN>-<slug>.md`.

    Returns the list of paths written, in the same order as `proposals`
    (which is already score-sorted). Existing files for the same rank are
    overwritten — re-running `nightly ideate` refreshes the drafts.
    """
    issues_dir = run.path / "proposed" / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)

    # Clear any prior numbered drafts so stale ones don't linger after a
    # re-ideation. We leave non-numbered .md files alone (humans may add).
    for existing in issues_dir.glob("[0-9][0-9][0-9]-*.md"):
        existing.unlink()

    written: list[Path] = []
    for idx, proposal in enumerate(proposals, start=1):
        path = issues_dir / f"{idx:03d}-{proposal.slug}.md"
        metadata = {
            "proposer": proposal.proposer,
            "category": proposal.category,
            "strategic_category": proposal.strategic_category,
            "score": f"{proposal.score:.3f}",
            "estimated_loc": str(proposal.estimated_loc),
            "file_scope": ", ".join(proposal.file_scope) or "(none)",
            "auto_pr_eligible": "true" if can_auto_pr(proposal) else "false",
        }
        reason = auto_pr_rejection_reason(proposal)
        if reason is not None:
            metadata["auto_pr_rejection"] = reason

        body = f"\n# {proposal.title}\n\n{proposal.body.rstrip()}\n"
        path.write_text(render_frontmatter(metadata, body), encoding="utf-8")
        written.append(path)
    return written


def top_auto_pr(proposals: list[Proposal]) -> Proposal | None:
    """Highest-scoring proposal that clears the autonomy bar (or None)."""
    eligible = [p for p in proposals if can_auto_pr(p)]
    if not eligible:
        return None
    return max(eligible, key=lambda p: p.score)
