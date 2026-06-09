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
    """The Karpathy-inspired strategies must all be present, plus the
    `plan_improvement` universal fallback that ships in v0.0.9+."""
    names = {s.name for s in KEEPALIVE_STRATEGIES}
    assert {
        "reread_planning",
        "mine_uncertainty",
        "revive_parked",
        "merge_near_misses",
        "closed_pr_inspiration",
        "radical_reread",
        "plan_improvement",
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


# ── v0.0.9+ — plan_improvement universal fallback ────────────────────────


def test_plan_improvement_strategy_contains_planning_angles() -> None:
    """The `plan_improvement` strategy is the keep-alive layer's
    expression of the planning-phase doctrine. Its prompt must name
    every canonical planning angle so the agent has explicit
    categories to choose from, plus the headline sentinel."""
    from nightly_core.keepalive import KEEPALIVE_STRATEGIES

    strategy = next(s for s in KEEPALIVE_STRATEGIES if s.name == "plan_improvement")
    assert "GENUINE WORK IS NEVER EXHAUSTED" in strategy.prompt
    for angle in ("usability", "tests", "features", "readability refactor", "documentation"):
        assert angle in strategy.prompt.lower(), f"missing planning angle {angle!r}"


def test_pick_keepalive_returns_plan_improvement_when_only_source_exists(
    tmp_path: Path,
) -> None:
    """No planning/, no past runs, no README — but a Python file is
    present. The `plan_improvement` universal fallback must fire
    because reading source code always produces actionable
    improvements."""
    (tmp_path / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    choice = pick_keepalive(tmp_path)
    assert choice is not None
    assert choice.name == "plan_improvement"


def test_pick_keepalive_still_prefers_radical_reread_over_plan_improvement(
    tmp_path: Path,
) -> None:
    """Strategy precedence: `radical_reread` (entry docs present) still
    wins over `plan_improvement` because re-reading the canonical
    entry-point docs is a more targeted starting point than scanning
    arbitrary source files."""
    (tmp_path / "README.md").write_text("# repo", encoding="utf-8")
    (tmp_path / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    choice = pick_keepalive(tmp_path)
    assert choice is not None
    assert choice.name == "radical_reread"


def test_pick_keepalive_returns_none_only_for_truly_empty_repo(tmp_path: Path) -> None:
    """The function is total over any repo with content; only a
    literally-empty fixture path returns None. This pins the contract
    that the keep-alive ladder always has a last rung."""
    # tmp_path has no files at all — pre-v0.0.9 returned None;
    # post-v0.0.9 still returns None because plan_improvement requires
    # at least one source-like file in the top two levels.
    assert pick_keepalive(tmp_path) is None


def test_render_strategies_includes_plan_improvement() -> None:
    """The markdown render must include the new strategy so operators
    see it in `nightly keepalive` output."""
    output = render_strategies()
    assert "## plan_improvement" in output
    assert "GENUINE WORK IS NEVER EXHAUSTED" in output
