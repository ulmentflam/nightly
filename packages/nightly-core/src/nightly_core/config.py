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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nightly_core.paths import nightly_dir

__all__ = [
    "GitConfig",
    "VaultConfig",
    "WorktreeConfig",
    "load_git_config",
    "load_vault_config",
    "load_worktree_config",
]

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


@dataclass(frozen=True)
class VaultConfig:
    """The `vault:` block of `.nightly/config.yml` — RFC 003."""

    enabled: bool = True
    """Master switch. False = `nightly brief` skips the vault build step."""

    open_on_brief: bool = False
    """If True, `nightly brief` opens the dashboard after rendering. Useful
    for an interactive operator; off by default so unattended runs don't
    pop windows."""


@dataclass(frozen=True)
class WorktreeConfig:
    """The `worktree:` block of `.nightly/config.yml` — RFC 002."""

    probe_enabled: bool = True
    """Master switch — disable to skip readiness probing entirely."""

    remediate_enabled: bool = True
    """If False, remediable failures surface as `worktree_blocked`
    rather than being auto-fixed via `uv sync` / `pre-commit install`."""


def load_worktree_config(root: Path) -> WorktreeConfig:
    """Parse the `worktree:` block from `<root>/.nightly/config.yml`.
    Both knobs default on; missing block / malformed YAML → defaults."""
    defaults = WorktreeConfig()
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
    wt = data.get("worktree") if isinstance(data, dict) else None
    if not isinstance(wt, dict):
        return defaults
    return WorktreeConfig(
        probe_enabled=bool(wt.get("probe_enabled", defaults.probe_enabled)),
        remediate_enabled=bool(wt.get("remediate_enabled", defaults.remediate_enabled)),
    )


def load_vault_config(root: Path) -> VaultConfig:
    """Parse the `vault:` block from `<root>/.nightly/config.yml`. Defaults
    when the file is missing, unreadable, or has no `vault:` block."""
    defaults = VaultConfig()
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

    vault = data.get("vault") if isinstance(data, dict) else None
    if not isinstance(vault, dict):
        return defaults

    return VaultConfig(
        enabled=bool(vault.get("enabled", defaults.enabled)),
        open_on_brief=bool(vault.get("open_on_brief", defaults.open_on_brief)),
    )


@dataclass(frozen=True)
class SynthesisConfig:
    """The `ideate.synthesis:` sub-block of `.nightly/config.yml` — RFC 009."""

    enabled: bool = True
    """Master switch for the LLM synthesis proposer. False = the three
    Phase-5 narrow proposers still run; synthesis is skipped entirely
    (no host CLI spawn). Cost-sensitive operators flip this off."""

    timeout_seconds: int = 120
    """Wall-clock cap on the synthesis spawn. The host CLI is killed
    if it doesn't return within this many seconds; the proposer
    degrades to empty proposals."""

    max_proposals: int = 25
    """Cap on synthesis output. The parser truncates at this count to
    keep the morning briefing readable; the prompt template also
    instructs the model to cap itself."""


@dataclass(frozen=True)
class IdeateConfig:
    """The `ideate:` block of `.nightly/config.yml` — RFC 009 §8."""

    category_ordering: bool = True
    """RFC 009 §4. When True (the default), the cascade sorts ideated
    proposals by `(strategic_category_rank, -score)` so cleaning
    outranks capability even at lower numeric scores. When False, the
    cascade reverts to score-only ordering (pre-v0.0.6 behavior).
    Operators who don't want the category-first ordering can opt out
    without disabling the synthesis proposer entirely."""

    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)


def load_ideate_config(root: Path | None = None) -> IdeateConfig:
    """Parse the `ideate:` block from `<root>/.nightly/config.yml`.

    Defaults whenever the file is missing, unreadable, malformed, or
    has no `ideate:` block. Missing nested `synthesis:` sub-block
    falls back to `SynthesisConfig()` defaults. `root=None` resolves
    via `nightly_dir(None)` which uses the cwd-derived repo root —
    matching the existing `load_*_config` shape.
    """
    defaults = IdeateConfig()
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
    ideate = data.get("ideate") if isinstance(data, dict) else None
    if not isinstance(ideate, dict):
        return defaults

    synthesis_raw = ideate.get("synthesis")
    if isinstance(synthesis_raw, dict):
        synthesis = SynthesisConfig(
            enabled=bool(synthesis_raw.get("enabled", defaults.synthesis.enabled)),
            timeout_seconds=int(
                synthesis_raw.get("timeout_seconds", defaults.synthesis.timeout_seconds)
            ),
            max_proposals=int(synthesis_raw.get("max_proposals", defaults.synthesis.max_proposals)),
        )
    else:
        synthesis = defaults.synthesis

    return IdeateConfig(
        category_ordering=bool(ideate.get("category_ordering", defaults.category_ordering)),
        synthesis=synthesis,
    )
