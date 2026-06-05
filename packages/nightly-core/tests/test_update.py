"""Tests for nightly_core.update — self-update & per-repo refresh."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from nightly_core.update import (
    REEXEC_BEFORE_ENV,
    REEXEC_SENTINEL_ENV,
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


# ── Phase 9o: re-exec after self-upgrade ──────────────────────────────────


class _FakeReexec:
    """Recording stand-in for `os.execvpe`. Stores the `before` SHA the
    caller would have passed through the env var and raises a sentinel
    exception so the test process doesn't actually replace itself."""

    class _Reexeced(BaseException):
        """Raised by the fake re-exec — `BaseException` so a stray
        `except Exception:` further up the stack can't accidentally
        swallow the simulated process-replacement."""

    def __init__(self) -> None:
        self.called_with: str | None = None
        self.call_count = 0

    def __call__(self, before: str) -> NoReturn:
        self.called_with = before
        self.call_count += 1
        raise _FakeReexec._Reexeced


def _stub_update_machinery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    head_sequence: list[str],
) -> InstallMethod:
    """Wire up the standard fakes for git/uv so update_install runs end-to-end.

    `head_sequence` is consumed by `git_head_commit` calls in order —
    use `["before_sha", "after_sha"]` to simulate a real update, or
    `["sha", "sha"]` to simulate an already-current install.
    """
    method = InstallMethod(kind="git", root=tmp_path)
    monkeypatch.setattr("nightly_core.update.detect_install_method", lambda: method)
    seq = list(head_sequence)
    monkeypatch.setattr(
        "nightly_core.update.git_head_commit",
        lambda _root: seq.pop(0) if seq else "",
    )
    monkeypatch.setattr("nightly_core.update._git", lambda _args, *, cwd: None)
    monkeypatch.setattr("nightly_core.update._uv_sync", lambda _cwd: None)
    return method


def test_update_install_reexecs_when_source_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the on-disk source actually changes, `update_install` must
    re-exec so downstream lazy imports see the new modules.

    Regression guard for the 2026-05 corpus-forge ImportError where
    `BUG_SKILL_MD` was added to `nightly_core` but the running
    `nightly update` process still held the pre-update `nightly_core`
    in `sys.modules`, causing `refresh_repo_install` to fail with
    `cannot import name 'BUG_SKILL_MD'`.
    """
    _stub_update_machinery(monkeypatch, tmp_path=tmp_path, head_sequence=["before", "after"])
    fake = _FakeReexec()
    with pytest.raises(_FakeReexec._Reexeced):
        update_install(version="main", reexec=fake)
    assert fake.call_count == 1
    assert fake.called_with == "before"


def test_update_install_no_reexec_when_source_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent re-runs (same SHA before/after) skip the re-exec —
    no module content changed, so the parent process's namespace is
    still correct."""
    _stub_update_machinery(monkeypatch, tmp_path=tmp_path, head_sequence=["same", "same"])
    fake = _FakeReexec()
    result = update_install(version="main", reexec=fake)
    assert fake.call_count == 0
    assert result[1] == "same"
    assert result[2] == "same"


def test_update_install_no_reexec_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run preview never re-execs — there's no on-disk change to
    propagate to a fresh process."""
    _stub_update_machinery(monkeypatch, tmp_path=tmp_path, head_sequence=["before"])
    fake = _FakeReexec()
    result = update_install(version="main", dry_run=True, reexec=fake)
    assert fake.call_count == 0
    assert result[1] == result[2]  # before == after on dry-run


def test_update_install_post_reexec_skips_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the sentinel env var is set (post-re-exec second pass),
    `update_install` returns immediately with the before SHA from the
    env and the current HEAD — no fetch, no checkout, no uv sync."""
    monkeypatch.setenv(REEXEC_SENTINEL_ENV, "1")
    monkeypatch.setenv(REEXEC_BEFORE_ENV, "parent_before_sha")

    method = InstallMethod(kind="git", root=tmp_path)
    monkeypatch.setattr("nightly_core.update.detect_install_method", lambda: method)
    monkeypatch.setattr("nightly_core.update.git_head_commit", lambda _root: "current_head")

    sentinel_called = {"git": False, "uv_sync": False}

    def fail_git(_args: list[str], *, cwd: Path) -> None:
        sentinel_called["git"] = True
        pytest.fail("git should not run in post-re-exec pass")

    def fail_sync(_cwd: Path) -> None:
        sentinel_called["uv_sync"] = True
        pytest.fail("uv_sync should not run in post-re-exec pass")

    monkeypatch.setattr("nightly_core.update._git", fail_git)
    monkeypatch.setattr("nightly_core.update._uv_sync", fail_sync)

    fake = _FakeReexec()
    result_method, before, after = update_install(version="main", reexec=fake)
    assert result_method is method
    assert before == "parent_before_sha"
    assert after == "current_head"
    assert fake.call_count == 0
    assert sentinel_called == {"git": False, "uv_sync": False}


# ── pull-failure surfacing (silently-stuck install) ──────────────────────


