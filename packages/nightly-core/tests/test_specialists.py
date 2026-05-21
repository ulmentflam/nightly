"""Tests for nightly_core.specialists."""

from __future__ import annotations

import pytest

from nightly_core.contract import SpecialistRole
from nightly_core.specialists import all_roles, specialist_prompt


def test_all_roles_matches_contract_literal() -> None:
    assert all_roles() == ["implementer", "tester", "reviewer", "researcher"]


@pytest.mark.parametrize("role", ["implementer", "tester", "reviewer", "researcher"])
def test_specialist_prompt_returns_non_empty_text(role: SpecialistRole) -> None:
    prompt = specialist_prompt(role)
    assert prompt.strip()
    assert len(prompt) > 100, f"prompt for {role!r} is suspiciously short"


def test_implementer_mentions_refusal_policy_and_file_scope() -> None:
    text = specialist_prompt("implementer")
    assert "refusal policy" in text.lower()
    assert "file scope" in text.lower()
    assert "uncertainty.md" in text


def test_tester_requires_deterministic_tests() -> None:
    text = specialist_prompt("tester")
    assert "deterministic" in text.lower()
    assert "coverage" in text.lower()


def test_reviewer_is_read_only() -> None:
    text = specialist_prompt("reviewer")
    assert "read-only" in text.lower() or "read only" in text.lower()
    assert "LGTM" in text


def test_researcher_is_read_only_and_cites() -> None:
    text = specialist_prompt("researcher")
    assert "do not edit" in text.lower()
    assert "cite" in text.lower()
