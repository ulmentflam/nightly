"""Tests for RFC 007 model-tier routing and RFC 012 context handoff.

Covers every branch of the resolution order (plan override, role default,
host miss, disabled), the config merge semantics, and the context-window
scaling that lets one pair of ratios govern a heterogeneous fleet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nightly_core.config import (
    DEFAULT_TIER_EFFORT,
    ContextConfig,
    ModelTierConfig,
    load_context_config,
    load_model_tier_config,
    load_parallelism_config,
)
from nightly_core.contract import MODEL_TIERS
from nightly_core.plans import PlanRecord
from nightly_core.routing import (
    context_window_for,
    resolve_context_thresholds,
    resolve_model_for_task,
)
from nightly_core.specialists import SPECIALIST_TIER_DEFAULTS, all_roles, tier_for_role


def _write_config(root: Path, body: str) -> None:
    (root / ".nightly").mkdir(parents=True, exist_ok=True)
    (root / ".nightly" / "config.yml").write_text(body, encoding="utf-8")


# ── specialist defaults ───────────────────────────────────────────────────


def test_every_role_has_a_tier_default() -> None:
    """A role without an entry would silently route to the fallback tier."""
    assert set(SPECIALIST_TIER_DEFAULTS) == set(all_roles())


def test_tier_defaults_follow_cost_of_being_wrong() -> None:
    """Reviewer is the one role nothing downstream double-checks."""
    assert SPECIALIST_TIER_DEFAULTS["implementer"] == "coding"
    assert SPECIALIST_TIER_DEFAULTS["tester"] == "coding"
    assert SPECIALIST_TIER_DEFAULTS["reviewer"] == "reasoning"
    assert SPECIALIST_TIER_DEFAULTS["researcher"] == "lite"


def test_unknown_role_falls_back_to_coding() -> None:
    """A future role added to the Literal but not the table degrades safely."""
    assert tier_for_role("archaeologist") == "coding"  # type: ignore[arg-type]


# ── plan frontmatter override ─────────────────────────────────────────────


@pytest.mark.parametrize("tier", MODEL_TIERS)
def test_plan_model_tier_parses_each_tier(tmp_path: Path, tier: str) -> None:
    plan = PlanRecord(path=tmp_path / "plan.md", metadata={"model_tier": tier}, body="")
    assert plan.model_tier == tier


@pytest.mark.parametrize("raw", ["", "   ", "REASONING ", "Lite"])
def test_plan_model_tier_is_case_and_space_insensitive(tmp_path: Path, raw: str) -> None:
    plan = PlanRecord(path=tmp_path / "plan.md", metadata={"model_tier": raw}, body="")
    expected = raw.strip().lower() or None
    assert plan.model_tier == expected


@pytest.mark.parametrize("raw", ["turbo", "7", "coding-tier"])
def test_plan_model_tier_rejects_garbage(tmp_path: Path, raw: str) -> None:
    """A frontmatter typo must fall back to the role default, not crash."""
    plan = PlanRecord(path=tmp_path / "plan.md", metadata={"model_tier": raw}, body="")
    assert plan.model_tier is None


def test_plan_without_model_tier_is_none(tmp_path: Path) -> None:
    plan = PlanRecord(path=tmp_path / "plan.md", metadata={}, body="")
    assert plan.model_tier is None


# ── resolution order ──────────────────────────────────────────────────────


def test_resolves_role_default_when_plan_is_silent() -> None:
    cfg = ModelTierConfig()
    resolved = resolve_model_for_task(host="claude", role="researcher", config=cfg)
    assert resolved.tier == "lite"
    assert resolved.model == "claude-haiku-4-5"
    assert resolved.source == "role"


def test_plan_override_beats_role_default() -> None:
    """An implementer task that is really a one-line doc fix routes lite."""
    cfg = ModelTierConfig()
    resolved = resolve_model_for_task(
        host="claude", role="implementer", config=cfg, plan_tier="lite"
    )
    assert resolved.tier == "lite"
    assert resolved.model == "claude-haiku-4-5"
    assert resolved.source == "plan"


def test_reviewer_routes_to_reasoning_model() -> None:
    cfg = ModelTierConfig()
    resolved = resolve_model_for_task(host="claude", role="reviewer", config=cfg)
    assert resolved.tier == "reasoning"
    assert resolved.model == "claude-opus-5"
    assert resolved.effort == "xhigh"


def test_host_without_binding_falls_back_to_cli_default() -> None:
    """codex ships no default map — dispatch keeps working, flagged as friction."""
    cfg = ModelTierConfig()
    resolved = resolve_model_for_task(host="codex", role="implementer", config=cfg)
    assert resolved.tier == "coding"
    assert resolved.model is None
    assert resolved.fell_back is True


def test_disabled_config_bypasses_routing_entirely() -> None:
    cfg = ModelTierConfig(enabled=False)
    resolved = resolve_model_for_task(host="claude", role="reviewer", config=cfg)
    assert resolved.model is None
    assert resolved.source == "disabled"
    # `disabled` is a deliberate opt-out, not friction to report.
    assert resolved.fell_back is False


def test_effort_tracks_tier_not_role() -> None:
    cfg = ModelTierConfig()
    fast = resolve_model_for_task(host="claude", role="implementer", config=cfg)
    slow = resolve_model_for_task(host="claude", role="reviewer", config=cfg)
    assert fast.effort == "low"
    assert slow.effort == "xhigh"


# ── config loading ────────────────────────────────────────────────────────


def test_missing_config_yields_defaults(tmp_path: Path) -> None:
    cfg = load_model_tier_config(tmp_path)
    assert cfg.enabled is True
    assert cfg.binding("claude", "coding").model == "claude-sonnet-5"


def test_malformed_yaml_degrades_to_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_tiers: [unclosed\n")
    assert load_model_tier_config(tmp_path).effort == DEFAULT_TIER_EFFORT


def test_partial_host_entry_merges_over_defaults(tmp_path: Path) -> None:
    """Declaring one tier must not blank the other two."""
    _write_config(tmp_path, "model_tiers:\n  claude:\n    lite: my-tiny-model\n")
    cfg = load_model_tier_config(tmp_path)
    assert cfg.binding("claude", "lite").model == "my-tiny-model"
    assert cfg.binding("claude", "coding").model == "claude-sonnet-5"
    assert cfg.binding("claude", "reasoning").model == "claude-opus-5"


def test_operator_can_bind_an_unseeded_host(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_tiers:\n  codex:\n    coding: some-vendor-model\n")
    cfg = load_model_tier_config(tmp_path)
    assert cfg.binding("codex", "coding").model == "some-vendor-model"


def test_unknown_host_key_is_ignored(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_tiers:\n  emacs:\n    coding: nope\n")
    cfg = load_model_tier_config(tmp_path)
    assert "emacs" not in cfg.models


def test_unknown_effort_value_falls_back(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_tiers:\n  effort:\n    coding: ludicrous\n")
    cfg = load_model_tier_config(tmp_path)
    assert cfg.effort["coding"] == DEFAULT_TIER_EFFORT["coding"]


def test_effort_override_is_honored(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_tiers:\n  effort:\n    coding: medium\n")
    assert load_model_tier_config(tmp_path).effort["coding"] == "medium"


def test_enabled_false_round_trips(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_tiers:\n  enabled: false\n")
    assert load_model_tier_config(tmp_path).enabled is False


# ── parallelism ───────────────────────────────────────────────────────────


def test_parallelism_defaults_lean_wide(tmp_path: Path) -> None:
    cfg = load_parallelism_config(tmp_path)
    assert cfg.max_concurrent_specialists == 8
    assert cfg.max_worktrees == 8


def test_reasoning_tier_is_the_scarce_one(tmp_path: Path) -> None:
    cfg = load_parallelism_config(tmp_path)
    assert cfg.per_tier["reasoning"] < cfg.per_tier["coding"] < cfg.per_tier["lite"]


def test_limit_for_takes_the_tighter_of_the_two_caps(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "parallelism:\n  max_concurrent_specialists: 4\n  per_tier:\n    lite: 12\n",
    )
    cfg = load_parallelism_config(tmp_path)
    # Global cap of 4 clamps the wide lite tier.
    assert cfg.limit_for("lite") == 4
    # Reasoning stays at its own tighter per-tier cap.
    assert cfg.limit_for("reasoning") == 2


def test_zero_means_unlimited_on_both_axes(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "parallelism:\n  max_concurrent_specialists: 0\n  per_tier:\n    lite: 0\n",
    )
    assert load_parallelism_config(tmp_path).limit_for("lite") == 0


def test_negative_parallelism_clamps_to_unlimited(tmp_path: Path) -> None:
    _write_config(tmp_path, "parallelism:\n  max_worktrees: -3\n")
    assert load_parallelism_config(tmp_path).max_worktrees == 0


# ── context handoff thresholds ────────────────────────────────────────────


def test_thresholds_scale_with_a_1m_window() -> None:
    """The operator's stated shape: 256K-ish soft, 500K hard on a 1M model."""
    t = resolve_context_thresholds("claude-opus-5", ContextConfig())
    assert t.window_tokens == 1_000_000
    assert t.soft_tokens == 250_000
    assert t.hard_tokens == 500_000


