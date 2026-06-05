"""Tests for RFC 009 Phase B — cascade ordering by strategic category."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightly_core.cascade import _ordered_proposals, pick_ideated_fallback
from nightly_core.proposers.base import Proposal


def _proposal(
    *,
    proposer: str = "synthesis",
    title: str,
    score: float,
    strategic_category: str = "housekeeping",
) -> Proposal:
    """Minimal Proposal for ordering tests."""
    return Proposal(
        proposer=proposer,
        category="synthesis" if proposer == "synthesis" else "lint_debt",
        title=title,
        body=title,
        score=score,
        strategic_category=strategic_category,  # type: ignore[arg-type]
    )


def _write_config(root: Path, *, category_ordering: bool) -> None:
    """Stage `.nightly/config.yml` with the ideate config block."""
    (root / ".nightly").mkdir(parents=True, exist_ok=True)
    (root / ".nightly" / "config.yml").write_text(
        f"ideate:\n  category_ordering: {str(category_ordering).lower()}\n",
        encoding="utf-8",
    )


# ── _ordered_proposals — RFC 009 §4 sort ─────────────────────────────────


def test_ordered_proposals_puts_cleaning_above_capability_even_at_lower_score(
    tmp_path: Path,
) -> None:
    """Operator-stated priority: cleaning at score 1.2 outranks capability
    at score 1.8. The category index dominates the score in the sort."""
    proposals = [
        _proposal(title="add new verb", score=1.8, strategic_category="capability"),
        _proposal(title="drop dead code", score=1.2, strategic_category="cleaning"),
    ]
    ordered = _ordered_proposals(proposals, tmp_path)
    assert ordered[0].title == "drop dead code"
    assert ordered[1].title == "add new verb"


def test_ordered_proposals_breaks_category_ties_by_score(tmp_path: Path) -> None:
    """Within a single category, the higher-score proposal wins."""
    proposals = [
        _proposal(title="cleaning A", score=1.1, strategic_category="cleaning"),
        _proposal(title="cleaning B", score=2.5, strategic_category="cleaning"),
        _proposal(title="cleaning C", score=1.8, strategic_category="cleaning"),
    ]
    ordered = _ordered_proposals(proposals, tmp_path)
    assert [p.title for p in ordered] == ["cleaning B", "cleaning C", "cleaning A"]


def test_ordered_proposals_traverses_all_five_categories_in_priority(
    tmp_path: Path,
) -> None:
    """Mixed-category set ends up ordered cleaning → refactoring →
    housekeeping → convenience → capability regardless of input shuffle."""
    proposals = [
        _proposal(title="convenience X", score=2.0, strategic_category="convenience"),
        _proposal(title="capability X", score=2.0, strategic_category="capability"),
        _proposal(title="housekeeping X", score=2.0, strategic_category="housekeeping"),
        _proposal(title="cleaning X", score=2.0, strategic_category="cleaning"),
        _proposal(title="refactoring X", score=2.0, strategic_category="refactoring"),
    ]
    ordered = _ordered_proposals(proposals, tmp_path)
    assert [p.strategic_category for p in ordered] == [
        "cleaning",
        "refactoring",
        "housekeeping",
        "convenience",
        "capability",
    ]


def test_ordered_proposals_opt_out_falls_back_to_score_only(tmp_path: Path) -> None:
    """Config `ideate.category_ordering: false` reverts to score-only
    ordering — capability at 1.8 wins over cleaning at 1.2."""
    _write_config(tmp_path, category_ordering=False)
    proposals = [
        _proposal(title="cleaning low", score=1.2, strategic_category="cleaning"),
        _proposal(title="capability high", score=1.8, strategic_category="capability"),
    ]
    ordered = _ordered_proposals(proposals, tmp_path)
    assert ordered[0].title == "capability high"


def test_ordered_proposals_unknown_category_sorts_last(tmp_path: Path) -> None:
    """An unknown strategic_category (from a future RFC or a misconfigured
    proposer) sorts after all known categories — graceful degrade rather
    than crash."""
    proposals = [
        Proposal(
            proposer="x",
            category="lint_debt",
            title="unknown bucket",
            body="b",
            score=99.0,
            strategic_category="future_category",  # type: ignore[arg-type]
        ),
        _proposal(title="cleaning", score=0.5, strategic_category="cleaning"),
    ]
    ordered = _ordered_proposals(proposals, tmp_path)
    assert ordered[0].title == "cleaning"
    assert ordered[1].title == "unknown bucket"


# ── pick_ideated_fallback uses the new ordering ──────────────────────────


def test_pick_ideated_fallback_returns_highest_priority_not_highest_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a session armed with two proposals — one cleaning at
    low score, one capability at high score — returns the cleaning one
    via the fallback path."""
    proposals = [
        _proposal(title="capability high", score=3.0, strategic_category="capability"),
        _proposal(title="cleaning low", score=1.0, strategic_category="cleaning"),
    ]
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: proposals,
    )
    pick = pick_ideated_fallback(tmp_path)
    assert pick is not None
    assert pick.title == "cleaning low"


