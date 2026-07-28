"""Every `Literal` member must survive the functions that dispatch on it.

Several tables are keyed by a `Literal` and indexed directly — `_PROMPTS`,
`TIER_FAMILIES`, `DEFAULT_TIER_EFFORT`, `_EFFORT_DIRECTIVES`. Adding a
member to the `Literal` without adding a row raises `KeyError` at the
moment the new member is first used, which for a dispatch table means
3am, inside a spawned subprocess, with the traceback in a log nobody is
reading.

These tests exercise the **public function** for every member rather than
asserting on dict keys. The distinction matters: a key-presence assertion
passes for a row whose value is empty, wrong-typed, or unreachable behind
a guard. Calling the function is the actual contract.

All registries are total as of writing — this is prevention, not a fix.
"""

from __future__ import annotations

from typing import get_args

import pytest

from nightly_core.config import (
    DEFAULT_TIER_EFFORT,
    ModelTierConfig,
    ParallelismConfig,
)
from nightly_core.contract import (
    MODEL_TIERS,
    HostId,
    ModelTier,
    ReasoningEffort,
    SpecialistRole,
)
from nightly_core.model_probe import TIER_FAMILIES, assign_tiers, tier_of_model
from nightly_core.routing import effort_directive, resolve_model_for_task
from nightly_core.specialists import specialist_prompt, tier_for_role


@pytest.mark.parametrize("role", get_args(SpecialistRole))
def test_every_role_has_a_usable_prompt(role: SpecialistRole) -> None:
    prompt = specialist_prompt(role)
    assert prompt.strip(), f"{role} has an empty prompt"
    # A prompt that never names its own role is almost certainly a
    # copy-paste of a sibling's.
    assert role in prompt.lower()


@pytest.mark.parametrize("role", get_args(SpecialistRole))
def test_every_role_resolves_to_a_real_tier(role: SpecialistRole) -> None:
    assert tier_for_role(role) in MODEL_TIERS


@pytest.mark.parametrize("tier", MODEL_TIERS)
def test_every_tier_has_families_and_an_effort(tier: ModelTier) -> None:
    assert TIER_FAMILIES[tier], f"{tier} has no model families"
    assert DEFAULT_TIER_EFFORT[tier] in get_args(ReasoningEffort)


@pytest.mark.parametrize("tier", MODEL_TIERS)
def test_every_tier_round_trips_through_family_matching(tier: ModelTier) -> None:
    """A family listed under a tier must classify back to that tier —
    otherwise `assign_tiers` would file a model under one band and
    `tier_of_model` report it as another."""
    sample = f"vendor-{TIER_FAMILIES[tier][0]}-1"
    assert tier_of_model(sample) == tier
    assert assign_tiers([sample]) == {tier: sample}


@pytest.mark.parametrize("effort", get_args(ReasoningEffort))
def test_every_effort_level_has_a_directive(effort: ReasoningEffort) -> None:
    text = effort_directive(effort)
    assert text.strip(), f"{effort} has no directive"


@pytest.mark.parametrize("host", get_args(HostId))
@pytest.mark.parametrize("tier", MODEL_TIERS)
def test_binding_resolves_for_every_host_and_tier(host: HostId, tier: ModelTier) -> None:
    """Hosts without a seeded map must yield `model=None`, never raise —
    an unbound host falls through to its CLI default by design."""
    binding = ModelTierConfig().binding(host, tier)
    assert binding.tier == tier
    assert binding.effort in get_args(ReasoningEffort)


@pytest.mark.parametrize("host", get_args(HostId))
@pytest.mark.parametrize("role", get_args(SpecialistRole))
def test_dispatch_resolves_for_every_host_and_role(host: HostId, role: SpecialistRole) -> None:
    resolved = resolve_model_for_task(host=host, role=role, config=ModelTierConfig())
    assert resolved.tier in MODEL_TIERS
    assert resolved.source in {"plan", "role", "disabled"}


@pytest.mark.parametrize("tier", MODEL_TIERS)
def test_parallelism_answers_for_every_tier(tier: ModelTier) -> None:
    assert ParallelismConfig().limit_for(tier) >= 0


def test_tier_families_do_not_overlap() -> None:
    """A family string in two tiers makes classification order-dependent,
    so the same model id would land in different bands depending on which
    tier happened to be checked first."""
    seen: dict[str, ModelTier] = {}
    for tier in MODEL_TIERS:
        for family in TIER_FAMILIES[tier]:
            assert family not in seen, (
                f"family {family!r} appears in both {seen.get(family)} and {tier}"
            )
            seen[family] = tier
