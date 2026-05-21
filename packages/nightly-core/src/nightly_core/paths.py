"""Path helpers for locating Nightly's on-disk state.

The two top-level folders are conventions, not requirements:
- `.nightly/` — agent runtime state
- `.planning/` — human-authored design intent

Both paths are configurable via config.yml; these helpers return the defaults.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "current_run_pointer",
    "new_run_id",
    "nightly_dir",
    "planning_dir",
    "repo_root",
    "run_dir",
    "runs_dir",
]


def repo_root(start: Path | None = None) -> Path:
    """Find the git repo root containing `start` (default cwd).

    Falls back to the resolved start path when `git rev-parse` fails — that
    way callers always get a real directory, never None. Callers that care
    whether they're in a git repo should check separately.
    """
    cwd = (start or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return cwd
    return Path(result.stdout.strip())


def nightly_dir(root: Path | None = None) -> Path:
    """Path to `.nightly/` — agent runtime state."""
    return (root or repo_root()) / ".nightly"


def planning_dir(root: Path | None = None) -> Path:
    """Path to `.planning/` — human-authored design intent."""
    return (root or repo_root()) / ".planning"


def runs_dir(root: Path | None = None) -> Path:
    """Path to `.nightly/runs/` — one folder per session."""
    return nightly_dir(root) / "runs"


def current_run_pointer(root: Path | None = None) -> Path:
    """Path to `.nightly/runs/CURRENT` — points at the live run id."""
    return runs_dir(root) / "CURRENT"


def new_run_id(now: datetime | None = None) -> str:
    """Generate a filesystem-safe ISO-8601 run id (UTC)."""
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H-%M-%SZ")


def run_dir(run_id: str, root: Path | None = None) -> Path:
    """Path to `.nightly/runs/<run_id>/`."""
    return runs_dir(root) / run_id
