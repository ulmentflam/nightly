"""Tests for RFC 002 cascade integration in `nightly_core.cascade`.

These tests stub `probe_worktree_readiness` so we can characterize the
cascade's behavior without spawning real `pre-commit` runs.
"""

from __future__ import annotations

from pathlib import Path

import nightly_core.cascade as cascade_mod
from nightly_core.cascade import (
    CASCADE_SOURCES,
    pick_worktree_blocked,
)
from nightly_core.worktree_doctor import WorktreeReadiness


def test_worktree_blocked_is_a_known_cascade_source():
    assert "worktree_blocked" in CASCADE_SOURCES
    # Slotted right after `concluded`
    assert CASCADE_SOURCES.index("worktree_blocked") == 1


def test_pick_worktree_blocked_returns_none_when_probe_disabled(tmp_path: Path, monkeypatch):
    nightly_dir = tmp_path / ".nightly"
    nightly_dir.mkdir()
    (nightly_dir / "config.yml").write_text("worktree:\n  probe_enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(
        cascade_mod, "probe_worktree_readiness", lambda _r: WorktreeReadiness(state="blocked")
    )
    assert pick_worktree_blocked(tmp_path) is None


def test_pick_worktree_blocked_returns_none_when_ready(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        cascade_mod, "probe_worktree_readiness", lambda _r: WorktreeReadiness(state="ok")
    )
    assert pick_worktree_blocked(tmp_path) is None


def test_pick_worktree_blocked_returns_choice_when_blocked(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        cascade_mod,
        "probe_worktree_readiness",
        lambda _r: WorktreeReadiness(state="blocked", kind="missing_binary", detail="pyrefly"),
    )
    choice = pick_worktree_blocked(tmp_path)
    assert choice is not None
    assert choice.source == "worktree_blocked"
    assert "missing_binary" in choice.summary


def test_pick_worktree_blocked_defers_when_remediation_enabled(tmp_path: Path, monkeypatch):
    """A remediable failure with `remediate_enabled: true` should NOT block
    the cascade — the driver will run the remediator and re-probe."""
    monkeypatch.setattr(
        cascade_mod,
        "probe_worktree_readiness",
        lambda _r: WorktreeReadiness(state="remediable", kind="missing_python_dep", detail="x"),
    )
    assert pick_worktree_blocked(tmp_path) is None


def test_pick_worktree_blocked_surfaces_when_remediation_disabled(tmp_path: Path, monkeypatch):
    nightly_dir = tmp_path / ".nightly"
    nightly_dir.mkdir()
    (nightly_dir / "config.yml").write_text(
        "worktree:\n  remediate_enabled: false\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        cascade_mod,
        "probe_worktree_readiness",
        lambda _r: WorktreeReadiness(state="remediable", kind="missing_python_dep", detail="x"),
    )
    choice = pick_worktree_blocked(tmp_path)
    assert choice is not None
    assert "remediable" in choice.summary


def test_ready_marker_short_circuits_the_probe(tmp_path: Path, monkeypatch):
    """A fresh READY marker should skip the subprocess call entirely."""
    marker_dir = tmp_path / ".nightly" / "worktrees" / "main"
    marker_dir.mkdir(parents=True)
    (marker_dir / "READY").touch()

    def boom(_r):
        raise AssertionError("probe should not be called when READY marker is fresh")

    monkeypatch.setattr(cascade_mod, "probe_worktree_readiness", boom)
    # Stub the branch slug so it matches the marker path
    monkeypatch.setattr(cascade_mod, "_branch_slug_for", lambda _r: "main")
    assert pick_worktree_blocked(tmp_path) is None


def test_worktree_remediation_is_not_auto_pr_eligible():
    """Locks the autonomy-bar carveout (RFC 002 Resolved decision #4)."""
    from nightly_core.autonomy import AUTO_PR_CATEGORIES

    assert "worktree_remediation" not in AUTO_PR_CATEGORIES
