"""Tests for nightly_core.keepalive — think-harder strategies."""

from __future__ import annotations

from pathlib import Path

from nightly_core.keepalive import (
    KEEPALIVE_STRATEGIES,
    KeepaliveStrategy,
    pick_keepalive,
    render_strategies,
)


def test_strategies_have_unique_names() -> None:
    names = [s.name for s in KEEPALIVE_STRATEGIES]
    assert len(names) == len(set(names))


def test_strategies_include_canonical_set() -> None:
    """The Karpathy-inspired strategies must all be present."""
    names = {s.name for s in KEEPALIVE_STRATEGIES}
    assert {
        "reread_planning",
        "mine_uncertainty",
        "revive_parked",
        "merge_near_misses",
        "closed_pr_inspiration",
        "radical_reread",
    }.issubset(names)


def test_strategy_prompts_are_non_empty() -> None:
    for strategy in KEEPALIVE_STRATEGIES:
        assert strategy.prompt.strip()
        assert strategy.applies_when.strip()
        assert isinstance(strategy, KeepaliveStrategy)


def test_render_strategies_is_markdown() -> None:
    output = render_strategies()
    assert output.startswith("# Keep-alive strategies")
    for strategy in KEEPALIVE_STRATEGIES:
        assert f"## {strategy.name}" in output
        assert strategy.prompt in output
    # Karpathy attribution must survive
    assert "autoresearch" in output


def test_pick_keepalive_empty_repo_falls_through(tmp_path: Path) -> None:
    """Repo with no planning, no plans, no entry docs → None."""
    assert pick_keepalive(tmp_path) is None


def test_pick_keepalive_prefers_planning_when_present(tmp_path: Path) -> None:
    (tmp_path / ".planning").mkdir()
    (tmp_path / ".planning" / "rfcs").mkdir()
    (tmp_path / ".planning" / "rfcs" / "0001-test.md").write_text("# rfc", encoding="utf-8")
    choice = pick_keepalive(tmp_path)
    assert choice is not None
    assert choice.name == "reread_planning"


def test_pick_keepalive_falls_back_to_radical_reread(tmp_path: Path) -> None:
    """No planning, no plans → radical_reread (assuming README/AGENTS exist)."""
    (tmp_path / "README.md").write_text("# repo", encoding="utf-8")
    choice = pick_keepalive(tmp_path)
    assert choice is not None
    assert choice.name == "radical_reread"


def test_pick_keepalive_prefers_uncertainty_over_radical(tmp_path: Path) -> None:
    """If uncertainty.md exists under a plan, prefer mining it over a fresh re-read."""
    (tmp_path / "README.md").write_text("# repo", encoding="utf-8")
    task_dir = tmp_path / ".nightly" / "runs" / "2026-05-21" / "tasks" / "0001-foo"
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text(
        "---\nstatus: done\nslug: 0001-foo\n---\n# foo\n", encoding="utf-8"
    )
    (task_dir / "uncertainty.md").write_text("I wasn't sure...", encoding="utf-8")
    choice = pick_keepalive(tmp_path)
    assert choice is not None
    assert choice.name == "mine_uncertainty"


def test_pick_keepalive_revives_parked_when_no_uncertainty(tmp_path: Path) -> None:
    """A parked plan beats radical_reread when nothing else applies."""
    (tmp_path / "README.md").write_text("# repo", encoding="utf-8")
    task_dir = tmp_path / ".nightly" / "runs" / "2026-05-21" / "tasks" / "0001-foo"
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text(
        "---\nstatus: parked\nslug: 0001-foo\n---\n# foo\n", encoding="utf-8"
    )
    choice = pick_keepalive(tmp_path)
    assert choice is not None
    assert choice.name == "revive_parked"
