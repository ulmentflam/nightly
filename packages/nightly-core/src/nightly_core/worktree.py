"""Git worktree primitives — isolated per-task working trees.

Every Nightly task runs on its own `git worktree` so concurrent dispatches
cannot stomp on each other and a half-finished task never bleeds into the
user's primary working tree. Phase 8 wires this up.

Operations are async + git-runner-injectable so tests don't actually
spawn git (they're shells with arguments + cwds we can capture).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "GitRunner",
    "WorktreeHandle",
    "create_worktree",
    "default_git_runner",
    "is_icloud_path",
    "list_worktrees",
    "remove_worktree",
]

_log = logging.getLogger(__name__)

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


def _worktree_path(base: Path, branch: str) -> Path:
    """Compose the worktree path ``<base>/<leaf>``.

    ``base`` is the already-resolved parent directory (see
    `_resolve_worktree_base`). The branch's first path segment (the ``nightly/``
    prefix) names that parent dir, so strip it from the leaf to avoid a
    redundant ``nightly-`` echo, and flatten any remaining slashes
    (``nightly/fix/x`` → ``fix-x``).
    """
    _, _, leaf = branch.partition("/")
    safe = (leaf or branch).replace("/", "-")
    return base / safe


async def _main_worktree_root(root: Path, run: GitRunner) -> Path:
    """Resolve the canonical *main* worktree root for ``root``.

    ``git rev-parse --git-common-dir`` returns the shared ``.git`` directory —
    the main worktree's, even when called from inside a linked worktree — so its
    parent is the repo we should hang ``<repo>-nightly/`` off of. Without this,
    running Nightly from inside a worktree would nest new trees off the worktree
    instead of the repo.

    Falls back to ``root`` whenever git is unavailable or the output isn't a
    usable ``.git`` directory (bare repos, separate gitdirs, mock runners), so
    callers always get a real directory.
    """
    try:
        stdout, _, exit_code = await run(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], root
        )
    except Exception:  # any git failure → safe fallback to `root`
        return root
    if exit_code != 0:
        return root
    common = stdout.decode("utf-8", errors="replace").strip()
    return _main_root_from_common(root, common)


def _main_root_from_common(root: Path, common: str) -> Path:
    """Turn a ``git rev-parse --git-common-dir`` string into the main worktree.

    Split out (sync) from `_main_worktree_root` so the path math — which touches
    the filesystem via ``.resolve()`` — stays out of the async body.
    """
    if not common:
        return root
    git_dir = Path(common)
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    return git_dir.parent if git_dir.name == ".git" else root


def _cloud_docs_mount_active() -> bool:
    """True if an iCloud/CloudDocs FileProvider mount is currently active."""
    try:
        out = subprocess.run(  # fixed argv, no shell
            ["mount"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    haystack = out.stdout.lower()
    return "fileprovider" in haystack or "clouddocs" in haystack


def is_icloud_path(p: Path) -> bool:
    """Best-effort: is ``p`` under macOS iCloud Drive / FileProvider sync?

    On macOS, ``fileproviderd`` silently reverts shell ``mv``/``rm``/``git mv``
    and leaves dataless ``.icloud`` placeholders, which corrupts git worktrees.

    The reliable signal is a realpath under ``~/Library/Mobile Documents/``
    (iCloud Drive's backing store) or a ``com~apple~CloudDocs`` path component.
    ``~/Documents`` and ``~/Desktop`` are *also* exposed when "Desktop &
    Documents Folders" sync is on, but that's only detectable at runtime via an
    active CloudDocs FileProvider mount, so it's a secondary heuristic. Always
    ``False`` off macOS.
    """
    if platform.system() != "Darwin":
        return False
    try:
        real = Path(os.path.realpath(p))
    except OSError:
        real = p
    text = str(real)
    if "/Library/Mobile Documents/" in text or "com~apple~CloudDocs" in text:
        return True
    home = Path.home()
    synced = (home / "Documents", home / "Desktop")
    if any(real == d or d in real.parents for d in synced):
        return _cloud_docs_mount_active()
    return False


def _non_synced_fallback(repo_name: str) -> Path:
    """A worktree base guaranteed off iCloud: ``$XDG_CACHE_HOME/nightly/worktrees/<repo>``."""
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return (Path(base).expanduser() / "nightly" / "worktrees" / repo_name).resolve()


def _select_base(main: Path, worktree_root: str | None) -> Path:
    """Pick the parent dir for new worktrees, relocating off iCloud if needed.

    Precedence: explicit ``worktree_root`` config → default sibling
    ``<repo>-nightly/``. Either way, if the result lands under iCloud sync we
    warn and relocate to a non-synced fallback so tasks (and the host's session
    spawned there) never run on a filesystem that corrupts git state. Sync (no
    awaits) so its filesystem-touching path math stays out of the async caller.
    """
    if worktree_root:
        base = (Path(worktree_root).expanduser() / main.name).resolve()
    else:
        base = (main.parent / f"{main.name}-nightly").resolve()
    if is_icloud_path(base):
        fallback = _non_synced_fallback(main.name)
        _log.warning(
            "worktree base %s is under iCloud/FileProvider sync; relocating to %s "
            "to avoid silent corruption. Set git.worktree_root to a non-synced path "
            "to silence this.",
            base,
            fallback,
        )
        base = fallback
    return base


async def _resolve_worktree_base(
    root: Path,
    *,
    worktree_root: str | None,
    run: GitRunner,
) -> Path:
    """Resolve the worktree parent dir: find the main repo, then `_select_base`."""
    main = await _main_worktree_root(root, run)
    return _select_base(main, worktree_root)


def _lookup_open_pr_head_ref(pr_number: int, root: Path | None) -> str | None:
    """Return the head ref of PR #N if it is OPEN, else None.

    Splits the failure-path bookkeeping (logging, fall-throughs) out of
    `_resolve_base_branch` so the latter stays a small dispatcher. Each
    failure mode (missing `gh`, subprocess error, non-zero exit, bad
    JSON, non-OPEN state) logs once with `pr_number` in context and
    returns None — the caller substitutes `default_base`.
    """
    if shutil.which("gh") is None:
        _log.warning(
            "plan declares depends_on_pr=%s but `gh` is unavailable; "
            "falling back to default base",
            pr_number,
        )
        return None
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "headRefName,state"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log.warning(
            "gh pr view %s failed (%s); falling back to default base", pr_number, exc
        )
        return None
    if result.returncode != 0:
        _log.warning(
            "gh pr view %s exited %s; falling back to default base",
            pr_number,
            result.returncode,
        )
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        _log.warning(
            "gh pr view %s returned unparseable JSON; falling back to default base",
            pr_number,
        )
        return None
    state = str(payload.get("state") or "").upper()
    head_ref = str(payload.get("headRefName") or "").strip()
    if state != "OPEN" or not head_ref:
        _log.warning(
            "depends_on_pr=%s is in state %r (head=%r); falling back to default base",
            pr_number,
            state or "<unknown>",
            head_ref,
        )
        return None
    return head_ref


def _resolve_base_branch(
    *,
    depends_on_pr: int | None,
    default_base: str,
    root: Path | None = None,
) -> str:
    """Resolve the effective base branch for a new worktree (RFC 004).

    Default behavior is "branch from `default_base`" (the configured base,
    usually `main`). The plan can opt into a stacked geometry by declaring
    `depends_on_pr: <N>` in its frontmatter: when set, this helper looks
    up PR #N via `gh pr view <N> --json headRefName,state` and returns
    the PR's head ref *only if the PR is OPEN*. Closed/merged PRs, a
    missing `gh`, or any failure (timeout, JSON parse, non-zero exit)
    fall back to `default_base` with a warning — RFC 004 deliberately
    biases toward `default_base` over silently stacking on a stale PR.

    Lives in `worktree.py` rather than `plans.py` so the geometry-check
    primitive stays plan-agnostic; the caller passes the parsed
    `depends_on_pr` int. This keeps the import direction
    `driver → {plans, worktree}` clean instead of inducing
    `worktree → plans`.

    Synchronous shell-out matches `cascade._nightly_open_pr_branches`'s
    `gh pr list` pattern; called from `run_one_task`'s async body just
    before `create_worktree`. RFC 002's READY-marker check is
    independent (post-creation cache); no shared state.
    """
    if depends_on_pr is None:
        return default_base
    head_ref = _lookup_open_pr_head_ref(depends_on_pr, root)
    return head_ref if head_ref is not None else default_base


async def create_worktree(  # noqa: PLR0913 - all params are real config dimensions
    root: Path,
    slug: str,
    *,
    base_branch: str = "main",
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
    worktree_root: str | None = None,
    runner: GitRunner | None = None,
    now: datetime | None = None,
) -> WorktreeHandle:
    """Create a new isolated worktree for `slug`.

    Spawns `git worktree add <path> -b <branch> <base>`. The base branch
    must already exist on the repo at `root`. Placement is decided by
    `_resolve_worktree_base` (config-overridable, iCloud-aware).
    """
    run = runner or default_git_runner
    branch = _branch_name(slug, prefix=branch_prefix, now=now)
    base = await _resolve_worktree_base(root, worktree_root=worktree_root, run=run)
    path = _worktree_path(base, branch)
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
