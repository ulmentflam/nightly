"""Tests for RFC 001 Phase B — stacked-geometry detection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import nightly_core.cascade as cascade_mod
from nightly_core.cascade import detect_stacked_geometry


def _stub_git_branch(monkeypatch, branch: str) -> None:
    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=branch + "\n")

    monkeypatch.setattr(cascade_mod.subprocess, "run", fake_run)


def test_detect_stacked_geometry_returns_empty_on_main(tmp_path: Path, monkeypatch):
    _stub_git_branch(monkeypatch, "main")
    monkeypatch.setattr(cascade_mod, "_nightly_open_pr_branches", lambda *a, **kw: [])
    geo = detect_stacked_geometry(tmp_path)
    assert geo.current_branch == "main"
    assert not geo.is_stacked
    assert geo.chain == ()


def test_detect_stacked_geometry_returns_empty_on_feature_branch(tmp_path: Path, monkeypatch):
    """Non-nightly/ branches are inert by design — operator's own work."""
    _stub_git_branch(monkeypatch, "feature/x")
    monkeypatch.setattr(cascade_mod, "_nightly_open_pr_branches", lambda *a, **kw: [])
    geo = detect_stacked_geometry(tmp_path)
    assert not geo.is_stacked


def test_detect_stacked_geometry_finds_one_level_stack(tmp_path: Path, monkeypatch):
    _stub_git_branch(monkeypatch, "nightly/in-flight")
    monkeypatch.setattr(
        cascade_mod,
        "_nightly_open_pr_branches",
        lambda *a, **kw: [("nightly/in-flight", 57, "https://example/57")],
    )
    # No plan exists → declared=False (accidental geometry).
    monkeypatch.setattr(cascade_mod, "_match_plan_to_branch", lambda *a, **kw: None)
    geo = detect_stacked_geometry(tmp_path)
    assert geo.is_stacked
    assert geo.chain == ((57, "nightly/in-flight", "https://example/57", False),)
    assert not geo.all_declared


def test_characterization_2026_05_24_stacked_paperwork(tmp_path: Path, monkeypatch):
    """Characterization: the 2026-05-24 incident where 5 Nightly PRs stacked
    because each merged unblock seeded the next worktree. The cascade still
    picks work (report-and-allow per RFC 001 resolved decision #1) but the
    geometry function reports HEAD's overlap with the in-flight PR. None of
    those plans declared `depends_on_pr`, so declared=False across the
    board — the panel renders rose, as a failure mode."""
    _stub_git_branch(monkeypatch, "nightly/phase-k-reconcile")
    monkeypatch.setattr(
        cascade_mod,
        "_nightly_open_pr_branches",
        lambda *a, **kw: [
            ("nightly/unblock-20260523", 54, "https://example/54"),
            ("nightly/phase-e-reconcile", 55, "https://example/55"),
            ("nightly/phase-j-reconcile", 56, "https://example/56"),
            ("nightly/phase-k-reconcile", 57, "https://example/57"),  # HEAD
            ("nightly/plan-reconcile", 58, "https://example/58"),
        ],
    )
    monkeypatch.setattr(cascade_mod, "_match_plan_to_branch", lambda *a, **kw: None)
    geo = detect_stacked_geometry(tmp_path)
    # v1 only reports the immediate ancestor (HEAD's own PR). Full chain
    # traversal is deferred to the worktree-policy follow-up RFC.
    assert geo.is_stacked
    assert geo.chain == ((57, "nightly/phase-k-reconcile", "https://example/57", False),)
    assert not geo.all_declared


def test_detect_stacked_geometry_returns_empty_when_git_fails(tmp_path: Path, monkeypatch):
    def boom(*_a, **_kw):
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(cascade_mod.subprocess, "run", boom)
    geo = detect_stacked_geometry(tmp_path)
    assert geo.current_branch == ""
    assert not geo.is_stacked


# ── RFC 004 §C — declared vs accidental chain entries ─────────────────────


def _fake_plan(metadata: dict[str, str]):
    """A minimal stand-in for `_match_plan_to_branch`'s return value."""
    from nightly_core.plans import PlanRecord

    return PlanRecord(path=Path("/nonexistent/plan.md"), metadata=metadata, body="")


def test_detect_stacked_geometry_marks_declared_when_plan_depends_on_pr(
    tmp_path: Path, monkeypatch
):
    """A plan that declares `depends_on_pr: 57` on a branch coinciding with
    PR #57 → chain entry declared=True → `all_declared` is True."""
    _stub_git_branch(monkeypatch, "nightly/in-flight")
    monkeypatch.setattr(
        cascade_mod,
        "_nightly_open_pr_branches",
        lambda *a, **kw: [("nightly/in-flight", 57, "https://example/57")],
    )
    monkeypatch.setattr(
        cascade_mod,
        "_match_plan_to_branch",
        lambda *a, **kw: _fake_plan({"depends_on_pr": "57"}),
    )
    geo = detect_stacked_geometry(tmp_path)
    assert geo.chain == ((57, "nightly/in-flight", "https://example/57", True),)
    assert geo.all_declared


def test_detect_stacked_geometry_accidental_when_plan_has_no_declaration(
    tmp_path: Path, monkeypatch
):
    """A plan exists but does NOT carry `depends_on_pr` → declared=False."""
    _stub_git_branch(monkeypatch, "nightly/in-flight")
    monkeypatch.setattr(
        cascade_mod,
        "_nightly_open_pr_branches",
        lambda *a, **kw: [("nightly/in-flight", 57, "https://example/57")],
    )
    monkeypatch.setattr(
        cascade_mod,
        "_match_plan_to_branch",
        lambda *a, **kw: _fake_plan({"status": "in_progress"}),
    )
    geo = detect_stacked_geometry(tmp_path)
    assert geo.chain == ((57, "nightly/in-flight", "https://example/57", False),)
    assert not geo.all_declared


def test_all_declared_is_false_for_empty_chain():
    """Empty chain → no geometry to classify → `all_declared` is False."""
    from nightly_core.cascade import StackedGeometry

    geo = StackedGeometry(current_branch="main", chain=())
    assert not geo.all_declared
