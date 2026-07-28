"""Resolve a dispatch to a concrete model, effort, and context budget.

Three questions get answered here, and they are deliberately kept in one
module because they are the same decision seen from three angles:

1. **Which model?** — `resolve_model_for_task` walks the RFC 007
   resolution order (plan override → specialist default → host binding).
2. **How hard should it think?** — the same call returns the tier's
   reasoning effort, so a lite-tier agent is told to act rather than
   deliberate.
3. **When should it hand off?** — `resolve_context_thresholds` scales the
   soft/hard handoff points to whatever context window the resolved model
   actually has.

Nothing here performs I/O or spawns anything. Callers load config once
(`load_model_tier_config`, `load_context_config`) and pass it in, which
keeps this module trivially testable and safe to call from the keepalive
hook's hot path.
"""

from __future__ import annotations

from dataclasses import dataclass

from nightly_core.config import ContextConfig, ModelTierConfig
from nightly_core.contract import HostId, ModelTier, ReasoningEffort, SpecialistRole
from nightly_core.specialists import tier_for_role

__all__ = [
    "ContextThresholds",
    "ResolvedDispatch",
    "context_window_for",
    "effort_directive",
    "resolve_context_thresholds",
    "resolve_model_for_task",
]


_EFFORT_DIRECTIVES: dict[ReasoningEffort, str] = {
    "low": (
        "Work at LOW deliberation. Act rather than plan: make the edit, run "
        "the check, move on. Consolidate tool calls, skip preamble, and do "
        "not narrate routine actions or survey options you will not take. "
        "`nightly verify` is the correctness gate — reach it quickly rather "
        "than reasoning your way to certainty first."
    ),
    "medium": (
        "Work at MEDIUM deliberation. Think through the approach once, then "
        "execute. Keep preamble short and avoid exploratory detours."
    ),
    "high": (
        "Work at HIGH deliberation. This task is intelligence-sensitive: "
        "reason carefully before acting, and state the reasoning that "
        "changes the outcome."
    ),
    "xhigh": (
        "Work at VERY HIGH deliberation. You are the judgment step — "
        "orchestration, validation, or merge adjudication. Nothing "
        "downstream re-checks your conclusion, so verify claims against "
        "evidence rather than plausibility, and say plainly what you could "
        "not confirm."
    ),
    "max": (
        "Work at MAXIMUM deliberation. Correctness dominates cost here: "
        "exhaust the alternatives, verify every claim against evidence, and "
        "flag anything you could not confirm."
    ),
}


def effort_directive(effort: ReasoningEffort) -> str:
    """Prompt text telling a dispatched agent how much to deliberate.

    Injected into the dispatch prompt rather than passed as a CLI flag.
    The flag surface differs per host and several hosts expose none, while
    prompt text works everywhere and degrades to a harmless no-op on a
    model that ignores it — see RFC 007 Resolved #11.
    """
    return _EFFORT_DIRECTIVES[effort]


@dataclass(frozen=True)
class ResolvedDispatch:
    """Everything a dispatch needs to know about its own model budget."""

    tier: ModelTier
    model: str | None
    """Concrete model id, or None when no binding exists for this host and
    the dispatch should fall through to the host CLI's default model."""

    effort: ReasoningEffort
    source: str
    """Where the tier came from — `plan`, `role`, or `disabled`. Surfaced
    in the briefing's tier breakdown so an operator can tell a deliberate
    override from a default."""

    @property
    def fell_back(self) -> bool:
        """True when tier routing produced no concrete model id.

        Callers surface this in the briefing's "Friction caught" section:
        it means the host has no `model_tiers:` entry, so the dispatch ran
        on whatever the host CLI defaults to rather than on the tier the
        plan asked for.
        """
        return self.model is None and self.source != "disabled"


def resolve_model_for_task(
    *,
    host: HostId,
    role: SpecialistRole,
    config: ModelTierConfig,
    plan_tier: ModelTier | None = None,
) -> ResolvedDispatch:
    """Resolve `(tier, model, effort)` for one dispatch — RFC 007 Resolved #6.

    Resolution order:

    1. `plan_tier` (from the plan's `model_tier:` frontmatter) wins when
       present. The agent sets it at scoping time for the cases where role
       and complexity diverge — a one-line README fix dispatched through
       `implementer`, or an architecture audit dispatched through
       `researcher`.
    2. Otherwise the role's default from `SPECIALIST_TIER_DEFAULTS`.
    3. The tier is then mapped to a concrete model id via the per-host
       `model_tiers:` block. A host with no entry yields `model=None`.

    With `config.enabled` False the whole feature is bypassed: the coding
    tier's effort is returned (a neutral middle) and `model` is None, so
    dispatch behaves exactly as it did before RFC 007.
    """
    if not config.enabled:
        return ResolvedDispatch(
            tier="coding",
            model=None,
            effort=config.effort.get("coding", "medium"),
            source="disabled",
        )

    tier: ModelTier = plan_tier or tier_for_role(role)
    binding = config.binding(host, tier)
    return ResolvedDispatch(
        tier=tier,
        model=binding.model,
        effort=binding.effort,
        source="plan" if plan_tier else "role",
    )


@dataclass(frozen=True)
class ContextThresholds:
    """Soft and hard context-handoff points, in absolute tokens."""

    window_tokens: int
    """The resolved model's full context window."""

    soft_tokens: int
    """Finish the current task, write a handoff summary, and relaunch a
    fresh agent with the same goals. `0` = soft handoff disabled."""

    hard_tokens: int
    """Stop immediately — mid-task if necessary — and write the handoff
    summary. `0` = hard handoff disabled."""

    def breach(self, used_tokens: int) -> str | None:
        """Classify `used_tokens` against the thresholds.

        Returns `"hard"`, `"soft"`, or None. Hard is checked first so a
        session that blew past both gets the stop-now instruction rather
        than the finish-then-hand-off one.
        """
        if self.hard_tokens and used_tokens >= self.hard_tokens:
            return "hard"
        if self.soft_tokens and used_tokens >= self.soft_tokens:
            return "soft"
        return None


def context_window_for(model: str | None, config: ContextConfig) -> int:
    """Context-window size for `model`, in tokens.

    Falls back to `config.default_context_tokens` for an unknown or absent
    model id — the conservative choice, since an unrecognized model is
    more likely to be small than large.
    """
    if not model:
        return config.default_context_tokens
    return config.model_context_tokens.get(model, config.default_context_tokens)


def resolve_context_thresholds(
    model: str | None,
    config: ContextConfig,
) -> ContextThresholds:
    """Scale the handoff ratios to `model`'s actual context window.

    This is what lets one pair of ratios govern a heterogeneous fleet: at
    the default 0.25 / 0.50, a 1M-context model hands off softly at 250K
    and hard-stops at 500K, while a 200K-context model hands off at 50K
    and hard-stops at 100K. The operator tunes *when* an agent should
    recycle, not *how many tokens* each model gets.
    """
    window = context_window_for(model, config)
    return ContextThresholds(
        window_tokens=window,
        soft_tokens=int(window * config.handoff_soft_ratio),
        hard_tokens=int(window * config.handoff_hard_ratio),
    )
