"""RFC 001 §A3 — `_RFCMatch.skipped_count` and rationale wiring."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import nightly_core.cascade as cascade_mod
from nightly_core.cascade import _find_accepted_rfc, next_task


def _stage_accepted_rfc(root: Path, *, name: str, items: list[str]) -> Path:
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
    def fake_run(*_a, **_kw):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(prs))

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(cascade_mod.subprocess, "run", fake_run)


def test_skipped_count_zero_when_no_overlap(tmp_path: Path, monkeypatch):
    _stage_accepted_rfc(tmp_path, name="001-x.md", items=["alpha", "beta"])
    monkeypatch.setattr(shutil, "which", lambda _: None)
    match = _find_accepted_rfc(tmp_path)
    assert match is not None
    assert match.skipped_count == 0


def test_skipped_count_reflects_pr_overlaps(tmp_path: Path, monkeypatch):
    _stage_accepted_rfc(tmp_path, name="001-x.md", items=["alpha", "beta", "gamma"])
    _stub_gh(
        monkeypatch,
        [
            {"title": "feat: alpha", "body": "", "headRefName": "nightly/a"},
            {"title": "feat: beta", "body": "", "headRefName": "nightly/b"},
        ],
    )
    match = _find_accepted_rfc(tmp_path)
    assert match is not None
    assert match.item_text == "gamma"
    assert match.skipped_count == 2


def test_next_task_rationale_mentions_skip_count(tmp_path: Path, monkeypatch):
    _stage_accepted_rfc(tmp_path, name="001-x.md", items=["alpha", "beta"])
    _stub_gh(
        monkeypatch,
        [{"title": "feat: alpha", "body": "", "headRefName": "nightly/a"}],
    )
    # Stub out the earlier cascade steps that would otherwise hit disk/gh
    monkeypatch.setattr(cascade_mod, "_conclude_requested", lambda _r: False)
    monkeypatch.setattr(cascade_mod, "pick_worktree_blocked", lambda _r: None)
    monkeypatch.setattr(cascade_mod, "pick_in_flight", lambda _r: None)
    monkeypatch.setattr(cascade_mod, "pick_unblocked", lambda _r: None)
    monkeypatch.setattr(cascade_mod, "repo_root", lambda: tmp_path)

    choice = next_task(tmp_path)
    assert choice.source == "accepted_rfc"
    assert "Skipped 1 earlier item" in (choice.rationale or "")
    assert "RFC 001 §A2" in (choice.rationale or "")
