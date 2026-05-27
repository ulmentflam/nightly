"""Git worktree primitives — isolated per-task working trees.

Every Nightly task runs on its own `git worktree` so concurrent dispatches
cannot stomp on each other and a half-finished task never bleeds into the
user's primary working tree. Phase 8 wires this up.

Operations are async + git-runner-injectable so tests don't actually
spawn git (they're shells with arguments + cwds we can capture).
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "GitRunner",
    "WorktreeHandle",
    "create_worktree",
    "default_git_runner",
    "list_worktrees",
    "remove_worktree",
]


DEFAULT_BRANCH_PREFIX = "nightly/"


@dataclass(frozen=True)
class WorktreeHandle:
    """A Nightly-owned worktree."""

    path: Path
    """Absolute filesystem path the worktree lives at."""

    branch: str
    """Branch name on the worktree (e.g. `nightly/fix-flaky-2026-05-20T22-14Z`)."""

    base_branch: str
    """The branch the worktree was forked from (typically `main`)."""

    created_at: datetime


GitRunner = Callable[
    [Sequence[str], Path | None],
    Awaitable[tuple[bytes, bytes, int]],
]
"""Run `git <args>` and return (stdout, stderr, exit_code)."""


async def default_git_runner(
    args: Sequence[str],
    cwd: Path | None,
) -> tuple[bytes, bytes, int]:
    """Default `GitRunner` — uses `asyncio.create_subprocess_exec`."""
    import asyncio  # noqa: PLC0415

    binary = shutil.which("git")
    if binary is None:
        raise FileNotFoundError("git not found on PATH")
    proc = await asyncio.create_subprocess_exec(
        binary,
        *args,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout, stderr, proc.returncode or 0


def _branch_name(slug: str, *, prefix: str, now: datetime | None = None) -> str:
    """Compose `<prefix><slug>-<YYYY-MM-DDTHH-MM-SSZ>`."""
    ts = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{prefix}{slug}-{ts}"


def _worktree_path(root: Path, branch: str) -> Path:
    """Nest worktrees under a sibling ``<repo>-nightly/`` directory.

    Keeps the workspace root uncluttered: repo ``corpus-forge`` puts its
    worktrees in ``../corpus-forge-nightly/<leaf>``. The branch's first path
    segment (the ``nightly/`` prefix) names the parent dir, so strip it from
    the leaf to avoid a redundant ``nightly-`` echo.
    """
    _, _, leaf = branch.partition("/")
    safe = (leaf or branch).replace("/", "-")
    return (root.parent / f"{root.name}-nightly" / safe).resolve()


async def create_worktree(  # noqa: PLR0913 - all params are real config dimensions
    root: Path,
    slug: str,
    *,
    base_branch: str = "main",
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
    runner: GitRunner | None = None,
    now: datetime | None = None,
) -> WorktreeHandle:
    """Create a new isolated worktree for `slug`.

    Spawns `git worktree add <path> -b <branch> <base>`. The base branch
    must already exist on the repo at `root`.
    """
    run = runner or default_git_runner
    branch = _branch_name(slug, prefix=branch_prefix, now=now)
    path = _worktree_path(root, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add", str(path), "-b", branch, base_branch]
    _, stderr, exit_code = await run(args, root)
    if exit_code != 0:
        raise RuntimeError(
            f"git worktree add failed (exit {exit_code}): {stderr.decode('utf-8', errors='replace')}"
        )
    return WorktreeHandle(
        path=path,
        branch=branch,
        base_branch=base_branch,
        created_at=now or datetime.now(UTC),
    )


async def list_worktrees(
    root: Path,
    *,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
    runner: GitRunner | None = None,
) -> list[WorktreeHandle]:
    """Return Nightly-owned worktrees (those whose branch starts with `prefix`)."""
    run = runner or default_git_runner
    stdout, _, exit_code = await run(["worktree", "list", "--porcelain"], root)
    if exit_code != 0:
        return []
    return _parse_worktree_list(stdout.decode("utf-8", errors="replace"), branch_prefix)


def _parse_worktree_list(text: str, branch_prefix: str) -> list[WorktreeHandle]:
    """Parse `git worktree list --porcelain` output.

    Records are separated by blank lines; each starts with `worktree <path>`,
    optionally followed by `HEAD <sha>` and `branch refs/heads/<name>`.
    """
    handles: list[WorktreeHandle] = []
    current: dict[str, str] = {}

    def _flush() -> None:
        if not current:
            return
        path_str = current.get("worktree")
        branch_ref = current.get("branch", "")
        if path_str and branch_ref.startswith("refs/heads/"):
            branch = branch_ref[len("refs/heads/") :]
            if branch.startswith(branch_prefix):
                handles.append(
                    WorktreeHandle(
                        path=Path(path_str),
                        branch=branch,
                        base_branch="",  # not recoverable from porcelain output
                        created_at=datetime.now(UTC),
                    )
                )
        current.clear()

    for line in text.splitlines():
        if not line.strip():
            _flush()
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    _flush()
    return handles


async def remove_worktree(
    handle: WorktreeHandle,
    *,
    root: Path,
    delete_branch: bool = False,
    runner: GitRunner | None = None,
) -> None:
    """Tear down a worktree.

    `delete_branch=True` also runs `git branch -D <branch>` — useful when
    the branch was scratch work and the user doesn't need it preserved.
    Defaults to `False` since destructive git is a refusal-policy category;
    drivers can flip it when they're sure.
    """
    run = runner or default_git_runner
    _, _, exit_code = await run(["worktree", "remove", "--force", str(handle.path)], root)
    if exit_code != 0:
        # Even if remove fails, fall back to manual rm so the directory
        # doesn't linger forever. `git worktree prune` cleans the metadata.
        with contextlib.suppress(Exception):
            await run(["worktree", "prune"], root)
        if handle.path.exists():
            shutil.rmtree(handle.path, ignore_errors=True)
    if delete_branch:
        await run(["branch", "-D", handle.branch], root)
