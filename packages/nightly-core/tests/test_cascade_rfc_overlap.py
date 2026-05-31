"""Tests for RFC 001 — RFC-overlap PR-awareness in `_find_accepted_rfc`."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import nightly_core.cascade as cascade_mod
from nightly_core.cascade import (
    _find_accepted_rfc,
    _is_item_in_flight,
    pick_accepted_rfc,
)


def _stage_accepted_rfc(root: Path, *, name: str, items: list[str]) -> Path:
    """Drop an accepted RFC under `.planning/rfcs/` with unchecked items."""
    rfcs = root / ".planning" / "rfcs"
    rfcs.mkdir(parents=True)
    body = "## Sized checklist\n\n" + "\n".join(f"- [ ] {item}" for item in items)
    path = rfcs / name
    path.write_text(
        f"---\nstatus: accepted\nsized: true\n---\n# RFC\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _stub_gh(monkeypatch, prs: list[dict]) -> None:
    """Stub `gh pr list` to return `prs` as its JSON payload."""

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(prs))

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(cascade_mod.subprocess, "run", fake_run)


def test_is_item_in_flight_substring_match_in_title():
    pr_texts = [("Add retry budget to auth client", "")]
    assert _is_item_in_flight("003-foo.md", "Add retry budget", pr_texts) is True


def test_is_item_in_flight_filename_match_in_body():
    pr_texts = [("chore: stuff", "Addresses 003-foo items 1-3.")]
    assert _is_item_in_flight("003-foo.md", "unrelated item text", pr_texts) is True


def test_is_item_in_flight_no_match_for_unrelated_pr():
    pr_texts = [("docs: README", "Updates the README header.")]
    assert _is_item_in_flight("003-foo.md", "Implement quux", pr_texts) is False


def test_is_item_in_flight_empty_prs():
    assert _is_item_in_flight("003-foo.md", "anything", []) is False


def test_find_accepted_rfc_picks_first_item_when_no_open_prs(tmp_path, monkeypatch):
    _stage_accepted_rfc(tmp_path, name="001-x.md", items=["item one", "item two"])
    monkeypatch.setattr(shutil, "which", lambda _: None)
    match = _find_accepted_rfc(tmp_path)
    assert match is not None
    assert match.item_text == "item one"


def test_find_accepted_rfc_skips_item_matched_by_open_pr_title(tmp_path, monkeypatch):
    _stage_accepted_rfc(tmp_path, name="001-x.md", items=["item alpha", "item beta"])
    _stub_gh(
        monkeypatch,
        [{"title": "feat: item alpha shipped", "body": "", "headRefName": "nightly/foo"}],
    )
    match = _find_accepted_rfc(tmp_path)
    assert match is not None
    # The first match should now be `item beta`, since alpha is in-flight.
    assert match.item_text == "item beta"


def test_find_accepted_rfc_skips_via_filename_match_in_pr_body(tmp_path, monkeypatch):
    _stage_accepted_rfc(tmp_path, name="002-y.md", items=["item one"])
    _stub_gh(
        monkeypatch,
        [{"title": "chore: ...", "body": "Addresses 002-y items 1-3.", "headRefName": "nightly/x"}],
    )
    # The only item in the only RFC overlaps with an open PR → None.
    assert _find_accepted_rfc(tmp_path) is None


def test_find_accepted_rfc_ignores_non_nightly_prs(tmp_path, monkeypatch):
    _stage_accepted_rfc(tmp_path, name="003-z.md", items=["item one"])
    # The PR has a matching title but isn't on a `nightly/` branch — ignored.
    _stub_gh(
        monkeypatch,
        [{"title": "item one", "body": "", "headRefName": "feature/x"}],
    )
    match = _find_accepted_rfc(tmp_path)
    assert match is not None
    assert match.item_text == "item one"


def test_pick_accepted_rfc_delegates_to_find(tmp_path, monkeypatch):
    _stage_accepted_rfc(tmp_path, name="004-w.md", items=["only item"])
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert pick_accepted_rfc(tmp_path) is not None
