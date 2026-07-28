"""Tests for RFC 012 Phase B — parallelism caps enforced at admission.

The config landed in Phase A declared caps that nothing read. These tests
pin the enforcement: a cap that doesn't refuse anything is a comment.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nightly_core.config import ParallelismConfig
from nightly_core.contract import MODEL_TIERS, ModelTier
from nightly_core.dispatch import (
    BackgroundDispatchResult,
    admission_blocked,
    tier_utilization,
)


def _dispatch(tier: ModelTier | None, *, pid: int | None = None) -> BackgroundDispatchResult:
    """A live dispatch record. Defaults to this process's own PID so the
    `is_alive` check in `active_dispatches` sees it as running."""
    return BackgroundDispatchResult(
        slug=f"task-{tier}-{pid or os.getpid()}",
        role="implementer",
        host="claude",
        pid=pid or os.getpid(),
        log_path=Path("/tmp/dispatch.log"),
        started_at=datetime.now(UTC),
        status="running",
        tier=tier,
    )


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch):
    """Patch `active_dispatches` so tests declare the fleet directly.

    Spawning real processes to test a counting rule would be slow and
    flaky; the liveness filter itself is covered separately below.
    """

    def _set(dispatches: list[BackgroundDispatchResult]) -> None:
        monkeypatch.setattr(
            "nightly_core.dispatch.active_dispatches",
            lambda _root=None: dispatches,
        )

    return _set


# ── admission ─────────────────────────────────────────────────────────────


def test_admits_when_the_fleet_is_empty(live) -> None:
    live([])
    assert admission_blocked("coding", ParallelismConfig()) is None


def test_admits_below_the_tier_cap(live) -> None:
    live([_dispatch("reasoning")])
    # reasoning cap is 2
    assert admission_blocked("reasoning", ParallelismConfig()) is None


def test_blocks_at_the_tier_cap(live) -> None:
    live([_dispatch("reasoning"), _dispatch("reasoning")])
    reason = admission_blocked("reasoning", ParallelismConfig())
    assert reason is not None
    assert "reasoning" in reason
    assert "cap is 2" in reason


def test_a_full_tier_does_not_block_a_different_tier(live) -> None:
    """The per-tier gradient is the point — a saturated reasoning tier must
    not stall the wide lite fleet."""
    live([_dispatch("reasoning"), _dispatch("reasoning")])
    assert admission_blocked("lite", ParallelismConfig()) is None


def test_blocks_at_the_global_cap_even_with_tier_headroom(live) -> None:
    config = ParallelismConfig(max_concurrent_specialists=3)
    live([_dispatch("lite"), _dispatch("lite"), _dispatch("coding")])
    reason = admission_blocked("lite", config)
    assert reason is not None
    assert "global cap is 3" in reason


def test_untiered_dispatches_count_toward_the_global_cap(live) -> None:
    """Pre-RFC-012 state files have no tier; they still occupy a slot."""
    config = ParallelismConfig(max_concurrent_specialists=2)
    live([_dispatch(None), _dispatch(None)])
    assert admission_blocked("lite", config) is not None


def test_untiered_dispatches_do_not_count_against_a_tier(live) -> None:
    config = ParallelismConfig(max_concurrent_specialists=0)
    live([_dispatch(None), _dispatch(None), _dispatch(None)])
    assert admission_blocked("reasoning", config) is None


def test_zero_cap_means_unlimited(live) -> None:
    config = ParallelismConfig(
        max_concurrent_specialists=0,
        per_tier=dict.fromkeys(MODEL_TIERS, 0),
    )
    live([_dispatch("reasoning") for _ in range(50)])
    assert admission_blocked("reasoning", config) is None


def test_global_cap_clamps_a_wider_tier_cap(live) -> None:
    """`limit_for` takes the tighter of the two, so a lite cap of 12 under a
    global cap of 2 admits only 2."""
    config = ParallelismConfig(
        max_concurrent_specialists=2,
        per_tier={"lite": 12, "coding": 6, "reasoning": 2},
    )
    live([_dispatch("lite")])
    assert admission_blocked("lite", config) is None
    live([_dispatch("lite"), _dispatch("lite")])
    assert admission_blocked("lite", config) is not None


def test_blocking_never_suggests_a_cheaper_tier(live) -> None:
    """A silent downgrade would hand back a lite review nobody asked for."""
    live([_dispatch("reasoning"), _dispatch("reasoning")])
    reason = admission_blocked("reasoning", ParallelismConfig())
    assert reason is not None
    assert "lite" not in reason
    assert "coding" not in reason


# ── utilization reporting ─────────────────────────────────────────────────


def test_utilization_reports_every_tier(live) -> None:
    live([_dispatch("coding")])
    util = tier_utilization(ParallelismConfig())
    assert set(util) == set(MODEL_TIERS)
    assert util["coding"] == (1, 6)
    assert util["lite"] == (0, 8)


def test_utilization_reflects_the_effective_cap(live) -> None:
    live([])
    util = tier_utilization(ParallelismConfig(max_concurrent_specialists=3))
    # Global cap of 3 clamps lite's per-tier 8.
    assert util["lite"] == (0, 3)
    # Reasoning's own cap of 2 is already tighter.
    assert util["reasoning"] == (0, 2)


def test_utilization_ignores_untiered_dispatches_per_tier(live) -> None:
    live([_dispatch(None)])
    util = tier_utilization(ParallelismConfig())
    assert all(used == 0 for used, _ in util.values())


# ── liveness ──────────────────────────────────────────────────────────────


def test_dead_dispatches_do_not_occupy_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawn that died without being polled still reads `running` on disk.
    Counting it would wedge the fleet until someone ran `dispatch status`."""
    from nightly_core import dispatch as dispatch_mod

    stale = _dispatch("reasoning", pid=999_999)
    monkeypatch.setattr(dispatch_mod, "list_dispatches", lambda _root=None: [stale, stale])
    monkeypatch.setattr(dispatch_mod, "is_alive", lambda _pid: False)
    assert dispatch_mod.active_dispatches() == []
