"""Tests for nightly_core.update — self-update & per-repo refresh."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightly_core.update import (
    InstallMethod,
    detect_install_method,
    detect_install_root,
    git_head_commit,
    refresh_repo_install,
    update_install,
)


def test_detect_install_root_finds_git_dir() -> None:
    """The running nightly_core lives under the Nightly repo, which has .git."""
    root = detect_install_root()
    assert root is not None
    assert (root / ".git").is_dir()


def test_detect_install_method_falls_back_to_unknown_when_no_git_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `detect_install_root` returns None, `detect_install_method` is `unknown`."""
    monkeypatch.setattr("nightly_core.update.detect_install_root", lambda: None)
    method = detect_install_method()
    assert method.kind == "unknown"
    assert method.root is None
    assert not method.is_git


def test_detect_install_method_returns_git_when_repo_present() -> None:
    method = detect_install_method()
    assert method.is_git
    assert method.root is not None


def test_install_method_is_git_helper() -> None:
    assert InstallMethod(kind="git", root=Path("/tmp")).is_git
    assert not InstallMethod(kind="git", root=None).is_git
    assert not InstallMethod(kind="unknown", root=None).is_git


def test_git_head_commit_returns_short_sha() -> None:
    root = detect_install_root()
    assert root is not None
    sha = git_head_commit(root)
    assert sha != ""
    # Short SHA is 7-12 chars typically
    assert 4 <= len(sha) <= 40


def test_git_head_commit_handles_non_git(tmp_path: Path) -> None:
    assert git_head_commit(tmp_path) == ""


def test_git_head_commit_handles_none() -> None:
    assert git_head_commit(None) == ""


def test_update_install_raises_when_not_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If we can't find a git root, update should raise with a helpful message."""
    monkeypatch.setattr(
        "nightly_core.update.detect_install_method",
        lambda: InstallMethod(kind="unknown", root=None),
    )
    with pytest.raises(RuntimeError) as exc:
        update_install(version="main")
    assert "PyPI" in str(exc.value) or "install.sh" in str(exc.value)


def test_update_install_dry_run_skips_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run should fetch but not check out or sync."""
    method = InstallMethod(kind="git", root=tmp_path)
    monkeypatch.setattr("nightly_core.update.detect_install_method", lambda: method)
    monkeypatch.setattr("nightly_core.update.git_head_commit", lambda _root: "abc1234")

    calls: list[list[str]] = []

    def fake_git(args: list[str], *, cwd: Path) -> None:
        calls.append(args)

    monkeypatch.setattr("nightly_core.update._git", fake_git)
    monkeypatch.setattr(
        "nightly_core.update._uv_sync",
        lambda _cwd: pytest.fail("uv_sync should not run in dry-run"),
    )
    result = update_install(version="main", dry_run=True)
    assert result == (method, "abc1234", "abc1234")
    # Only fetch should have run; no checkout / no pull
    assert calls == [["fetch", "--quiet", "--tags", "origin"]]


def test_refresh_repo_install_refreshes_only_installed_hosts(
    tmp_path: Path,
) -> None:
    """Hosts not installed in the repo should not get re-installed."""
    calls: list[str] = []

    class _FakeIntegration:
        def __init__(self, name: str, installed: bool) -> None:
            self.name = name
            self._installed = installed

        def is_installed(self, scope: str) -> bool:
            return self._installed

        async def install(self, scope: str) -> None:
            calls.append(self.name)

    loaders = {
        "claude": lambda _root: _FakeIntegration("claude", installed=True),
        "codex": lambda _root: _FakeIntegration("codex", installed=False),
        "cursor": lambda _root: _FakeIntegration("cursor", installed=True),
    }
    refreshed, rules_action = refresh_repo_install(tmp_path, host_loader=loaders)
    assert sorted(refreshed) == ["claude", "cursor"]
    assert "codex" not in refreshed
    assert rules_action == "skipped"  # no AGENTS.md / CLAUDE.md present


def test_refresh_repo_install_updates_rules_when_present(tmp_path: Path) -> None:
    """If AGENTS.md exists, the rules block should get refreshed."""
    (tmp_path / "AGENTS.md").write_text(
        "<!-- nightly:rules:start -->\n# old\n<!-- nightly:rules:end -->\n",
        encoding="utf-8",
    )
    loaders: dict = {}  # no hosts installed
    _, rules_action = refresh_repo_install(tmp_path, host_loader=loaders)
    assert rules_action in {"updated", "unchanged"}


def test_update_install_propagates_git_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `git fetch` fails, the CalledProcessError should propagate to the caller."""
    method = InstallMethod(kind="git", root=tmp_path)
    monkeypatch.setattr("nightly_core.update.detect_install_method", lambda: method)
    monkeypatch.setattr("nightly_core.update.git_head_commit", lambda _root: "abc")

    def failing_git(args: list[str], *, cwd: Path) -> None:
        raise subprocess.CalledProcessError(1, args, stderr="boom")

    monkeypatch.setattr("nightly_core.update._git", failing_git)
    with pytest.raises(subprocess.CalledProcessError):
        update_install(version="main")