def test_update_install_records_pull_failure_in_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `pull --ff-only` fails, the user sees a note instead of a
    misleading "already current" report.

    Regression guard for the install observed at commit `c98b35b` that
    was stuck a week behind `main`: `pull --ff-only` failed (suppressed),
    `before == after`, and the CLI rendered "already current" — leaving
    the user thinking nothing was wrong.
    """
    method = InstallMethod(kind="git", root=tmp_path)
    monkeypatch.setattr("nightly_core.update.detect_install_method", lambda: method)
    monkeypatch.setattr("nightly_core.update.git_head_commit", lambda _root: "stale_sha")
    monkeypatch.setattr("nightly_core.update._uv_sync", lambda _cwd: None)
    # _remote_ref_sha returning empty disables the second-tier check —
    # we only want to exercise the pull-exception path here.
    monkeypatch.setattr("nightly_core.update._remote_ref_sha", lambda _root, _v: "")

    def git_with_failing_pull(args: list[str], *, cwd: Path) -> None:
        if args[:2] == ["pull", "--quiet"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["git", *args],
                stderr="fatal: Not possible to fast-forward, aborting.\n",
            )
        # All other git calls succeed silently.

    monkeypatch.setattr("nightly_core.update._git", git_with_failing_pull)

    notes: list[str] = []
    update_install(version="main", notes=notes)
    assert any("pull --ff-only" in n for n in notes), notes
    assert any("Not possible to fast-forward" in n for n in notes), notes


def test_update_install_records_pull_failure_recovery_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull-failure note includes the resolve-it command the operator
    should run, so the failure path is actionable not just informative."""
    method = InstallMethod(kind="git", root=tmp_path)
    monkeypatch.setattr("nightly_core.update.detect_install_method", lambda: method)
    monkeypatch.setattr("nightly_core.update.git_head_commit", lambda _root: "stale")
    monkeypatch.setattr("nightly_core.update._uv_sync", lambda _cwd: None)
    monkeypatch.setattr("nightly_core.update._remote_ref_sha", lambda _root, _v: "")

    def git_with_failing_pull(args: list[str], *, cwd: Path) -> None:
        if args[:2] == ["pull", "--quiet"]:
            raise subprocess.CalledProcessError(returncode=128, cmd=args, stderr="fatal: refusing")

    monkeypatch.setattr("nightly_core.update._git", git_with_failing_pull)
    notes: list[str] = []
    update_install(version="main", notes=notes)
    assert any("pull --rebase" in n for n in notes), notes


def test_update_install_no_notes_when_pull_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: every git call returns clean, no pull note emitted."""
    _stub_update_machinery(monkeypatch, tmp_path=tmp_path, head_sequence=["same", "same"])
    monkeypatch.setattr("nightly_core.update._remote_ref_sha", lambda _root, _v: "same")
    notes: list[str] = []
    update_install(version="main", notes=notes, reexec=_FakeReexec())
    assert notes == []


def test_update_install_surfaces_divergence_when_remote_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If pull didn't raise but origin/<version> is still ahead of HEAD
    (e.g. a tag checkout, a detached HEAD, or a silent suppress earlier
    in the pipeline), the divergence check should still fire."""
    _stub_update_machinery(monkeypatch, tmp_path=tmp_path, head_sequence=["stuck", "stuck"])
    monkeypatch.setattr("nightly_core.update._remote_ref_sha", lambda _root, _v: "newer")
    notes: list[str] = []
    update_install(version="main", notes=notes, reexec=_FakeReexec())
    assert any("origin/main" in n and "stuck" in n and "newer" in n for n in notes), notes


def test_update_install_skips_divergence_when_remote_ref_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `_remote_ref_sha` returns empty (tag/SHA checkout, no remote
    ref), the divergence check stays silent — no false positive."""
    _stub_update_machinery(monkeypatch, tmp_path=tmp_path, head_sequence=["any", "any"])
    monkeypatch.setattr("nightly_core.update._remote_ref_sha", lambda _root, _v: "")
    notes: list[str] = []
    update_install(version="main", notes=notes, reexec=_FakeReexec())
    assert notes == []


def test_reexec_helper_sets_sentinel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default reexec helper passes the sentinel + before SHA via
    env so the post-re-exec process can pick up where the parent left
    off. We can't actually exec in a test, so we patch os.execvpe."""
    from nightly_core.update import _reexec_into_new_source

    captured: dict[str, object] = {}

    def fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        captured["file"] = file
        captured["args"] = list(args)
        captured["env"] = dict(env)
        # Don't actually exec — just simulate "would have replaced process".

    monkeypatch.setattr("os.execvpe", fake_execvpe)
    # The function raises after the (faked) execvpe returns; that's a
    # defensive guardrail in production.
    with pytest.raises(RuntimeError, match="execvpe returned unexpectedly"):
        _reexec_into_new_source("before_sha")
    assert captured["env"][REEXEC_SENTINEL_ENV] == "1"  # type: ignore[index]
    assert captured["env"][REEXEC_BEFORE_ENV] == "before_sha"  # type: ignore[index]