def test_thresholds_scale_down_for_a_small_window() -> None:
    """Same ratios, proportionally earlier handoff on a 200K model."""
    t = resolve_context_thresholds("claude-haiku-4-5", ContextConfig())
    assert t.window_tokens == 200_000
    assert t.soft_tokens == 50_000
    assert t.hard_tokens == 100_000


def test_unknown_model_uses_the_conservative_default_window() -> None:
    cfg = ContextConfig()
    assert context_window_for("some-new-model", cfg) == cfg.default_context_tokens
    assert context_window_for(None, cfg) == cfg.default_context_tokens


def test_breach_classifies_soft_then_hard() -> None:
    t = resolve_context_thresholds("claude-opus-5", ContextConfig())
    assert t.breach(1_000) is None
    assert t.breach(250_000) == "soft"
    assert t.breach(499_999) == "soft"
    assert t.breach(500_000) == "hard"


def test_hard_breach_wins_when_both_are_exceeded() -> None:
    """An agent past both thresholds must stop, not finish-then-hand-off."""
    t = resolve_context_thresholds("claude-opus-5", ContextConfig())
    assert t.breach(900_000) == "hard"


def test_zero_ratio_disables_a_threshold() -> None:
    cfg = ContextConfig(handoff_soft_ratio=0.0)
    t = resolve_context_thresholds("claude-opus-5", cfg)
    assert t.soft_tokens == 0
    assert t.breach(300_000) is None
    assert t.breach(600_000) == "hard"


