"""Read `.nightly/config.yml` into typed config objects.

The config file is written by `nightly init` (see `_DEFAULT_CONFIG_YML` in
`cli.py`) but, until now, was never read back — `nightly run` built its
`DriverConfig` from hardcoded defaults, so the `git:` block was inert. This
module closes that gap.

Loading is deliberately best-effort: a missing, unreadable, or malformed file
yields all-defaults rather than raising, so a typo in config.yml degrades to
"defaults" instead of crashing the loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nightly_core.paths import nightly_dir

__all__ = ["GitConfig", "load_git_config"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitConfig:
    """The `git:` block of `.nightly/config.yml`."""

    base_branch: str = "main"
    """Branch Nightly forks each per-task worktree from."""

    branch_prefix: str = "nightly/"
    """Prefix for branches Nightly cuts; also how it recognizes its own worktrees."""

    worktree_root: str | None = None
    """Where per-task worktrees are placed. `None` = nest under a sibling
    `<repo>-nightly/` dir. Set to a path (e.g. `~/.cache/nightly/worktrees`) to
    keep trees off a synced/iCloud filesystem; `~` is expanded."""


def load_git_config(root: Path) -> GitConfig:
    """Parse the `git:` block from `<root>/.nightly/config.yml`.

    Returns `GitConfig()` defaults when the file is absent, unreadable, not a
    mapping, or has no `git:` block. Individual missing keys fall back to their
    defaults too.
    """
    defaults = GitConfig()
    path = nightly_dir(root) / "config.yml"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return defaults

    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        _log.warning("ignoring malformed %s: %s", path, exc)
        return defaults

    git = data.get("git") if isinstance(data, dict) else None
    if not isinstance(git, dict):
        return defaults

    worktree_root = git.get("worktree_root")
    return GitConfig(
        base_branch=str(git.get("base_branch", defaults.base_branch)),
        branch_prefix=str(git.get("branch_prefix", defaults.branch_prefix)),
        # Treat empty/whitespace-only as "unset" so a blank line in the template
        # doesn't become a literal worktree path.
        worktree_root=(str(worktree_root).strip() or None if worktree_root is not None else None),
    )
