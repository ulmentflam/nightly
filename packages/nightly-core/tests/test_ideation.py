"""Tests for the ideation orchestrator."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from nightly_core.ideation import run_proposers, top_auto_pr, write_drafts
from nightly_core.plans import parse_frontmatter
from nightly_core.proposers.base import Proposal, Proposer
from nightly_core.runs import start_run


class _StaticProposer(Proposer):
    """Test double — yields a fixed list of proposals."""

    def __init__(self, pid: str, proposals: list[Proposal]) -> None:
        self.id = pid
        self._proposals = proposals

    def propose(self, root: Path) -> Iterable[Proposal]:
        return list(self._proposals)


class _BrokenProposer(Proposer):
    id = "broken"

    def propose(self, root: Path) -> Iterable[Proposal]:
        raise RuntimeError("simulated failure")


def _proposal(
    title: str,
    *,
    score: float = 1.0,
    category: str = "lint_debt",
    file_scope: tuple[str, ...] = ("src/a.py",),
    estimated_loc: int = 5,
) -> Proposal:
    return Proposal(
        proposer="x",
        category=category,  # type: ignore[arg-type]
        title=title,
        body=f"# {title}\n\nbody",
        score=score,
        file_scope=file_scope,
        estimated_loc=estimated_loc,
    )


# ── run_proposers ─────────────────────────────────────────────────────────


def test_run_proposers_merges_and_sorts_by_score(tmp_path: Path) -> None:
    p1 = _StaticProposer("p1", [_proposal("low", score=1.0)])
    p2 = _StaticProposer("p2", [_proposal("high", score=4.0), _proposal("mid", score=2.5)])
    out = run_proposers(tmp_path, proposers=[p1, p2])
    assert [p.title for p in out] == ["high", "mid", "low"]


def test_run_proposers_swallows_individual_failures(tmp_path: Path) -> None:
    """One broken proposer must not break the rest."""
    p_ok = _StaticProposer("ok", [_proposal("survivor", score=2.0)])
    out = run_proposers(tmp_path, proposers=[_BrokenProposer(), p_ok])
    assert [p.title for p in out] == ["survivor"]


def test_run_proposers_empty_when_no_one_yields(tmp_path: Path) -> None:
    out = run_proposers(tmp_path, proposers=[_StaticProposer("empty", [])])
    assert out == []


# ── write_drafts ──────────────────────────────────────────────────────────


def test_write_drafts_creates_numbered_files(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    proposals = [
        _proposal("first highest", score=3.0),
        _proposal("second", score=2.0),
        _proposal("third", score=1.0),
    ]
    paths = write_drafts(run, proposals)
    assert len(paths) == 3
    issues_dir = run.path / "proposed" / "issues"
    files = sorted(issues_dir.glob("*.md"))
    # Files numbered 001/002/003 in score order
    assert files[0].name.startswith("001-")
    assert files[1].name.startswith("002-")
    assert files[2].name.startswith("003-")


def test_write_drafts_writes_frontmatter_with_autonomy_verdict(tmp_path: Path) -> None:
    run = start_run(tmp_path)
    paths = write_drafts(
        run,
        [
            _proposal("lint clean", category="lint_debt", estimated_loc=5),
            _proposal(
                "todo audit",
                category="todo_audit",
                file_scope=("a.py", "b.py", "c.py"),
                estimated_loc=20,
            ),
        ],
    )

    text_a = paths[0].read_text(encoding="utf-8")
    meta_a, _ = parse_frontmatter(text_a)
    assert meta_a["proposer"] == "x"
    assert meta_a["category"] == "lint_debt"
    assert meta_a["auto_pr_eligible"] == "true"
    assert "auto_pr_rejection" not in meta_a

    text_b = paths[1].read_text(encoding="utf-8")
    meta_b, _ = parse_frontmatter(text_b)
    assert meta_b["auto_pr_eligible"] == "false"
    assert "category" in meta_b["auto_pr_rejection"]  # rejection reason recorded


def test_write_drafts_clears_old_numbered_drafts(tmp_path: Path) -> None:
    """Re-running ideation should refresh, not pile up."""
    run = start_run(tmp_path)
    first = write_drafts(run, [_proposal(f"v1-{i}") for i in range(3)])
    assert all(p.exists() for p in first)

    second = write_drafts(run, [_proposal("only one")])
    assert len(second) == 1
    issues_dir = run.path / "proposed" / "issues"
    leftover = sorted(issues_dir.glob("[0-9][0-9][0-9]-*.md"))
    # All three v1 files were cleared; just the one new file remains.
    assert len(leftover) == 1
    assert leftover[0].name.startswith("001-only-one")


def test_write_drafts_preserves_non_numbered_files(tmp_path: Path) -> None:
    """A human-added human-notes.md shouldn't get deleted by ideation."""
    run = start_run(tmp_path)
    issues_dir = run.path / "proposed" / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    human_note = issues_dir / "human-notes.md"
    human_note.write_text("# Keep me\n", encoding="utf-8")

    write_drafts(run, [_proposal("auto")])
    assert human_note.exists()
    assert human_note.read_text(encoding="utf-8") == "# Keep me\n"


# ── top_auto_pr ───────────────────────────────────────────────────────────


def test_top_auto_pr_returns_none_when_nothing_eligible() -> None:
    assert top_auto_pr([_proposal("t", category="todo_audit")]) is None


def test_top_auto_pr_returns_highest_score_among_eligible() -> None:
    candidates = [
        _proposal("low lint", category="lint_debt", score=1.0, estimated_loc=5),
        _proposal("high lint", category="lint_debt", score=4.5, estimated_loc=10),
        _proposal(
            "huge lint",
            category="lint_debt",
            score=5.0,
            estimated_loc=200,  # too big — bar rejects
        ),
        _proposal("todo", category="todo_audit", score=5.0),
    ]
    pick = top_auto_pr(candidates)
    assert pick is not None
    assert pick.title == "high lint"
