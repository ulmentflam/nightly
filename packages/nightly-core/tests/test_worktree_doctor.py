"""Tests for `nightly_core.worktree_doctor`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightly_core import worktree_doctor as wd
from nightly_core.worktree_doctor import (
    probe_worktree_readiness,
    remediate_missing_pre_commit_hook,
    remediate_missing_python_dep,
)


def _make_completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def repo_with_hook(tmp_path: Path) -> Path:
    """A tmp repo with a fake .pre-commit-config.yaml so the probe runs."""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    return tmp_path


def test_probe_ok_when_no_pre_commit_config(tmp_path: Path):
    result = probe_worktree_readiness(tmp_path)
    assert result.ok
    assert result.kind is None


def test_probe_blocked_when_pre_commit_not_on_path(repo_with_hook: Path, monkeypatch):
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns=None))
    result = probe_worktree_readiness(repo_with_hook)
    assert result.blocked
    assert result.kind == "missing_binary"
    assert result.detail == "pre-commit"


def test_probe_ok_when_pre_commit_exits_zero(repo_with_hook: Path, monkeypatch):
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pre-commit"))
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **kw: _make_completed(0))
    assert probe_worktree_readiness(repo_with_hook).ok


def test_probe_classifies_missing_python_dep(repo_with_hook: Path, monkeypatch):
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pre-commit"))
    output = (
        "pyrefly check..............................Failed\n"
        "ModuleNotFoundError: No module named 'sentence_transformers'\n"
    )
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **kw: _make_completed(1, stdout=output))
    result = probe_worktree_readiness(repo_with_hook)
    assert result.remediable
    assert result.kind == "missing_python_dep"
    assert result.detail == "sentence_transformers"


def test_probe_classifies_missing_pre_commit_hook(repo_with_hook: Path, monkeypatch):
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pre-commit"))
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        lambda *a, **kw: _make_completed(
            1,
            stderr="hook ruff is not installed — run `pre-commit install --install-hooks`",
        ),
    )
    result = probe_worktree_readiness(repo_with_hook)
    assert result.remediable
    assert result.kind == "missing_pre_commit_hook"


def test_probe_classifies_missing_binary_as_blocked(repo_with_hook: Path, monkeypatch):
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pre-commit"))
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        lambda *a, **kw: _make_completed(1, stderr="pyrefly: command not found"),
    )
    result = probe_worktree_readiness(repo_with_hook)
    assert result.blocked
    assert result.kind == "missing_binary"


def test_probe_unknown_failure_is_blocked(repo_with_hook: Path, monkeypatch):
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pre-commit"))
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        lambda *a, **kw: _make_completed(1, stdout="something exotic"),
    )
    result = probe_worktree_readiness(repo_with_hook)
    assert result.blocked
    assert result.kind == "unknown"


def test_probe_handles_subprocess_timeout(repo_with_hook: Path, monkeypatch):
    def boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="pre-commit", timeout=120)

    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pre-commit"))
    monkeypatch.setattr(wd.subprocess, "run", boom)
    result = probe_worktree_readiness(repo_with_hook)
    assert result.blocked
    assert result.kind == "unknown"


def test_remediate_missing_python_dep_uses_uv_sync_if_lock_present(tmp_path: Path, monkeypatch):
    (tmp_path / "uv.lock").write_text("# fake", encoding="utf-8")
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/uv"))
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        return _make_completed(0)

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    assert remediate_missing_python_dep(tmp_path) is True
    assert len(calls) >= 1
    assert calls[0][:2] == ["uv", "sync"]


def test_remediate_missing_python_dep_falls_back_to_pip(tmp_path: Path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pip"))
    monkeypatch.setattr(wd.subprocess, "run", lambda argv, **_kw: _make_completed(0))
    assert remediate_missing_python_dep(tmp_path) is True


def test_remediate_missing_python_dep_returns_false_without_installer(tmp_path: Path):
    # No uv.lock, no requirements.txt → nothing to do.
    assert remediate_missing_python_dep(tmp_path) is False


def test_remediate_missing_pre_commit_hook_calls_pre_commit_install(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pre-commit"))
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        return _make_completed(0)

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    assert remediate_missing_pre_commit_hook(tmp_path) is True
    assert calls[0][:3] == ["pre-commit", "install", "--install-hooks"]


def test_remediate_returns_false_when_remediator_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(wd, "shutil", _ShutilStub(which_returns="/usr/bin/pre-commit"))
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **kw: _make_completed(1))
    assert remediate_missing_pre_commit_hook(tmp_path) is False


def test_corpus_forge_signature_is_remediable():
    """Characterization: the exact failure from corpus-forge issue #2."""
    output = (
        "pyrefly check..............................Failed\n"
        "ModuleNotFoundError: No module named 'sentence_transformers'\n"
        "ModuleNotFoundError: No module named 'transformers'\n"
    )
    from nightly_core.worktree_doctor import _classify

    kind, detail = _classify(output)
    assert kind == "missing_python_dep"
    # Captures the first missing module — operator sees it in the proposal
    assert detail == "sentence_transformers"


# ── helpers ───────────────────────────────────────────────────────────────


class _ShutilStub:
    """Stub the `shutil` module attribute on worktree_doctor for tests
    that need to control `which` lookups."""

    def __init__(self, *, which_returns: str | None):
        self._which_returns = which_returns

    def which(self, _name: str) -> str | None:
        return self._which_returns
