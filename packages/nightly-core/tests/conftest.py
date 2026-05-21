"""Shared fixtures for nightly-core tests.

Most importantly: stub `fetch_via_gh` everywhere by default. The triage
module shells out to the `gh` CLI; we never want that to happen during
tests. Tests that need to test the real `fetch_via_gh` import it directly
(a local binding, so patching the module attribute doesn't affect it).
Tests that need issues injected re-monkeypatch the module attribute with
their own list.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stub_fetch_via_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: triage finds no issues. Tests override per-test as needed."""
    monkeypatch.setattr(
        "nightly_core.triage.fetch_via_gh",
        lambda _root, **_: [],
    )


@pytest.fixture(autouse=True)
def _stub_cascade_proposers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: cascade ideation finds no proposals (no filesystem scan).

    Tests that exercise ideation override this with their own list. Tests
    that call `run_proposers` directly from `nightly_core.ideation` are
    unaffected — they hit the real implementation via local binding.
    """
    monkeypatch.setattr(
        "nightly_core.cascade.run_proposers",
        lambda _root, **_: [],
    )


@pytest.fixture(autouse=True)
def _stub_cascade_pr_rescue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: cascade PR-rescue finds no open Nightly PRs (no `gh` calls).

    Tests that exercise pr_rescue override `_nightly_open_pr_branches`
    and (separately) the feedback fetcher.
    """
    monkeypatch.setattr(
        "nightly_core.cascade._nightly_open_pr_branches",
        lambda _root=None, **_: [],
    )
