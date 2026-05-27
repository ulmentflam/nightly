"""Tests for nightly_core.worktree."""

from __future__ import annotations

import platform
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import nightly_core.worktree as worktree_mod
from nightly_core.worktree import (
    WorktreeHandle,
    create_worktree,
    is_icloud_path,
    list_worktrees,
    remove_worktree,
)


def _make_runner(
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int = 0,
) -> tuple[Any, dict[str, Any]]:
    """Return a runner that captures calls and replays the given response."""
    captured: dict[str, Any] = {"calls": []}

    async def runner(args: Sequence[str], cwd: Path | None) -> tuple[bytes, bytes, int]:
        captured["calls"].append((list(args), cwd))
        return stdout, stderr, exit_code

    return runner, captured


def _routing_runner(common_dir: bytes) -> tuple[Any, dict[str, Any]]:
    """A runner that answers `rev-parse --git-common-dir` with `common_dir`.

    Every other call (e.g. `worktree add`) succeeds with empty output. Lets us
    drive `_main_worktree_root` to a specific main-repo path.
    """
    captured: dict[str, Any] = {"calls": []}

    async def runner(args: Sequence[str], cwd: Path | None) -> tuple[bytes, bytes, int]:
        captured["calls"].append((list(args), cwd))
        if args[:1] == ["rev-parse"]:
            return common_dir, b"", 0
        return b"", b"", 0

    return runner, captured


def _add_call(captured: dict[str, Any]) -> tuple[list[str], Path | None]:
    """Pull the `git worktree add ...` invocation out of captured calls."""
    return next(c for c in captured["calls"] if c[0][:2] == ["worktree", "add"])


# ── create_worktree ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_worktree_invokes_git_worktree_add(tmp_path: Path) -> None:
    runner, captured = _make_runner(exit_code=0)
    now = datetime(2026, 5, 20, 22, 14, tzinfo=UTC)
    handle = await create_worktree(
        tmp_path,
        slug="fix-flaky",
        base_branch="main",
        runner=runner,
        now=now,
    )

    assert isinstance(handle, WorktreeHandle)
    assert handle.branch == "nightly/fix-flaky-2026-05-20T22-14-00Z"
    assert handle.base_branch == "main"
    assert handle.created_at == now
    # Nested under a sibling `<repo>-nightly/` dir, with the branch's
    # `nightly/` prefix stripped from the leaf name. (The empty-stdout runner
    # makes `_main_worktree_root` fall back to `root`, i.e. tmp_path.)
    assert handle.path.name == "fix-flaky-2026-05-20T22-14-00Z"
    assert handle.path.parent == (tmp_path.parent / f"{tmp_path.name}-nightly").resolve()

    # The runner saw the right `git worktree add` invocation (preceded by the
    # `rev-parse --git-common-dir` lookup that resolves the main worktree).
    args, cwd = _add_call(captured)
    assert "-b" in args
    assert handle.branch in args
    assert "main" in args
    assert cwd == tmp_path


