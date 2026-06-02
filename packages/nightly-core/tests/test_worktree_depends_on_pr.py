"""Tests for RFC 004 §A — `depends_on_pr` frontmatter + `_resolve_base_branch`.

Covers:
- `PlanRecord.depends_on_pr` parsing (int, `#`-prefixed, malformed, absent)
- `_resolve_base_branch` fallback behavior (no decl / open PR / merged /
  closed / no-gh / gh-failure / unparseable JSON)
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import nightly_core.worktree as worktree_mod
from nightly_core.plans import PlanRecord
from nightly_core.worktree import _resolve_base_branch


def _plan(metadata: dict[str, str]) -> PlanRecord:
    """Construct a `PlanRecord` with the given frontmatter (no file I/O)."""
    return PlanRecord(path=Path("/nonexistent/plan.md"), metadata=metadata, body="")


# ── PlanRecord.depends_on_pr ──────────────────────────────────────────────


def test_depends_on_pr_absent_returns_none() -> None:
    assert _plan({}).depends_on_pr is None


def test_depends_on_pr_empty_string_returns_none() -> None:
    assert _plan({"depends_on_pr": ""}).depends_on_pr is None
    assert _plan({"depends_on_pr": "   "}).depends_on_pr is None


def test_depends_on_pr_bare_integer() -> None:
    assert _plan({"depends_on_pr": "54"}).depends_on_pr == 54


def test_depends_on_pr_hash_prefixed() -> None:
    assert _plan({"depends_on_pr": "#54"}).depends_on_pr == 54


def test_depends_on_pr_hash_with_whitespace() -> None:
    assert _plan({"depends_on_pr": "  # 54  "}).depends_on_pr == 54


def test_depends_on_pr_malformed_returns_none() -> None:
    assert _plan({"depends_on_pr": "abc"}).depends_on_pr is None
    assert _plan({"depends_on_pr": "12.3"}).depends_on_pr is None


def test_depends_on_pr_non_positive_returns_none() -> None:
    assert _plan({"depends_on_pr": "0"}).depends_on_pr is None
    assert _plan({"depends_on_pr": "-5"}).depends_on_pr is None


# ── _resolve_base_branch ──────────────────────────────────────────────────


def _stub_gh(monkeypatch, payload: dict, *, returncode: int = 0) -> list[list[str]]:
    """Stub `gh pr view` to return `payload` as JSON. Captures invocations."""
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(worktree_mod.subprocess, "run", fake_run)
    return calls


def test_resolve_base_no_declaration_returns_default(monkeypatch) -> None:
    # gh shouldn't even be consulted.
    def _unexpected(*_args, **_kwargs) -> None:
        msg = "gh should not be invoked when depends_on_pr is None"
        raise AssertionError(msg)

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(worktree_mod.subprocess, "run", _unexpected)
    assert _resolve_base_branch(depends_on_pr=None, default_base="main") == "main"


def test_resolve_base_open_pr_returns_head_ref(monkeypatch) -> None:
    calls = _stub_gh(
        monkeypatch,
        {"state": "OPEN", "headRefName": "nightly/unblock-20260523"},
    )
    base = _resolve_base_branch(depends_on_pr=54, default_base="main")
    assert base == "nightly/unblock-20260523"
    assert calls
    assert calls[0][:4] == ["gh", "pr", "view", "54"]


def test_resolve_base_merged_pr_falls_back_to_default(monkeypatch, caplog) -> None:
    _stub_gh(
        monkeypatch,
        {"state": "MERGED", "headRefName": "nightly/already-merged"},
    )
    with caplog.at_level("WARNING", logger="nightly_core.worktree"):
        base = _resolve_base_branch(depends_on_pr=54, default_base="main")
    assert base == "main"
    assert any("MERGED" in rec.message for rec in caplog.records)


def test_resolve_base_closed_pr_falls_back_to_default(monkeypatch, caplog) -> None:
    _stub_gh(
        monkeypatch,
        {"state": "CLOSED", "headRefName": "nightly/abandoned"},
    )
    with caplog.at_level("WARNING", logger="nightly_core.worktree"):
        base = _resolve_base_branch(depends_on_pr=54, default_base="main")
    assert base == "main"
    assert any("CLOSED" in rec.message for rec in caplog.records)


def test_resolve_base_no_gh_falls_back_to_default(monkeypatch, caplog) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with caplog.at_level("WARNING", logger="nightly_core.worktree"):
        base = _resolve_base_branch(depends_on_pr=54, default_base="develop")
    assert base == "develop"
    assert any("`gh` is unavailable" in rec.message for rec in caplog.records)


def test_resolve_base_gh_nonzero_exit_falls_back(monkeypatch, caplog) -> None:
    _stub_gh(monkeypatch, {}, returncode=1)
    with caplog.at_level("WARNING", logger="nightly_core.worktree"):
        base = _resolve_base_branch(depends_on_pr=999, default_base="main")
    assert base == "main"
    assert any("exited 1" in rec.message for rec in caplog.records)


def test_resolve_base_gh_unparseable_json_falls_back(monkeypatch, caplog) -> None:
    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="not-json{", stderr="")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(worktree_mod.subprocess, "run", fake_run)
    with caplog.at_level("WARNING", logger="nightly_core.worktree"):
        base = _resolve_base_branch(depends_on_pr=54, default_base="main")
    assert base == "main"
    assert any("unparseable JSON" in rec.message for rec in caplog.records)


def test_resolve_base_subprocess_error_falls_back(monkeypatch, caplog) -> None:
    def raising_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(worktree_mod.subprocess, "run", raising_run)
    with caplog.at_level("WARNING", logger="nightly_core.worktree"):
        base = _resolve_base_branch(depends_on_pr=54, default_base="main")
    assert base == "main"
    assert any("falling back" in rec.message for rec in caplog.records)


def test_resolve_base_respects_non_main_default(monkeypatch) -> None:
    """A custom configured base (e.g. `develop`) is honored when no PR declared."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert _resolve_base_branch(depends_on_pr=None, default_base="develop") == "develop"