def test_operator_declared_window_feeds_the_thresholds(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "context:\n  model_context_tokens:\n    some-vendor-model: 256000\n",
    )
    cfg = load_context_config(tmp_path)
    t = resolve_context_thresholds("some-vendor-model", cfg)
    assert t.soft_tokens == 64_000
    assert t.hard_tokens == 128_000


def test_inverted_ratios_fall_back_to_defaults(tmp_path: Path) -> None:
    """Soft firing after hard would invert the whole protocol."""
    _write_config(
        tmp_path,
        "context:\n  handoff_soft_ratio: 0.9\n  handoff_hard_ratio: 0.2\n",
    )
    cfg = load_context_config(tmp_path)
    assert cfg.handoff_soft_ratio == 0.25
    assert cfg.handoff_hard_ratio == 0.50


@pytest.mark.parametrize("bad", ["1.7", "-0.2", "banana"])
def test_out_of_range_ratio_degrades_to_default(tmp_path: Path, bad: str) -> None:
    _write_config(tmp_path, f"context:\n  handoff_soft_ratio: {bad}\n")
    assert load_context_config(tmp_path).handoff_soft_ratio == 0.25


def test_context_config_keeps_existing_keys_working(tmp_path: Path) -> None:
    """A pre-RFC-012 config must still parse its original two keys."""
    _write_config(tmp_path, "context:\n  budget_tokens: 128000\n  digest_every_turns: 3\n")
    cfg = load_context_config(tmp_path)
    assert cfg.budget_tokens == 128_000
    assert cfg.digest_every_turns == 3
    assert cfg.handoff_soft_ratio == 0.25


# ── doctor surfacing ──────────────────────────────────────────────────────


def test_doctor_reports_ok_when_every_configured_host_is_bound(tmp_path: Path) -> None:
    from nightly_core.doctor import _check_model_tiers

    _write_config(tmp_path, "hosts:\n  - claude\n")
    check = _check_model_tiers(tmp_path)
    assert check.status == "ok"
    assert "claude-opus-5" in check.detail


def test_doctor_warns_when_a_configured_host_has_no_binding(tmp_path: Path) -> None:
    """codex ships no seeded map — routing is inert there until wired."""
    from nightly_core.doctor import _check_model_tiers

    _write_config(tmp_path, "hosts:\n  - claude\n  - codex\n")
    check = _check_model_tiers(tmp_path)
    assert check.status == "warning"
    assert "codex" in check.detail


