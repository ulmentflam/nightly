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
    "AgentsConfig",
    "ContextConfig",
    "GitConfig",
    "VaultConfig",
    "WorktreeConfig",
    "load_agents_config",
    "load_context_config",
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


@dataclass(frozen=True)
class AgentsConfig:
    """The `agents:` block of `.nightly/config.yml`.

    Governs how specialist sub-agents (implementer / tester / reviewer /
    researcher) get dispatched in interactive sessions. The skill text
    on each host reads this preference and chooses between
    `nightly dispatch start` (background) and the host's native Task-
    tool surface (foreground).
    """

    background_dispatch: bool = True
    """When True (default), specialists spawn as detached host processes
    via `nightly dispatch start <slug> --role <role>` — the operator's
    chat stays free for other work while the sub-agent runs. State is
    recorded under `.nightly/runs/<id>/tasks/<n>-<slug>/dispatch.json`;
    `nightly dispatch status` / `tail` / `wait` poll the spawn.

    When False, the skill falls back to the host's native Task-tool
    surface, which blocks the calling chat until the sub-agent returns.
    Use only when you explicitly want to watch the specialist's
    progress in-band (debugging an unfamiliar host, eyeballing a
    long-running review). Nightly's headless `nightly run` driver
    ignores this preference — each task gets its own host process by
    construction, so the chat-block concern doesn't apply."""


def load_agents_config(root: Path | None = None) -> AgentsConfig:
    """Parse the `agents:` block from `<root>/.nightly/config.yml`.

    Defaults whenever the file is missing, unreadable, malformed, or
    has no `agents:` block. Matches the shape of the other per-feature
    `load_*_config` helpers."""
    defaults = AgentsConfig()
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
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        return defaults
    return AgentsConfig(
        background_dispatch=bool(agents.get("background_dispatch", defaults.background_dispatch)),
    )


@dataclass(frozen=True)
class ContextConfig:
    """The `context:` block of `.nightly/config.yml` — v0.0.12.

    Governs the context-compaction feature: how aggressively the keepalive
    hook steers the live session toward context hygiene, and how often it
    refreshes the on-disk session digest the `SessionStart(compact)` hook
    re-injects after a compaction.
    """

    budget_tokens: int = 256_000
    """Soft context budget in tokens. When the keepalive hook's per-turn
    estimate of the live session's context exceeds this, it prepends a
    "context diet" block to the continuation prompt nudging the agent
    toward hygiene (lean on the digest, background heavy work, avoid
    re-reading large files). It is a SOFT limit by design — the prompt
    explicitly tells the agent to finish any delicate in-flight step
    first. `0` disables budget steering entirely (no estimate-vs-budget
    comparison, no diet block)."""

    digest_every_turns: int = 1
    """Write the session digest every N keepalive turn boundaries. `1`
    (default) refreshes it every turn so the `SessionStart(compact)` hook
    always re-injects current state; a larger value reduces write churn on
    very long sessions. `0` disables the interval write (the digest is
    still written unconditionally whenever the cascade routes the agent to
    the planning phase, since an ideate boundary is the natural compaction
    point)."""


def load_context_config(root: Path | None = None) -> ContextConfig:
    """Parse the `context:` block from `<root>/.nightly/config.yml`.

    Defaults whenever the file is missing, unreadable, malformed, or has
    no `context:` block. Individual missing/garbage keys fall back to
    their defaults — a non-integer `budget_tokens` degrades to the
    default rather than raising, matching the forgiving posture of the
    other `load_*_config` helpers."""
    defaults = ContextConfig()
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
    context = data.get("context") if isinstance(data, dict) else None
    if not isinstance(context, dict):
        return defaults

    def _coerce_int(key: str, default: int) -> int:
        # A typo'd / non-numeric value should degrade to the default, not
        # crash the loop — same forgiveness as a missing key.
        try:
            return int(context.get(key, default))
        except (TypeError, ValueError):
            return default

    return ContextConfig(
        budget_tokens=_coerce_int("budget_tokens", defaults.budget_tokens),
        digest_every_turns=_coerce_int("digest_every_turns", defaults.digest_every_turns),
    )
