"""The scaffold config template must be one artifact, not two.

`nightly init` and `nightly doctor --fix` both write `.nightly/config.yml`.
They used to hold private copies of the template, which drifted: doctor's
copy had lost the `vault:` and `worktree:` blocks entirely, so a repo
repaired by doctor got a materially different config than a freshly
initialized one. These tests pin the consolidation.
"""

from __future__ import annotations

import yaml

from nightly_core.cli import _DEFAULT_CONFIG_YML as CLI_TEMPLATE
from nightly_core.config import (
    DEFAULT_CONFIG_YML,
    ContextConfig,
    ModelTierConfig,
    ParallelismConfig,
)
from nightly_core.contract import MODEL_TIERS
from nightly_core.doctor import _DEFAULT_CONFIG_YML as DOCTOR_TEMPLATE


def test_init_and_doctor_write_the_same_template() -> None:
    """The drift this consolidation fixed must not come back."""
    assert CLI_TEMPLATE == DOCTOR_TEMPLATE == DEFAULT_CONFIG_YML


def test_template_is_valid_yaml() -> None:
    assert isinstance(yaml.safe_load(DEFAULT_CONFIG_YML), dict)


def test_template_carries_every_block_a_loader_reads() -> None:
    """A loader with no template block is a knob nobody discovers."""
    data = yaml.safe_load(DEFAULT_CONFIG_YML)
    expected = {
        "hosts",
        "git",
        "refuse",
        "pr_feedback",
        "vault",
        "worktree",
        "ideate",
        "agents",
        "model_tiers",
        "parallelism",
        "context",
        "compact",
    }
    assert expected <= set(data)


def test_template_model_tiers_bind_every_tier_for_claude() -> None:
    tiers = yaml.safe_load(DEFAULT_CONFIG_YML)["model_tiers"]["claude"]
    assert set(tiers) == set(MODEL_TIERS)
    assert all(isinstance(v, str) and v for v in tiers.values())


def test_template_matches_the_dataclass_defaults() -> None:
    """A template that disagrees with code defaults is a silent behavior change:
    an initialized repo would behave differently from a config-less one."""
    data = yaml.safe_load(DEFAULT_CONFIG_YML)

    tiers = ModelTierConfig()
    assert data["model_tiers"]["enabled"] is tiers.enabled
    assert data["model_tiers"]["effort"] == dict(tiers.effort)
    assert data["model_tiers"]["claude"] == tiers.models["claude"]

    par = ParallelismConfig()
    assert data["parallelism"]["max_concurrent_specialists"] == par.max_concurrent_specialists
    assert data["parallelism"]["max_worktrees"] == par.max_worktrees
    assert data["parallelism"]["per_tier"] == dict(par.per_tier)

    ctx = ContextConfig()
    assert data["context"]["handoff_soft_ratio"] == ctx.handoff_soft_ratio
    assert data["context"]["handoff_hard_ratio"] == ctx.handoff_hard_ratio
    assert data["context"]["budget_tokens"] == ctx.budget_tokens