def test_doctor_skips_when_routing_is_disabled(tmp_path: Path) -> None:
    from nightly_core.doctor import _check_model_tiers

    _write_config(tmp_path, "hosts:\n  - codex\nmodel_tiers:\n  enabled: false\n")
    assert _check_model_tiers(tmp_path).status == "skipped"


def test_doctor_never_repairs_model_tiers(tmp_path: Path) -> None:
    """An advisory check must not rewrite the operator's config."""
    from nightly_core.doctor import _check_model_tiers

    _write_config(tmp_path, "hosts:\n  - codex\n")
    before = (tmp_path / ".nightly" / "config.yml").read_text(encoding="utf-8")
    _check_model_tiers(tmp_path)
    assert (tmp_path / ".nightly" / "config.yml").read_text(encoding="utf-8") == before


# ── tier/model agreement (doctor) ─────────────────────────────────────────


def test_default_config_is_internally_consistent(tmp_path: Path) -> None:
    """The shipped defaults must not themselves trip the check."""
    from nightly_core.doctor import _check_tier_sanity

    _write_config(tmp_path, "hosts:\n  - claude\n")
    assert _check_tier_sanity(tmp_path).status == "ok"


def test_swapped_tiers_are_caught(tmp_path: Path) -> None:
    """The expensive silent misconfiguration: routing keeps working and
    only the bill notices."""
    from nightly_core.doctor import _check_tier_sanity

    _write_config(
        tmp_path,
        "hosts:\n  - claude\nmodel_tiers:\n  claude:\n"
        "    lite: claude-opus-5\n    reasoning: claude-haiku-4-5\n",
    )
    check = _check_tier_sanity(tmp_path)
    assert check.status == "warning"
    assert "lite=claude-opus-5" in check.detail
    assert "reasoning-tier model" in check.detail


def test_unrecognized_family_is_skipped_not_guessed(tmp_path: Path) -> None:
    """A membership test against advertised ids would flag correct config
    as broken — `claude --help` names four tokens and none of them are
    the production ids Nightly ships. Family matching only."""
    from nightly_core.doctor import _check_tier_sanity

    _write_config(
        tmp_path,
        "hosts:\n  - claude\nmodel_tiers:\n  claude:\n    coding: some-vendor-model-x\n",
    )
    assert _check_tier_sanity(tmp_path).status == "ok"


def test_check_is_skipped_when_routing_is_disabled(tmp_path: Path) -> None:
    from nightly_core.doctor import _check_tier_sanity

    _write_config(tmp_path, "hosts:\n  - claude\nmodel_tiers:\n  enabled: false\n")
    assert _check_tier_sanity(tmp_path).status == "skipped"


def test_tier_of_model_has_no_opinion_on_unknown_ids() -> None:
    from nightly_core.model_probe import tier_of_model

    assert tier_of_model("claude-opus-5") == "reasoning"
    assert tier_of_model("claude-haiku-4-5") == "lite"
    assert tier_of_model("mystery-model-9") is None


def test_partially_bound_host_is_reported_as_unbound(tmp_path: Path) -> None:
    """A host bound for only one tier looks configured while the other two
    silently fall through to the host CLI's default — the more dangerous
    shape than an entirely empty map."""
    from nightly_core.doctor import _check_model_tiers

    _write_config(
        tmp_path,
        "hosts:\n  - codex\nmodel_tiers:\n  codex:\n    coding: some-vendor-model\n",
    )
    check = _check_model_tiers(tmp_path)
    assert check.status == "warning"
    assert "codex" in check.detail
    assert "lite" in check.detail
    assert "reasoning" in check.detail
    # The bound tier must not be listed as missing.
    assert "coding" not in check.detail.split("(")[1].split(")")[0]


def test_ok_detail_covers_every_configured_host(tmp_path: Path) -> None:
    """The detail used to sample only the first host, so a second host's
    bindings were never shown."""
    from nightly_core.doctor import _check_model_tiers

    _write_config(tmp_path, "hosts:\n  - claude\n  - cursor\n")
    check = _check_model_tiers(tmp_path)
    assert check.status == "ok"
    assert "claude:" in check.detail
    assert "cursor:" in check.detail
