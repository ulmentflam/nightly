"""Self-update — pull the latest Nightly source and refresh skills.

Inspired by [`gsd-build/get-shit-done`](https://github.com/gsd-build/get-shit-done)'s
idempotent npm installer pattern: re-running it pulls the latest
package and re-drops the host-specific skill files so every repo
gets the new content without manual intervention.

Nightly's distribution is git-based today (via `install.sh` which
clones to `~/.local/share/nightly` and writes a `uv run` shim into
`~/.local/bin/nightly`). The update command:

1. Finds the source checkout by walking up from `nightly_core.__file__`
   until it hits a `.git` directory.
2. Fetches the requested version (`main` by default; can be a tag or
   commit SHA), checks it out, and runs `uv sync` to update deps.
3. Walks the current repo and re-runs `install("project")` for every
   host that's already installed there — so the new SKILL.md content,
   the rules block, and any updated Stop-hook command propagate
   without the user having to remember every per-host re-init step.

If/when Nightly publishes to PyPI, this module gets a parallel path
that calls `uv tool upgrade nightly` or `pipx upgrade nightly` — the
public API (`update_install`) is shaped to accommodate either.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from nightly_core._version import __version__
from nightly_core.rules import seed_rules

__all__ = [
    "InstallMethod",
    "UpdateReport",
    "detect_install_method",
    "detect_install_root",
    "git_head_commit",
    "refresh_repo_install",
    "update_install",
]


@dataclass(frozen=True)
class InstallMethod:
    """How the running `nightly` was installed; drives the upgrade strategy."""

    kind: str  # `git` · `unknown`
    root: Path | None  # source dir for git installs; None for unknown

    @property
    def is_git(self) -> bool:
        return self.kind == "git" and self.root is not None


@dataclass(frozen=True)
class UpdateReport:
    """Result of an `update_install` invocation; printed by the CLI.

    `before` / `after` are the short commit SHAs at the source root
    (empty string if unknown). `refreshed_hosts` lists the host ids
    whose `install("project")` was re-run in the current repo. Empty
    `refreshed_hosts` is fine — it just means no host was installed
    there to begin with.
    """

    method: InstallMethod
    requested_version: str
    before: str
    after: str
    refreshed_hosts: tuple[str, ...]
    rules_action: str  # `created` / `updated` / `unchanged` / `skipped`
    dry_run: bool
    notes: tuple[str, ...] = ()

    @property
    def source_changed(self) -> bool:
        return bool(self.before) and bool(self.after) and self.before != self.after


# ── detection ─────────────────────────────────────────────────────────────


def detect_install_root() -> Path | None:
    """Walk up from `nightly_core` until we hit a `.git` directory.

    Returns the directory containing `.git` (the source checkout) when
    the running Nightly came from a git install; returns None for
    PyPI / wheel / editable-non-git installs.
    """
    here = Path(__file__).resolve()
    for ancestor in (here, *here.parents):
        if (ancestor / ".git").is_dir():
            return ancestor
    return None


def detect_install_method() -> InstallMethod:
    """Heuristic: did the user install via install.sh (git) or PyPI / pipx?"""
    root = detect_install_root()
    if root is not None and shutil.which("git") is not None:
        return InstallMethod(kind="git", root=root)
    return InstallMethod(kind="unknown", root=None)


def git_head_commit(root: Path | None) -> str:
    """Short HEAD commit at `root`, or empty string if not a git repo / git missing."""
    if root is None or shutil.which("git") is None:
        return ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip()


# ── upgrade ───────────────────────────────────────────────────────────────


def _git(args: list[str], *, cwd: Path) -> None:
    """Run `git ...` and raise on non-zero."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=120)


def _uv_sync(cwd: Path) -> None:
    """Run `uv sync --all-packages --quiet` in the source root.

    Best-effort — falls back to a non-package sync if the workspace
    layout was changed in the new version. Errors propagate so the
    CLI can surface them.
    """
    if shutil.which("uv") is None:
        msg = "uv is required to sync dependencies after a source update."
        raise RuntimeError(msg)
    subprocess.run(
        ["uv", "sync", "--all-packages", "--quiet"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


def update_install(
    *,
    version: str = "main",
    dry_run: bool = False,
) -> tuple[InstallMethod, str, str]:
    """Update Nightly's source to `version`. Returns (method, before_sha, after_sha).

    For git installs, this fetches + checks out the ref + runs `uv sync`.
    For unknown installs (PyPI, pipx), this raises — the caller should
    surface a message pointing the user at the right tool.

    `dry_run=True` performs the fetch but doesn't check out or sync —
    useful for previewing the upgrade.
    """
    method = detect_install_method()
    if not method.is_git or method.root is None:
        msg = (
            "Couldn't find a git source checkout for Nightly. "
            "If you installed via PyPI / pipx / uv tool, upgrade with "
            "`uv tool upgrade nightly` or `pipx upgrade nightly`. "
            "If you installed via install.sh, re-run it: "
            "`curl -fsSL https://raw.githubusercontent.com/ulmentflam/nightly/main/install.sh | bash`."
        )
        raise RuntimeError(msg)

    before = git_head_commit(method.root)

    # Fetch always — even on dry-run — so the user sees the latest refs.
    _git(["fetch", "--quiet", "--tags", "origin"], cwd=method.root)
    if dry_run:
        return method, before, before

    _git(["checkout", "--quiet", version], cwd=method.root)
    # Only pull when we're on a branch (tags/SHAs are detached HEADs).
    try:
        _git(["symbolic-ref", "--quiet", "HEAD"], cwd=method.root)
        on_branch = True
    except subprocess.CalledProcessError:
        on_branch = False
    if on_branch:
        # Non-ff or branch mismatch — leave the checkout where it is and
        # let the after-SHA surface the actual state. The user can resolve
        # manually if the pull would have rewritten history.
        with contextlib.suppress(subprocess.CalledProcessError):
            _git(["pull", "--quiet", "--ff-only", "origin", version], cwd=method.root)

    _uv_sync(method.root)
    after = git_head_commit(method.root)
    return method, before, after


# ── per-repo refresh ──────────────────────────────────────────────────────


def refresh_repo_install(
    repo_root: Path,
    *,
    host_loader: dict | None = None,
) -> tuple[tuple[str, ...], str]:
    """Re-install every host that's already present in `repo_root`.

    Returns `(refreshed_hosts, rules_action)`. `host_loader` is the
    same dict the CLI uses to map host id → integration loader; tests
    can inject a stub here. When None, the function imports the CLI's
    `_HOST_LOADERS` lazily to avoid a top-of-module import cycle.

    Per-host install is idempotent (verified by the tests in Phases
    9h/9i) so this is safe to call even on a fresh repo where no host
    is yet installed — in that case `refreshed_hosts` is empty.
    """
    loaders = host_loader
    if loaders is None:
        from nightly_core.cli import _HOST_LOADERS  # noqa: PLC0415 - lazy

        loaders = _HOST_LOADERS
    refreshed: list[str] = []
    for host_id, loader in loaders.items():
        integration = loader(repo_root)
        if not integration.is_installed("project"):
            continue
        asyncio.run(integration.install("project"))
        refreshed.append(host_id)

    # The rules block (AGENTS.md / CLAUDE.md) also gets re-seeded so
    # any new rules from the upgrade land in the user's repo.
    results = seed_rules(repo_root, create_if_absent=False)
    rules_action = "skipped"
    for r in results:
        if r.action in {"created", "updated"}:
            rules_action = r.action
            break
    return tuple(refreshed), rules_action


def current_version() -> str:
    """Convenience re-export of the installed Nightly version string."""
    return __version__
