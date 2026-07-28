"""Pin README claims against the code that implements them.

Documentation drift is the failure mode these guard: the README's tier
tables and threshold numbers restate values that live in code, and a
restatement with no test is a promise with no enforcement. Each test here
fails when the code moves and the prose doesn't.

Deliberately narrow — this checks *load-bearing factual claims* (tier
defaults, ratios, exit codes), not wording or structure. Prose should be
free to change without a test failing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nightly_core.config import DEFAULT_TIER_EFFORT, ContextConfig
from nightly_core.contract import MODEL_TIERS
from nightly_core.specialists import SPECIALIST_TIER_DEFAULTS

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    if not README.is_file():
        pytest.skip(f"README not found at {README}")
    return README.read_text(encoding="utf-8")


def test_readme_documents_every_tier(readme: str) -> None:
    for tier in MODEL_TIERS:
        assert f"`{tier}`" in readme, f"tier {tier} is undocumented"


def test_readme_role_table_matches_the_code(readme: str) -> None:
    """The reviewer/researcher assignment is the surprising part of the
    design; a README that disagrees with it is worse than no README."""
    for role, tier in SPECIALIST_TIER_DEFAULTS.items():
        # The row reads e.g. "| `reviewer` | **`reasoning`** | ..." — the
        # emphasis varies, so match role and tier on the same line.
        rows = [ln for ln in readme.splitlines() if f"`{role}`" in ln and ln.startswith("|")]
        assert rows, f"no README table row for role {role}"
        assert any(tier in row for row in rows), (
            f"README lists {role} without its actual default tier {tier!r}"
        )


def test_readme_effort_defaults_match_the_code(readme: str) -> None:
    for tier, effort in DEFAULT_TIER_EFFORT.items():
        rows = [ln for ln in readme.splitlines() if f"`{tier}`" in ln and ln.startswith("|")]
        assert any(f"`{effort}`" in row for row in rows), (
            f"README does not show {tier}'s default effort {effort!r}"
        )


def test_readme_handoff_table_matches_the_ratios(readme: str) -> None:
    """The worked examples are arithmetic on the defaults — if the ratios
    change, the table is wrong, not merely stale."""
    cfg = ContextConfig()
    # Compare numerically: the README writes `0.50` for symmetry with
    # `0.25`, which is not `repr(0.5)`. Prose formatting is not the thing
    # under test — the value is.
    documented = {
        label: float(value) for label, value in re.findall(r"(Soft|Hard) \(([\d.]+)\)", readme)
    }
    assert documented.get("Soft") == cfg.handoff_soft_ratio
    assert documented.get("Hard") == cfg.handoff_hard_ratio

    for window, label in ((1_000_000, "1M"), (200_000, "200K")):
        soft = int(window * cfg.handoff_soft_ratio)
        hard = int(window * cfg.handoff_hard_ratio)
        row = next(
            (ln for ln in readme.splitlines() if ln.startswith("|") and f"| {label} |" in ln),
            None,
        )
        assert row is not None, f"no handoff row for a {label} window"
        assert f"{soft // 1000}K" in row
        assert f"{hard // 1000}K" in row


def test_readme_default_context_window_matches_the_code(readme: str) -> None:
    fallback = ContextConfig().default_context_tokens
    assert f"{fallback // 1000}K" in readme


def test_readme_documents_the_at_capacity_exit_code(readme: str) -> None:
    """Exit 3 is the contract `dispatch start` and worktree creation share."""
    assert "exits 3" in readme.lower() or "exit 3" in readme.lower()


def test_readme_capacity_line_matches_the_printed_format(readme: str) -> None:
    """The sample output is a promise about what the operator will see."""
    assert "capacity:" in readme
    sample = next(ln for ln in readme.splitlines() if ln.strip().startswith("capacity:"))
    for tier in MODEL_TIERS:
        assert tier in sample, f"sample capacity line omits {tier}"
    # Tiers must appear in the same order the code emits them.
    positions = [sample.index(tier) for tier in MODEL_TIERS]
    assert positions == sorted(positions)