# ── load_ideate_config ───────────────────────────────────────────────────


def test_load_ideate_config_defaults_when_file_missing(tmp_path: Path) -> None:
    from nightly_core.config import load_ideate_config

    cfg = load_ideate_config(tmp_path)
    assert cfg.category_ordering is True
    assert cfg.synthesis.enabled is True
    assert cfg.synthesis.timeout_seconds == 120
    assert cfg.synthesis.max_proposals == 25


def test_load_ideate_config_respects_category_ordering_override(tmp_path: Path) -> None:
    from nightly_core.config import load_ideate_config

    _write_config(tmp_path, category_ordering=False)
    cfg = load_ideate_config(tmp_path)
    assert cfg.category_ordering is False


def test_load_ideate_config_reads_synthesis_subblock(tmp_path: Path) -> None:
    from nightly_core.config import load_ideate_config

    (tmp_path / ".nightly").mkdir()
    (tmp_path / ".nightly" / "config.yml").write_text(
        "ideate:\n"
        "  category_ordering: true\n"
        "  synthesis:\n"
        "    enabled: false\n"
        "    timeout_seconds: 60\n"
        "    max_proposals: 10\n",
        encoding="utf-8",
    )
    cfg = load_ideate_config(tmp_path)
    assert cfg.synthesis.enabled is False
    assert cfg.synthesis.timeout_seconds == 60
    assert cfg.synthesis.max_proposals == 10


def test_load_ideate_config_handles_malformed_yaml(tmp_path: Path) -> None:
    from nightly_core.config import load_ideate_config

    (tmp_path / ".nightly").mkdir()
    (tmp_path / ".nightly" / "config.yml").write_text("ideate: {{{ malformed\n", encoding="utf-8")
    cfg = load_ideate_config(tmp_path)
    assert cfg.category_ordering is True  # defaults


# ── briefing grouping (RFC 009 §B4) ──────────────────────────────────────


def test_group_issues_by_strategic_category_orders_buckets() -> None:
    from nightly_core.briefing import _group_issues_by_strategic_category

    issues = [
        {
            "id": "001-cap",
            "proposer": "synthesis",
            "strategic_category": "capability",
            "title": "t",
        },
        {
            "id": "002-clean",
            "proposer": "synthesis",
            "strategic_category": "cleaning",
            "title": "t",
        },
        {
            "id": "003-nit",
            "proposer": "lint_debt",
            "strategic_category": "housekeeping",
            "title": "t",
        },
        {
            "id": "004-conv",
            "proposer": "synthesis",
            "strategic_category": "convenience",
            "title": "t",
        },
    ]
    groups = _group_issues_by_strategic_category(issues)
    # Static-analysis hits (proposer != synthesis) always end up in
    # the "static_analysis" bucket regardless of their strategic_category.
    labels = [g["label"] for g in groups]
    assert labels == ["Cleaning", "Convenience", "Capability", "Static-analysis hits"]


def test_group_issues_omits_empty_categories() -> None:
    """If a category has no issues, its sub-section doesn't render."""
    from nightly_core.briefing import _group_issues_by_strategic_category

    issues = [
        {"id": "001", "proposer": "synthesis", "strategic_category": "cleaning", "title": "t"},
    ]
    groups = _group_issues_by_strategic_category(issues)
    assert [g["strategic_category"] for g in groups] == ["cleaning"]


def test_group_issues_routes_phase5_proposers_to_static_analysis() -> None:
    """The three Phase-5 proposers (todo_fixme, lint_debt, type_holes)
    always go to the static_analysis bucket — they're nits, not
    strategy."""
    from nightly_core.briefing import _group_issues_by_strategic_category

    issues = [
        {"id": "001", "proposer": "lint_debt", "strategic_category": "housekeeping", "title": "t"},
        {"id": "002", "proposer": "todo_fixme", "strategic_category": "housekeeping", "title": "t"},
        {"id": "003", "proposer": "type_holes", "strategic_category": "housekeeping", "title": "t"},
        {"id": "004", "proposer": "synthesis", "strategic_category": "housekeeping", "title": "t"},
    ]
    groups = _group_issues_by_strategic_category(issues)
    # Synthesis goes to housekeeping; the other three go to static_analysis.
    by_label = {g["label"]: g for g in groups}
    assert len(by_label["Housekeeping"]["issues"]) == 1
    assert by_label["Housekeeping"]["issues"][0]["proposer"] == "synthesis"
    assert len(by_label["Static-analysis hits"]["issues"]) == 3
