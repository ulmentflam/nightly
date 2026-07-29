"""`nightly worktree prune` — the counterpart `worktree create` never had.

Nightly made a worktree per task and never removed one. A real repo held
five stale checkouts at ~93MB each, the oldest two months old, all fully
merged and none of them surfaced by anything.

The safety rule is the whole feature: a worktree is removed only when
losing it cannot lose work. So these tests are mostly about what is
*kept*, and about the failure direction — an over-eager prune destroys
work, a timid one wastes disk, and only the first is unrecoverable.

The git runner is faked. What is under test is the decision, not git.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nightly_core.worktree import (
    WorktreeHandle,
    assess_worktree,
    prune_worktrees,
)


class FakeGit:
    """Records invocations and replies from a scripted table."""

    def __init__(self, replies: dict[str, tuple[str, int]] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[list[str]] = []

    async def __call__(self, args: Sequence[str], cwd: Path | None) -> tuple[bytes, bytes, int]:
        self.calls.append(list(args))
        for key, (out, code) in self.replies.items():
            if key in " ".join(args):
                return out.encode(), b"", code
        return b"", b"", 0

    def ran(self, fragment: str) -> bool:
        return any(fragment in " ".join(c) for c in self.calls)


def _handle(tmp_path: Path, branch: str = "nightly/spent") -> WorktreeHandle:
    p = tmp_path / "wt"
    p.mkdir(exist_ok=True)
    return WorktreeHandle(path=p, branch=branch, base_branch="main", created_at=datetime.now(UTC))


def _porcelain(path: Path, branch: str) -> str:
    return f"worktree {path}\nHEAD abc123\nbranch refs/heads/{branch}\n\n"


# ── the decision ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clean_and_merged_is_disposable(tmp_path: Path) -> None:
    git = FakeGit({"rev-list": ("0", 0), "status": ("", 0)})
    v = await assess_worktree(_handle(tmp_path), root=tmp_path, runner=git)
    assert v.disposable
    assert v.blockers == ()


@pytest.mark.asyncio
async def test_uncommitted_changes_block(tmp_path: Path) -> None:
    """The unrecoverable case. Everything else can be redone."""
    git = FakeGit({"rev-list": ("0", 0), "status": (" M a.py\n?? b.py", 0)})
    v = await assess_worktree(_handle(tmp_path), root=tmp_path, runner=git)
    assert not v.disposable
    assert "2 uncommitted change(s)" in v.blockers


@pytest.mark.asyncio
async def test_unmerged_commits_block(tmp_path: Path) -> None:
    git = FakeGit({"rev-list": ("3", 0), "status": ("", 0)})
    v = await assess_worktree(_handle(tmp_path), root=tmp_path, runner=git)
    assert "3 commit(s) not in `main`" in v.blockers


@pytest.mark.asyncio
async def test_the_base_branch_is_configurable(tmp_path: Path) -> None:
    """A repo whose trunk is not `main` must not have every worktree read
    as unmerged."""
    git = FakeGit({"rev-list": ("0", 0)})
    await assess_worktree(_handle(tmp_path), root=tmp_path, base_branch="develop", runner=git)
    assert git.ran("develop..nightly/spent")


# ── "I could not tell" must never read as "nothing to lose" ───────────────


@pytest.mark.asyncio
async def test_a_failed_status_check_blocks(tmp_path: Path) -> None:
    git = FakeGit({"status": ("", 128), "rev-list": ("0", 0)})
    v = await assess_worktree(_handle(tmp_path), root=tmp_path, runner=git)
    assert "could not read working-tree status" in v.blockers


@pytest.mark.asyncio
async def test_a_failed_rev_list_blocks(tmp_path: Path) -> None:
    """Unknown base, detached HEAD, corrupt ref — all mean 'keep'."""
    git = FakeGit({"rev-list": ("", 128), "status": ("", 0)})
    v = await assess_worktree(_handle(tmp_path), root=tmp_path, runner=git)
    assert any("could not compare" in b for b in v.blockers)


@pytest.mark.asyncio
async def test_non_numeric_rev_list_output_blocks(tmp_path: Path) -> None:
    """A zero exit with garbage on stdout must not parse as 'zero commits'."""
    git = FakeGit({"rev-list": ("fatal: bad revision", 0), "status": ("", 0)})
    v = await assess_worktree(_handle(tmp_path), root=tmp_path, runner=git)
    assert any("could not compare" in b for b in v.blockers)


# ── the worktree you are standing in ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_current_worktree_is_never_disposable(tmp_path: Path) -> None:
    h = _handle(tmp_path)
    git = FakeGit({"rev-list": ("0", 0), "status": ("", 0)})
    v = await assess_worktree(h, root=tmp_path, current=h.path, runner=git)
    assert "this is the current worktree" in v.blockers


@pytest.mark.asyncio
async def test_force_does_not_remove_the_current_worktree(tmp_path: Path) -> None:
    """`--force` overrides caution, not impossibility: git cannot remove
    the worktree the process is standing in."""
    h = _handle(tmp_path)
    git = FakeGit({"worktree list": (_porcelain(h.path, h.branch), 0), "rev-list": ("0", 0)})
    report = await prune_worktrees(tmp_path, force=True, current=h.path, runner=git, dry_run=False)
    assert report.removed == ()
    assert len(report.kept) == 1
    assert not git.ran("worktree remove")


# ── missing paths ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_vanished_path_is_disposable_metadata(tmp_path: Path) -> None:
    """Deleted by hand, or on a detached volume. Nothing left to lose."""
    gone = WorktreeHandle(
        path=tmp_path / "not-there",
        branch="nightly/gone",
        base_branch="main",
        created_at=datetime.now(UTC),
    )
    v = await assess_worktree(gone, root=tmp_path, runner=FakeGit())
    assert v.disposable
    assert v.missing


# ── the sweep ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    h = _handle(tmp_path)
    git = FakeGit({"worktree list": (_porcelain(h.path, h.branch), 0), "rev-list": ("0", 0)})
    report = await prune_worktrees(tmp_path, dry_run=True, current=tmp_path, runner=git)
    assert len(report.removed) == 1
    assert report.dry_run
    assert not git.ran("worktree remove")
    assert not git.ran("branch -D")


@pytest.mark.asyncio
async def test_a_merged_worktree_is_removed_with_its_branch(tmp_path: Path) -> None:
    """Leaving the branch would trade stale worktrees for stale branches —
    the same residue in another form."""
    h = _handle(tmp_path)
    git = FakeGit({"worktree list": (_porcelain(h.path, h.branch), 0), "rev-list": ("0", 0)})
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert len(report.removed) == 1
    assert git.ran("worktree remove")
    assert git.ran(f"branch -D {h.branch}")


@pytest.mark.asyncio
async def test_keep_branches_removes_only_the_worktree(tmp_path: Path) -> None:
    h = _handle(tmp_path)
    git = FakeGit({"worktree list": (_porcelain(h.path, h.branch), 0), "rev-list": ("0", 0)})
    await prune_worktrees(tmp_path, delete_branch=False, current=tmp_path, runner=git)
    assert git.ran("worktree remove")
    assert not git.ran("branch -D")


# ── squash merges ─────────────────────────────────────────────────────────
#
# The first cut of this command shipped with a strict ancestry check and
# immediately refused to clean up its own worktree: the PR had been
# squash-merged, so the branch's commit was not an ancestor of `main`. In
# a repo that squash-merges every PR — this one — that is every worktree,
# which is precisely the accumulation the command exists to stop.


@pytest.mark.asyncio
async def test_a_squash_merged_worktree_is_removed(tmp_path: Path) -> None:
    """Ancestry says unmerged; the deleted remote ref says the PR landed."""
    h = _handle(tmp_path)
    git = FakeGit(
        {
            "worktree list": (_porcelain(h.path, h.branch), 0),
            "rev-list": ("1", 0),
            "for-each-ref": ("[gone]", 0),
        }
    )
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert len(report.removed) == 1
    assert git.ran("worktree remove")


@pytest.mark.asyncio
async def test_a_squash_merged_branch_is_kept(tmp_path: Path) -> None:
    """A deleted remote ref is weaker evidence than ancestry, so it buys
    the directory and not the history. Were the PR closed rather than
    merged, deleting the ref would strand the only copy of the commits."""
    h = _handle(tmp_path)
    git = FakeGit(
        {
            "worktree list": (_porcelain(h.path, h.branch), 0),
            "rev-list": ("1", 0),
            "for-each-ref": ("[gone]", 0),
        }
    )
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert not git.ran("branch -D")
    assert not report.removed[0].branch_is_spent


@pytest.mark.asyncio
async def test_the_reason_is_reported_not_silent(tmp_path: Path) -> None:
    """Removing on weaker evidence has to say so, or the operator cannot
    tell this case from an ordinary merged one."""
    h = _handle(tmp_path)
    git = FakeGit(
        {
            "worktree list": (_porcelain(h.path, h.branch), 0),
            "rev-list": ("1", 0),
            "for-each-ref": ("[gone]", 0),
        }
    )
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    joined = " ".join(report.removed[0].notes)
    assert "remote branch" in joined
    assert "keeping the branch" in joined


@pytest.mark.asyncio
async def test_a_never_pushed_branch_is_still_blocked(tmp_path: Path) -> None:
    """Empty `%(upstream:track)` means the branch never left the machine —
    that is the absence of evidence, not evidence of a merge."""
    h = _handle(tmp_path)
    git = FakeGit(
        {
            "worktree list": (_porcelain(h.path, h.branch), 0),
            "rev-list": ("1", 0),
            "for-each-ref": ("", 0),
        }
    )
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert report.removed == ()
    assert "1 commit(s) not in `main`" in report.kept[0].blockers


@pytest.mark.asyncio
async def test_a_branch_still_tracking_its_remote_is_blocked(tmp_path: Path) -> None:
    """An open PR's branch tracks a live ref — its worktree is in use."""
    h = _handle(tmp_path)
    git = FakeGit(
        {
            "worktree list": (_porcelain(h.path, h.branch), 0),
            "rev-list": ("1", 0),
            "for-each-ref": ("[ahead 1]", 0),
        }
    )
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert report.removed == ()


