"""Read `.nightly/config.yml` into typed config objects.

The config file is written by `nightly init` (see `DEFAULT_CONFIG_YML`
below) but, until now, was never read back — `nightly run` built its
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
from typing import Any, cast, get_args

import yaml

from nightly_core.contract import MODEL_TIERS, HostId, ModelTier, ReasoningEffort
from nightly_core.paths import nightly_dir

__all__ = [
    "DEFAULT_CONFIG_YML",
    "DEFAULT_MODEL_CONTEXT_TOKENS",
    "DEFAULT_TIER_EFFORT",
    "DEFAULT_TIER_MODELS",
    "AgentsConfig",
    "CompactConfig",
    "ContextConfig",
    "GitConfig",
    "ModelTierConfig",
    "ParallelismConfig",
    "TierBinding",
    "VaultConfig",
    "WorktreeConfig",
    "load_agents_config",
    "load_compact_config",
    "load_context_config",
    "load_git_config",
    "load_model_tier_config",
    "load_parallelism_config",
    "load_vault_config",
    "load_worktree_config",
    "render_config_yml",
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


# ── model-tier routing (RFC 007) ─────────────────────────────────────────

DEFAULT_TIER_EFFORT: dict[ModelTier, ReasoningEffort] = {
    "lite": "low",
    "coding": "low",
    "reasoning": "xhigh",
}
"""Default reasoning effort per tier.

`lite` and `coding` sit at `low` deliberately. Lower effort yields fewer,
more-consolidated tool calls and less preamble — which is the whole point
of those tiers: they should be writing files, not deliberating about
writing files. Their output is gated by `nightly verify` regardless, so
under-thinking surfaces as a red lint/type/test run rather than as a
silently-bad merge.

`reasoning` sits at `xhigh` because its jobs (orchestration, result
validation, merge adjudication) are exactly the intelligence-sensitive
work that effort buys. Raise to `max` only when correctness dominates
cost; `max` is prone to overthinking on routine tasks."""

DEFAULT_MODEL_CONTEXT_TOKENS: dict[str, int] = {
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
"""Known context-window sizes, keyed by model id.

Feeds `nightly_core.routing.resolve_context_thresholds`, which scales the
handoff thresholds to whatever window the dispatched model actually has —
so a 200K-context Haiku agent hands off proportionally earlier than a 1M
Opus agent, with no per-model threshold bookkeeping in the config.

Only Anthropic ids are pre-seeded, because those are the ones Nightly can
verify. Operators pointing a tier at a non-Anthropic model should declare
its window under `context.model_context_tokens:` in `.nightly/config.yml`;
undeclared models fall back to `ContextConfig.default_context_tokens`."""

DEFAULT_TIER_MODELS: dict[HostId, dict[ModelTier, str]] = {
    "claude": {
        "lite": "claude-haiku-4-5",
        "coding": "claude-sonnet-5",
        "reasoning": "claude-opus-5",
    },
    "cursor": {
        "lite": "claude-haiku-4-5",
        "coding": "claude-sonnet-5",
        "reasoning": "claude-opus-5",
    },
    "opencode": {
        "lite": "claude-haiku-4-5",
        "coding": "claude-sonnet-5",
        "reasoning": "claude-opus-5",
    },
}
"""Per-host tier → model-id mapping, seeded for the Anthropic-backed hosts.