@pytest.mark.asyncio
async def test_create_worktree_respects_worktree_root(tmp_path: Path) -> None:
    """An explicit `worktree_root` overrides the sibling default."""
    runner, _ = _make_runner(exit_code=0)
    base_root = tmp_path / "custom-wt"
    handle = await create_worktree(
        tmp_path,
        slug="alpha",
        worktree_root=str(base_root),
        runner=runner,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # Placed under <worktree_root>/<repo-name>/<leaf> so multiple repos don't collide.
    assert handle.path.parent == (base_root / tmp_path.name).resolve()


@pytest.mark.asyncio
async def test_create_worktree_uses_main_worktree_root(tmp_path: Path) -> None:
    """Placement hangs off the main worktree (git-common-dir), not `root`.

    Simulates being invoked from inside a linked worktree: `rev-parse
    --git-common-dir` reports the *main* repo's `.git`, so the tree must nest
    off the main repo, not the cwd we passed in.
    """
    main_repo = tmp_path / "main-repo"
    runner, _ = _routing_runner(str(main_repo / ".git").encode() + b"\n")
    handle = await create_worktree(
        tmp_path / "some-worktree",
        slug="beta",
        runner=runner,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert handle.path.parent == (tmp_path / "main-repo-nightly").resolve()


@pytest.mark.asyncio
async def test_create_worktree_relocates_off_icloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An iCloud-resident base is auto-relocated to a non-synced fallback."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    icloud_root = tmp_path / "Library" / "Mobile Documents" / "iCloud"
    handle = await create_worktree(
        tmp_path,
        slug="gamma",
        worktree_root=str(icloud_root),
        runner=_make_runner(exit_code=0)[0],
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    expected = (tmp_path / "cache" / "nightly" / "worktrees" / tmp_path.name).resolve()
    assert handle.path.parent == expected
    assert "Mobile Documents" not in str(handle.path)


def test_is_icloud_path_off_macos_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert is_icloud_path(Path.home() / "Library" / "Mobile Documents" / "x") is False


def test_is_icloud_path_detects_mobile_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert is_icloud_path(Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
    assert worktree_mod.is_icloud_path(Path("/tmp/not-synced/repo")) is False


@pytest.mark.asyncio
async def test_create_worktree_raises_on_git_failure(tmp_path: Path) -> None:
    runner, _ = _make_runner(stderr=b"fatal: already exists", exit_code=128)
    with pytest.raises(RuntimeError, match="git worktree add failed"):
        await create_worktree(tmp_path, slug="alpha", runner=runner)


@pytest.mark.asyncio
async def test_create_worktree_branch_prefix_overridable(tmp_path: Path) -> None:
    runner, captured = _make_runner(exit_code=0)
    handle = await create_worktree(
        tmp_path,
        slug="alpha",
        branch_prefix="agent/",
        runner=runner,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert handle.branch.startswith("agent/alpha-")
    args, _ = _add_call(captured)
    assert handle.branch in args


# ── list_worktrees ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_worktrees_parses_porcelain_output(tmp_path: Path) -> None:
    """`git worktree list --porcelain` emits blank-line-separated records."""
    porcelain = (
        b"worktree /repo\n"
        b"HEAD abc123\n"
        b"branch refs/heads/main\n"
        b"\n"
        b"worktree /repo-nightly-alpha\n"
        b"HEAD def456\n"
        b"branch refs/heads/nightly/alpha\n"
        b"\n"
        b"worktree /repo-other\n"
        b"HEAD ghi789\n"
        b"branch refs/heads/feature/x\n"
        b"\n"
    )
    runner, _ = _make_runner(stdout=porcelain, exit_code=0)

    handles = await list_worktrees(tmp_path, runner=runner)
    # Only nightly/* branches; main and feature/x are filtered out
    assert len(handles) == 1
    assert handles[0].branch == "nightly/alpha"
    assert handles[0].path == Path("/repo-nightly-alpha")


@pytest.mark.asyncio
async def test_list_worktrees_empty_when_git_fails(tmp_path: Path) -> None:
    runner, _ = _make_runner(stderr=b"fatal: not a git repository", exit_code=128)
    handles = await list_worktrees(tmp_path, runner=runner)
    assert handles == []


@pytest.mark.asyncio
async def test_list_worktrees_respects_custom_prefix(tmp_path: Path) -> None:
    porcelain = (
        b"worktree /a\nbranch refs/heads/nightly/x\n\nworktree /b\nbranch refs/heads/agent/y\n\n"
    )
    runner, _ = _make_runner(stdout=porcelain, exit_code=0)
    handles = await list_worktrees(tmp_path, branch_prefix="agent/", runner=runner)
    assert len(handles) == 1
    assert handles[0].branch == "agent/y"


# ── remove_worktree ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_worktree_invokes_git_worktree_remove(tmp_path: Path) -> None:
    runner, captured = _make_runner(exit_code=0)
    handle = WorktreeHandle(
        path=tmp_path / "wt",
        branch="nightly/alpha",
        base_branch="main",
        created_at=datetime.now(UTC),
    )
    await remove_worktree(handle, root=tmp_path, runner=runner)
    args, _ = captured["calls"][0]
    assert args[:2] == ["worktree", "remove"]
    assert "--force" in args
    assert str(handle.path) in args


@pytest.mark.asyncio
async def test_remove_worktree_deletes_branch_when_requested(tmp_path: Path) -> None:
    runner, captured = _make_runner(exit_code=0)
    handle = WorktreeHandle(
        path=tmp_path / "wt",
        branch="nightly/alpha",
        base_branch="main",
        created_at=datetime.now(UTC),
    )
    await remove_worktree(handle, root=tmp_path, delete_branch=True, runner=runner)
    # Two calls: worktree remove, then branch -D
    assert len(captured["calls"]) == 2
    assert captured["calls"][1][0][:2] == ["branch", "-D"]
    assert handle.branch in captured["calls"][1][0]


@pytest.mark.asyncio
async def test_remove_worktree_falls_back_to_rmtree_when_git_fails(
    tmp_path: Path,
) -> None:
    """If `git worktree remove` fails, the directory still gets cleaned."""
    runner, _ = _make_runner(stderr=b"locked", exit_code=128)
    wt_path = tmp_path / "wt"
    wt_path.mkdir()
    (wt_path / "file.txt").write_text("data", encoding="utf-8")
    handle = WorktreeHandle(
        path=wt_path,
        branch="nightly/alpha",
        base_branch="main",
        created_at=datetime.now(UTC),
    )

    await remove_worktree(handle, root=tmp_path, runner=runner)
    # Directory was removed by the fallback rmtree
    assert not wt_path.exists()