@pytest.mark.asyncio
async def test_an_ancestry_merged_branch_is_deleted(tmp_path: Path) -> None:
    """The strong case still deletes the ref — the gone-upstream path must
    not have quietly disabled branch cleanup for everyone."""
    h = _handle(tmp_path)
    git = FakeGit({"worktree list": (_porcelain(h.path, h.branch), 0), "rev-list": ("0", 0)})
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert report.removed[0].branch_is_spent
    assert git.ran(f"branch -D {h.branch}")


@pytest.mark.asyncio
async def test_a_blocked_worktree_survives_a_plain_prune(tmp_path: Path) -> None:
    h = _handle(tmp_path)
    git = FakeGit(
        {
            "worktree list": (_porcelain(h.path, h.branch), 0),
            "rev-list": ("2", 0),
        }
    )
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert report.removed == ()
    assert "2 commit(s) not in `main`" in report.kept[0].blockers
    assert not git.ran("worktree remove")


@pytest.mark.asyncio
async def test_force_removes_a_blocked_worktree_but_keeps_its_branch(
    tmp_path: Path,
) -> None:
    """The worktree is disposable under force; the unmerged commits are
    not. Keeping the branch is what makes them still reachable."""
    h = _handle(tmp_path)
    git = FakeGit({"worktree list": (_porcelain(h.path, h.branch), 0), "rev-list": ("2", 0)})
    report = await prune_worktrees(tmp_path, force=True, current=tmp_path, runner=git)
    assert len(report.removed) == 1
    assert git.ran("worktree remove")
    assert not git.ran("branch -D")