Deliberately partial. Nightly ships defaults only for hosts whose model
ids it can state authoritatively; `codex`, `gemini`, and `antigravity`
resolve to an empty mapping, which `resolve_model_for_task` reports as
"no binding" so the dispatch falls through to the host CLI's own default
model (today's behavior) with a friction note in the briefing.

That is the right failure mode: a wrong model id is a hard dispatch error
at 3am, while an absent one is a silent no-op that still gets the work
done. Operators wire their own ids under `model_tiers:` — the schema
accepts any string, so a tier can point at any model the host accepts."""


@dataclass(frozen=True)
class TierBinding:
    """One tier's resolved (model, effort) pair for a given host."""

    tier: ModelTier
    model: str | None
    """Concrete model id, or None when the host has no binding for this
    tier and the dispatch should fall back to the host CLI's default."""

    effort: ReasoningEffort


@dataclass(frozen=True)
class ModelTierConfig:
    """The `model_tiers:` block of `.nightly/config.yml` — RFC 007."""

    enabled: bool = True
    """Master switch. False = every dispatch uses the host CLI's default
    model and no effort hint is injected (pre-RFC-007 behavior)."""

    models: dict[HostId, dict[ModelTier, str]] = field(
        default_factory=lambda: {host: dict(tiers) for host, tiers in DEFAULT_TIER_MODELS.items()}
    )
    """Per-host tier → model-id map, merged over `DEFAULT_TIER_MODELS`."""

    effort: dict[ModelTier, ReasoningEffort] = field(
        default_factory=lambda: dict(DEFAULT_TIER_EFFORT)
    )
    """Per-tier reasoning effort, merged over `DEFAULT_TIER_EFFORT`."""

    flags: dict[HostId, str] = field(default_factory=dict)
    """Per-host model-selection flag, discovered by `nightly init` from the
    host CLI's own `--help` (see `nightly_core.model_probe`). Absent means
    "this host has no known model flag" — dispatch then runs on the host's
    default model, with the tier still applied via the effort directive in
    the prompt. Nightly never guesses a flag."""

    def flag_for(self, host: HostId) -> str | None:
        """The discovered model-selection flag for `host`, if any."""
        return self.flags.get(host)

    def binding(self, host: HostId, tier: ModelTier) -> TierBinding:
        """Resolve `(model, effort)` for `host` at `tier`.

        A host with no entry — or a host whose entry omits this tier —
        yields `model=None`, meaning "let the host CLI pick." Effort is
        always resolved, since it is injected as prompt text rather than
        as a vendor-specific CLI flag and therefore works on every host.
        """
        model = self.models.get(host, {}).get(tier)
        return TierBinding(
            tier=tier,
            model=model or None,
            effort=self.effort.get(tier, DEFAULT_TIER_EFFORT[tier]),
        )


def _merge_tier_effort(block: dict[str, Any], path: Path) -> dict[ModelTier, ReasoningEffort]:
    """Merge `model_tiers.effort:` over `DEFAULT_TIER_EFFORT`."""
    effort = dict(DEFAULT_TIER_EFFORT)
    raw = block.get("effort")
    if not isinstance(raw, dict):
        return effort
    known = set(get_args(ReasoningEffort))
    for tier, value in raw.items():
        text = str(value).strip().lower()
        if tier not in MODEL_TIERS:
            _log.warning("%s: unknown model tier %r under model_tiers.effort", path, tier)
        elif text not in known:
            _log.warning("%s: unknown reasoning effort %r for tier %r", path, value, tier)
        else:
            effort[cast("ModelTier", tier)] = cast("ReasoningEffort", text)
    return effort


def _merge_tier_models(block: dict[str, Any], path: Path) -> dict[HostId, dict[ModelTier, str]]:
    """Merge per-host `model_tiers.<host>:` maps over `DEFAULT_TIER_MODELS`."""
    models = {host: dict(tiers) for host, tiers in DEFAULT_TIER_MODELS.items()}
    known_hosts = set(get_args(HostId))
    for raw_host, tiers in block.items():
        if raw_host in {"enabled", "effort"}:
            continue
        if raw_host not in known_hosts:
            _log.warning("%s: unknown host %r under model_tiers", path, raw_host)
            continue
        if not isinstance(tiers, dict):
            continue
        host = cast("HostId", raw_host)
        merged = dict(models.get(host, {}))
        for raw_tier, model_id in tiers.items():
            if raw_tier == "flag":
                continue  # handled by `_merge_tier_flags`
            if raw_tier not in MODEL_TIERS:
                _log.warning("%s: unknown model tier %r under model_tiers.%s", path, raw_tier, host)
                continue
            text = str(model_id).strip()
            if text:
                merged[cast("ModelTier", raw_tier)] = text
        models[host] = merged
    return models


def _merge_tier_flags(block: dict[str, Any]) -> dict[HostId, str]:
    """Collect per-host `flag:` entries written by `nightly init`'s probe."""
    known_hosts = set(get_args(HostId))
    flags: dict[HostId, str] = {}
    for raw_host, tiers in block.items():
        if raw_host not in known_hosts or not isinstance(tiers, dict):
            continue
        flag = str(tiers.get("flag", "")).strip()
        if flag.startswith("-"):
            flags[cast("HostId", raw_host)] = flag
    return flags


def load_model_tier_config(root: Path | None = None) -> ModelTierConfig:
    """Parse the `model_tiers:` block from `<root>/.nightly/config.yml`.

    Operator entries are *merged over* the built-in defaults rather than
    replacing them, so declaring a single host (or a single tier within a
    host) does not silently blank the rest. Unknown host keys and unknown
    tier names are dropped with a warning — a typo should degrade to the
    default binding, not invent a phantom host.
    """
    defaults = ModelTierConfig()
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
    block = data.get("model_tiers") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return defaults

    return ModelTierConfig(
        enabled=bool(block.get("enabled", defaults.enabled)),
        models=_merge_tier_models(block, path),
        effort=_merge_tier_effort(block, path),
        flags=_merge_tier_flags(block),
    )


# ── parallelism (RFC 012) ────────────────────────────────────────────────


@dataclass(frozen=True)
class ParallelismConfig:
    """The `parallelism:` block of `.nightly/config.yml` — RFC 012.

    Nightly's throughput ceiling is how many specialists it dares run at
    once. The defaults lean wide: background dispatch already keeps the
    operator's chat free, worktrees already isolate the filesystem, and
    `nightly verify` already gates the output — so the marginal risk of a
    wider fleet is low and the marginal wall-clock saving is large.
    """

    max_concurrent_specialists: int = 8
    """Ceiling on simultaneously-running background dispatches across the
    whole session. `0` means unlimited (no admission control)."""

    max_worktrees: int = 8
    """Ceiling on live Nightly worktrees. Each parallel task needs one, so
    this is effectively the task-level fan-out cap. `0` means unlimited."""

    per_tier: dict[ModelTier, int] = field(
        default_factory=lambda: cast(
            "dict[ModelTier, int]", {"lite": 8, "coding": 6, "reasoning": 2}
        )
    )
    """Per-tier concurrency ceilings, checked in addition to the global cap.

    The gradient is the point: run lite and coding agents wide, and keep
    reasoning agents scarce. Reasoning-tier dispatches are the expensive
    ones and they are doing adjudication work that mostly serializes
    anyway — two at a time is plenty, and capping them here is what keeps
    a wide fleet from becoming a wide *expensive* fleet. `0` = unlimited."""

    def limit_for(self, tier: ModelTier) -> int:
        """Effective ceiling for `tier` — the tighter of per-tier and global.

        A `0` (unlimited) on either side defers to the other; `0` on both
        means genuinely unlimited.
        """
        tier_cap = self.per_tier.get(tier, 0)
        caps = [c for c in (tier_cap, self.max_concurrent_specialists) if c > 0]
        return min(caps) if caps else 0


def load_parallelism_config(root: Path | None = None) -> ParallelismConfig:
    """Parse the `parallelism:` block from `<root>/.nightly/config.yml`.

    Negative values are clamped to `0` (unlimited) rather than rejected —
    consistent with the other loaders, a nonsense value degrades to the
    most permissive reading instead of halting the run.
    """
    defaults = ParallelismConfig()
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
    block = data.get("parallelism") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return defaults

    def _coerce_int(source: dict[str, Any], key: str, default: int) -> int:
        try:
            return max(0, int(source.get(key, default)))
        except (TypeError, ValueError):
            return default

    per_tier = dict(defaults.per_tier)
    per_tier_raw = block.get("per_tier")
    if isinstance(per_tier_raw, dict):
        for tier in MODEL_TIERS:
            if tier in per_tier_raw:
                per_tier[tier] = _coerce_int(per_tier_raw, tier, per_tier[tier])

    return ParallelismConfig(
        max_concurrent_specialists=_coerce_int(
            block, "max_concurrent_specialists", defaults.max_concurrent_specialists
        ),
        max_worktrees=_coerce_int(block, "max_worktrees", defaults.max_worktrees),
        per_tier=per_tier,
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

    handoff_soft_ratio: float = 0.25
    """Fraction of a model's context window at which an agent should
    *finish the task it is on*, write a handoff summary, and let a fresh
    agent pick up the same goals with a clean context.

    Expressed as a ratio rather than an absolute so it scales with the
    dispatched model: 0.25 is 250K on a 1M-context model and 50K on a
    200K-context one. The agent is most useful in the first quarter of
    its window; past that, re-reading its own history crowds out the work.
    Set `0` to disable soft handoff."""

    handoff_hard_ratio: float = 0.50
    """Fraction of the context window at which an agent must **stop
    immediately** — mid-task if necessary — and emit a handoff summary.

    Unlike the soft threshold this is not a suggestion: an agent past half
    its window is close enough to the ceiling that finishing "just this
    one more step" risks truncation mid-write, which is the one failure
    mode that loses work rather than merely wasting tokens. Must exceed
    `handoff_soft_ratio`; `0` disables hard handoff."""

    default_context_tokens: int = 200_000
    """Assumed context window for a model with no declared size.

    Deliberately conservative — an unknown model is more likely to be a
    small one, and under-estimating costs an early (harmless) handoff
    while over-estimating costs a truncated one."""

    model_context_tokens: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_MODEL_CONTEXT_TOKENS)
    )
    """Per-model context-window sizes, merged over
    `DEFAULT_MODEL_CONTEXT_TOKENS`. Declare non-Anthropic models here so
    their handoff thresholds scale correctly."""


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

    def _coerce_ratio(key: str, default: float) -> float:
        # Ratios outside [0, 1] are meaningless — a "handoff at 150% of the
        # window" would never fire, which reads as "disabled" but isn't.
        try:
            value = float(context.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if 0.0 <= value <= 1.0 else default

    soft = _coerce_ratio("handoff_soft_ratio", defaults.handoff_soft_ratio)
    hard = _coerce_ratio("handoff_hard_ratio", defaults.handoff_hard_ratio)
    if soft and hard and soft > hard:
        # An inverted pair would make the soft threshold fire *after* the
        # hard stop, inverting the whole protocol. Fall back to defaults
        # rather than guessing which of the two the operator meant.
        _log.warning(
            "%s: context.handoff_soft_ratio (%s) exceeds handoff_hard_ratio (%s); using defaults",
            path,
            soft,
            hard,
        )
        soft, hard = defaults.handoff_soft_ratio, defaults.handoff_hard_ratio

    windows = dict(DEFAULT_MODEL_CONTEXT_TOKENS)
    windows_raw = context.get("model_context_tokens")
    if isinstance(windows_raw, dict):
        for model_id, size in windows_raw.items():
            try:
                tokens = int(size)
            except (TypeError, ValueError):
                _log.warning("%s: non-numeric context window %r for %r", path, size, model_id)
                continue
            if tokens > 0:
                windows[str(model_id)] = tokens

    return ContextConfig(
        budget_tokens=_coerce_int("budget_tokens", defaults.budget_tokens),
        digest_every_turns=_coerce_int("digest_every_turns", defaults.digest_every_turns),
        handoff_soft_ratio=soft,
        handoff_hard_ratio=hard,
        default_context_tokens=_coerce_int(
            "default_context_tokens", defaults.default_context_tokens
        ),
        model_context_tokens=windows,
    )


@dataclass(frozen=True)
class CompactConfig:
    """Configuration for session compaction (RFC 006)."""

    enabled: bool = True
    """Whether to enable boundary and threshold compaction triggers."""

    context_token_cap: int = 256_000
    """Context cap in tokens. Compaction is triggered when the estimated
    conversation size exceeds this."""


def load_compact_config(root: Path | None = None) -> CompactConfig:
    """Parse the `compact:` block from `<root>/.nightly/config.yml`.

    Defaults whenever the file is missing, unreadable, malformed, or has
    no `compact:` block. Individual missing/garbage keys fall back to
    their defaults."""
    defaults = CompactConfig()
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
    compact = data.get("compact") if isinstance(data, dict) else None
    if not isinstance(compact, dict):
        return defaults

    def _coerce_bool(key: str, default: bool) -> bool:
        val = compact.get(key)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if str(val).lower() in ("true", "1", "yes", "on"):
            return True
        if str(val).lower() in ("false", "0", "no", "off"):
            return False
        return default

    def _coerce_int(key: str, default: int) -> int:
        try:
            return int(compact.get(key, default))
        except (TypeError, ValueError):
            return default

    return CompactConfig(
        enabled=_coerce_bool("enabled", defaults.enabled),
        context_token_cap=_coerce_int("context_token_cap", defaults.context_token_cap),
    )


# ── default config template ──────────────────────────────────────────────

_CONFIG_YML_TEMPLATE = """\
# .nightly/config.yml — written by `nightly init`. Edit as needed.
# See `.nightly/config.yml.example` (if present) for the full schema, or
# `.planning/brainstorm.html` §05 for the design rationale.

hosts:
  - claude

git:
  base_branch:   main
  branch_prefix: nightly/
  wip_prefix:    nightly/wip-
  protected:     [main, master, "release/*"]
  # Where per-task worktrees are placed. Leave unset to nest them under a
  # sibling `<repo>-nightly/` dir. Set an absolute/`~` path to keep trees off a
  # synced filesystem — REQUIRED on macOS if this repo lives in iCloud Drive
  # (~/Documents, ~/Desktop), where FileProvider silently corrupts git state.
  # Nightly auto-relocates to ~/.cache/nightly/worktrees if it detects iCloud.
  # worktree_root: ~/.cache/nightly/worktrees

refuse:
  destructive_git:        true
  production_state:       true
  external_communication: true
  network_egress_unknown: true
  scope_creep:            true
  bypass_test_or_type:    true

# pr_feedback governs the `pr_rescue` cascade step.
# - `enabled` flips the whole feature off without removing the block.
# - `review_bots` extends the default bot allowlist (CodeRabbit, Cursor BugBot,
#   Copilot reviewer, Greptile, Amp, etc.) with project-specific accounts.
# - `treat_bots_as_human` flips a bot login into the "human" bucket — useful
#   for an internally-trusted automation that should outrank ordinary bots.
pr_feedback:
  enabled:              true
  review_bots:          []
  treat_bots_as_human:  []

# vault governs the .nightly/vault/ knowledge graph (RFC 003).
# - `enabled: false` skips the vault build step in `nightly brief`.
# - `open_on_brief: true` pops the dashboard in a browser at brief time.
vault:
  enabled:       true
  open_on_brief: false

# worktree governs the readiness probe (RFC 002).
# - `probe_enabled: false` skips the probe entirely.
# - `remediate_enabled: false` surfaces remediable failures rather than
#   auto-fixing via `uv sync` / `pre-commit install --install-hooks`.
worktree:
  probe_enabled:     true
  remediate_enabled: true

# ideate governs the proposer suite (RFC 009).
# - `category_ordering: false` reverts the cascade to score-only ordering
#   (pre-v0.0.6 behavior). With it on, cleaning proposals outrank
#   capability proposals even at lower scores — "fix what's broken
#   before inventing new things."
# - `synthesis.enabled: false` disables the LLM-driven SynthesisProposer
#   entirely; the three Phase-5 narrow proposers still run.
# - `synthesis.timeout_seconds` caps the host CLI spawn wall-clock.
# - `synthesis.max_proposals` caps total synthesis output so the morning
#   briefing stays readable.
ideate:
  category_ordering: true
  synthesis:
    enabled:          true
    timeout_seconds:  120
    max_proposals:    25

# agents governs how specialist sub-agents (implementer / tester /
# reviewer / researcher) are dispatched in interactive sessions.
# - `background_dispatch: true` (default, and the preferred setting for
#   Claude Code / Codex / Cursor / Antigravity sessions) — specialists
#   spawn as detached host processes via `nightly dispatch start <slug>
#   --role <role>` so the operator's chat stays free for other work.
#   Poll via `nightly dispatch status / tail / wait`.
# - `background_dispatch: false` — fall back to the host's native
#   Task-tool surface (blocking the calling chat until the sub-agent
#   returns). Use only when you explicitly want to watch the
#   specialist's progress in-band (debugging an unfamiliar host,
#   eyeballing a long-running review).
# `nightly run` headless ignores this preference — each task gets its
# own host process by construction.
agents:
  background_dispatch: true

# model_tiers routes each dispatch to a model sized for the job (RFC 007).
# Three tiers, named after task complexity rather than vendor:
# - `lite`      file search, summarization, docs. Wide fan-out, low effort.
# - `coding`    implementation + test authoring. The bulk of the fleet.
# - `reasoning` orchestration, result validation, merge adjudication.
# Role defaults live in code (SPECIALIST_TIER_DEFAULTS): implementer and
# tester → coding, reviewer → reasoning (review IS result validation, and
# a missed bug here is caught by nothing downstream), researcher → lite
# (file search + summarization over code already on disk). A plan's
# `model_tier:` frontmatter overrides the role default per task.
#
# `effort` is the deliberation dial. lite/coding sit at `low` on purpose:
# those agents should be writing files, not thinking about writing files,
# and `nightly verify` catches what under-thinking breaks. reasoning sits
# at `xhigh`, where effort actually buys correctness.
#
# Hosts absent from this block (codex, gemini, antigravity) fall through
# to the host CLI's own default model and log a friction note — wire your
# own ids below to enable routing for them.
__MODEL_TIERS_BLOCK__

# parallelism caps how wide the fleet runs (RFC 012). Defaults lean wide:
# background dispatch keeps the chat free, worktrees isolate the
# filesystem, and `nightly verify` gates every diff — so a wider fleet
# costs wall-clock, not correctness. 0 anywhere means "unlimited".
# The per_tier gradient is the cost control: run lite/coding agents wide
# and keep reasoning agents scarce, since those are both the expensive
# dispatches and the ones whose work mostly serializes anyway.
parallelism:
  max_concurrent_specialists: 8
  max_worktrees:              8
  per_tier:
    lite:      8
    coding:    6
    reasoning: 2

# context governs the context-compaction feature (v0.0.12). Nothing can
# programmatically trigger Claude Code's /compact, so Nightly instead makes
# compaction lossless (a digest re-injected via the SessionStart hook) and
# nudges the live session toward hygiene before it bloats.
# - `budget_tokens` is a SOFT ceiling: when the keepalive hook estimates the
#   session exceeds it, it prepends a "context diet" nudge to the continuation
#   prompt (finish delicate work first, lean on the digest, background heavy
#   work). 0 disables budget steering.
# - `digest_every_turns` writes .nightly/runs/<id>/digest.md every N keepalive
#   turns so the SessionStart(compact) hook re-injects fresh state. 0 disables
#   the interval write (the digest is still written on every planning-phase
#   reroute regardless).
# - `handoff_soft_ratio` / `handoff_hard_ratio` are the agent-recycling
#   protocol, expressed as fractions of the DISPATCHED MODEL's context
#   window so one setting governs a mixed fleet. At the defaults a
#   1M-context model finishes its task and hands off at 250K and hard-
#   stops at 500K; a 200K-context model does the same at 50K and 100K.
#   Soft = finish the current task, write a handoff summary, relaunch a
#   fresh agent with the same goals. Hard = stop now, mid-task, and
#   summarize — past half the window, "one more step" risks truncating a
#   write, which loses work rather than merely wasting tokens.
# - `model_context_tokens` declares window sizes for models Nightly does
#   not already know (any non-Anthropic model you route a tier to).
context:
  budget_tokens:      256000
  digest_every_turns: 1
  handoff_soft_ratio: 0.25
  handoff_hard_ratio: 0.50
  # model_context_tokens:
  #   <vendor model id>: 256000

# compact governs the session compaction triggers (RFC 006).
# - `enabled` flips both triggers (boundary and threshold) on or off.
# - `context_token_cap` is the threshold (in tokens) at which the mid-loop
#   trigger fires to compact the session context.
compact:
  enabled:           true
  context_token_cap: 256000
"""


def render_config_yml(
    tier_models: dict[HostId, dict[ModelTier, str]] | None = None,
    model_flags: dict[HostId, str] | None = None,
) -> str:
    """Render `.nightly/config.yml`, with the `model_tiers:` block filled in.

    `nightly init` passes what `nightly_core.model_probe` discovered from
    the harness that is running it, so the written config names the models
    that harness actually offers and the model-selection flag it actually
    accepts — rather than a table Nightly would have to keep current by
    hand. With no arguments this renders the seeded defaults, which is
    what `DEFAULT_CONFIG_YML` is.
    """
    models = tier_models if tier_models is not None else DEFAULT_TIER_MODELS
    flags = model_flags or {}

    lines = [
        "model_tiers:",
        "  enabled: true",
        "  effort:",
    ]
    lines += [f"    {tier + ':':<11}{DEFAULT_TIER_EFFORT[tier]}" for tier in MODEL_TIERS]

    for host in sorted(set(models) | set(flags)):
        host_tiers = models.get(host, {})
        if not host_tiers and host not in flags:
            continue
        lines.append(f"  {host}:")
        if host in flags:
            # Discovered from `<host> --help`; `build_argv` emits it verbatim.
            lines.append(f"    {'flag:':<11}{flags[host]}")
        lines += [
            f"    {tier + ':':<11}{host_tiers[tier]}" for tier in MODEL_TIERS if tier in host_tiers
        ]

    if not any(models.values()) and not flags:
        lines.append("  # no host CLI was detected on PATH at init time.")
        lines.append("  # Re-run `nightly doctor` after installing one, or")
        lines.append("  # add `<host>: {lite:, coding:, reasoning:}` by hand.")

    return _CONFIG_YML_TEMPLATE.replace("__MODEL_TIERS_BLOCK__", "\n".join(lines))


DEFAULT_CONFIG_YML = render_config_yml()
"""Canonical `.nightly/config.yml` scaffold with seeded (undiscovered)
model tiers. `nightly init` prefers `render_config_yml(...)` with probe
results; this is the fallback and the shape tests pin."""