@pytest.mark.asyncio
async def test_each_worktree_is_judged_on_its_own(tmp_path: Path) -> None:
    """One dirty worktree must not save — or doom — the others."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    listing = _porcelain(a, "nightly/a") + _porcelain(b, "nightly/b")

    class PerPath(FakeGit):
        async def __call__(self, args: Sequence[str], cwd: Path | None):
            joined = " ".join(args)
            self.calls.append(list(args))
            if "worktree list" in joined:
                return listing.encode(), b"", 0
            if "status" in joined:
                return (b" M x.py", b"", 0) if cwd == b else (b"", b"", 0)
            if "rev-list" in joined:
                return b"0", b"", 0
            return b"", b"", 0

    git = PerPath()
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert [v.handle.branch for v in report.removed] == ["nightly/a"]
    assert [v.handle.branch for v in report.kept] == ["nightly/b"]


@pytest.mark.asyncio
async def test_no_worktrees_is_not_an_error(tmp_path: Path) -> None:
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=FakeGit())
    assert report.considered == 0
    assert report.removed == ()


@pytest.mark.asyncio
async def test_the_prefix_filter_is_honoured(tmp_path: Path) -> None:
    """A human's own worktree on an unrelated branch is not Nightly's to
    clean up."""
    h = _handle(tmp_path, branch="feature/mine")
    git = FakeGit({"worktree list": (_porcelain(h.path, h.branch), 0), "rev-list": ("0", 0)})
    report = await prune_worktrees(tmp_path, current=tmp_path, runner=git)
    assert report.considered == 0
