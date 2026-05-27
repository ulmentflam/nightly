"""Tests for nightly_core.check_update — session-start version probe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightly_core import check_update as cu
from nightly_core._version import __version__


@pytest.fixture
def fake_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the cache at a tmp file so the test never touches the real one."""
    cache = tmp_path / "update-check.json"
    monkeypatch.setattr(cu, "CACHE_PATH", cache)
    return cache


# ── version normalization ────────────────────────────────────────────────


def test_normalize_version_strips_leading_v() -> None:
    assert cu._normalize_version("v0.0.1") == "0.0.1"
    assert cu._normalize_version("0.0.1") == "0.0.1"
    assert cu._normalize_version("  v0.0.1  ") == "0.0.1"


# ── UpdateCheckResult ────────────────────────────────────────────────────


def _make_result(
    *,
    current: str = "0.0.1",
    latest: str | None = "v0.0.2",
    channel: cu.InstallChannel = "git",
) -> cu.UpdateCheckResult:
    return cu.UpdateCheckResult(
        current=current,
        latest=latest,
        channel=channel,
        fetched_at=datetime(2026, 5, 27, tzinfo=UTC),
    )


def test_is_outdated_when_versions_differ() -> None:
    assert _make_result(current="0.0.1", latest="v0.0.2").is_outdated


def test_is_outdated_handles_v_prefix() -> None:
    # `current` is bare (from pyproject); `latest` carries the v prefix.
    assert not _make_result(current="0.0.1", latest="v0.0.1").is_outdated


def test_is_outdated_false_when_latest_unknown() -> None:
    """`latest=None` means network probe failed — silent, not outdated."""
    assert not _make_result(latest=None).is_outdated


def test_recommendation_silent_when_up_to_date() -> None:
    assert _make_result(current="0.0.1", latest="v0.0.1").recommendation() is None


def test_recommendation_git_channel_uses_nightly_update() -> None:
    rec = _make_result(channel="git").recommendation()
    assert rec is not None
    assert "/nightly-update" in rec
    assert "0.0.1 → 0.0.2" in rec


def test_recommendation_homebrew_channel_uses_brew_upgrade() -> None:
    rec = _make_result(channel="homebrew").recommendation()
    assert rec is not None
    assert "brew upgrade nightly" in rec
    assert "/nightly-update" not in rec


def test_recommendation_unknown_channel_points_at_docs() -> None:
    rec = _make_result(channel="unknown").recommendation()
    assert rec is not None
    assert "github.com/ulmentflam/nightly" in rec


# ── check_for_update (no real network) ───────────────────────────────────


def test_check_for_update_returns_none_for_dev_install(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Developers working on Nightly itself shouldn't see update nags."""
    monkeypatch.setattr(cu, "detect_install_channel", lambda: "dev")
    assert cu.check_for_update() is None
    # Cache should not have been written
    assert not fake_cache.exists()


def test_check_for_update_calls_fetcher_when_no_cache(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    result = cu.check_for_update(fetcher=lambda: "v9.9.9")
    assert result is not None
    assert result.latest == "v9.9.9"
    assert result.current == __version__
    assert result.channel == "git"
    # Cache was written
    assert fake_cache.is_file()


def test_check_for_update_uses_cache_within_ttl(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    # Seed the cache as if a fetch happened 1 hour ago.
    fetched_at = datetime.now(UTC) - timedelta(hours=1)
    fake_cache.write_text(
        json.dumps(
            {
                "current": __version__,
                "latest": "v0.5.0",
                "channel": "git",
                "fetched_at": fetched_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    sentinel = []

    def fetcher() -> str | None:
        sentinel.append("called")
        return "v999.0.0"

    result = cu.check_for_update(fetcher=fetcher)
    assert result is not None
    assert result.latest == "v0.5.0"  # came from cache, not the fetcher
    assert sentinel == []  # fetcher was never called


def test_check_for_update_bypasses_cache_when_forced(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    fake_cache.write_text(
        json.dumps(
            {
                "current": __version__,
                "latest": "v0.5.0",
                "channel": "git",
                "fetched_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    result = cu.check_for_update(force=True, fetcher=lambda: "v9.9.9")
    assert result is not None
    assert result.latest == "v9.9.9"


def test_check_for_update_refetches_past_ttl(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    # Seed the cache as if a fetch happened 2 days ago.
    fetched_at = datetime.now(UTC) - timedelta(days=2)
    fake_cache.write_text(
        json.dumps(
            {
                "current": __version__,
                "latest": "v0.5.0",
                "channel": "git",
                "fetched_at": fetched_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    result = cu.check_for_update(fetcher=lambda: "v9.9.9")
    assert result is not None
    assert result.latest == "v9.9.9"


def test_check_for_update_invalidates_cache_on_version_change(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the running binary's version differs from the cached `current`,
    the cache is stale by definition — an upgrade already happened."""
    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    fake_cache.write_text(
        json.dumps(
            {
                "current": "0.0.0",  # old version
                "latest": "v0.5.0",
                "channel": "git",
                "fetched_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    result = cu.check_for_update(fetcher=lambda: "v9.9.9")
    assert result is not None
    assert result.latest == "v9.9.9"
    assert result.current == __version__


def test_check_for_update_tolerates_malformed_cache(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    fake_cache.write_text("{not valid json", encoding="utf-8")
    # Should not raise; falls through to the fetcher.
    result = cu.check_for_update(fetcher=lambda: "v9.9.9")
    assert result is not None
    assert result.latest == "v9.9.9"


def test_check_for_update_records_network_failure_as_latest_none(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the fetcher returns None (network failure), the result still
    persists to cache so we don't retry every session — but `latest` is
    None and `is_outdated` is False (no recommendation surfaces)."""
    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    result = cu.check_for_update(fetcher=lambda: None)
    assert result is not None
    assert result.latest is None
    assert not result.is_outdated
    assert result.recommendation() is None


# ── detect_install_channel ───────────────────────────────────────────────


def test_detect_install_channel_homebrew_via_cellar_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file path containing a `Cellar/` segment identifies a Homebrew install."""
    fake_path = tmp_path / "opt" / "homebrew" / "Cellar" / "nightly" / "0.0.1" / "libexec"
    fake_path.mkdir(parents=True)
    fake_file = fake_path / "nightly_core" / "check_update.py"
    fake_file.parent.mkdir()
    fake_file.write_text("# fake", encoding="utf-8")

    # Patch __file__ so detect_install_channel reads the fake path
    monkeypatch.setattr(cu, "__file__", str(fake_file))
    assert cu.detect_install_channel() == "homebrew"


def test_detect_install_channel_git_at_default_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install.sh-style git clone at ~/.local/share/nightly → channel=git."""
    fake_home = tmp_path / "home" / "user"
    fake_clone = fake_home / ".local" / "share" / "nightly"
    fake_clone.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda _: fake_home))

    from nightly_core import update as upd

    monkeypatch.setattr(
        upd,
        "detect_install_method",
        lambda: upd.InstallMethod(kind="git", root=fake_clone),
    )
    monkeypatch.setattr(
        cu,
        "detect_install_method",
        lambda: upd.InstallMethod(kind="git", root=fake_clone),
    )
    # Ensure we don't accidentally hit the homebrew branch via the
    # check_update.py path containing "Cellar/" — point __file__ to a
    # clean path.
    monkeypatch.setattr(cu, "__file__", str(fake_clone / "nightly_core" / "check_update.py"))

    assert cu.detect_install_channel() == "git"


def test_detect_install_channel_dev_for_workspace_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A git install at a non-default path is a developer's workspace clone."""
    fake_home = tmp_path / "home" / "user"
    fake_clone = fake_home / "Workspace" / "nightly"
    fake_clone.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _: fake_home))

    from nightly_core import update as upd

    monkeypatch.setattr(
        cu,
        "detect_install_method",
        lambda: upd.InstallMethod(kind="git", root=fake_clone),
    )
    monkeypatch.setattr(cu, "__file__", str(fake_clone / "nightly_core" / "check_update.py"))

    assert cu.detect_install_channel() == "dev"


def test_detect_install_channel_unknown_for_non_git_non_brew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No Cellar/, no git checkout — channel falls through to 'unknown'."""
    from nightly_core import update as upd

    monkeypatch.setattr(
        cu,
        "detect_install_method",
        lambda: upd.InstallMethod(kind="unknown", root=None),
    )
    monkeypatch.setattr(
        cu, "__file__", str(tmp_path / "site-packages" / "nightly_core" / "check_update.py")
    )
    assert cu.detect_install_channel() == "unknown"


# ── network fallback chain ───────────────────────────────────────────────


def test_fetch_latest_tag_uses_gh_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gh path returns a tag → urllib is never called."""
    monkeypatch.setattr(cu.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    import subprocess as sp

    def fake_run(*_a, **_kw):
        return sp.CompletedProcess(args=[], returncode=0, stdout="v0.5.0\n", stderr="")

    monkeypatch.setattr(cu.subprocess, "run", fake_run)
    urllib_called = []
    monkeypatch.setattr(
        cu.urllib.request, "urlopen", lambda *_a, **_kw: urllib_called.append("nope")  # type: ignore[arg-type,return-value]
    )

    assert cu._fetch_latest_tag() == "v0.5.0"
    assert urllib_called == []  # urllib never invoked


def test_fetch_latest_tag_falls_back_to_urllib_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gh missing → urllib path returns the tag."""
    monkeypatch.setattr(cu.shutil, "which", lambda _: None)  # no gh

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"tag_name": "v0.7.0"}'

    def fake_urlopen(*_a, **_kw):
        return _FakeResp()

    monkeypatch.setattr(cu.urllib.request, "urlopen", fake_urlopen)
    # urllib path uses json.load on the response — patch read() reader.
    # Easier: patch json.load directly to skip the stream machinery.
    monkeypatch.setattr(cu.json, "load", lambda _resp: {"tag_name": "v0.7.0"})

    assert cu._fetch_latest_tag() == "v0.7.0"


def test_fetch_latest_tag_returns_none_when_both_paths_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No gh + urllib raises → returns None (no recommendation)."""
    import urllib.error

    monkeypatch.setattr(cu.shutil, "which", lambda _: None)

    def boom(*_a, **_kw):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(cu.urllib.request, "urlopen", boom)
    assert cu._fetch_latest_tag() is None


# ── CLI integration ──────────────────────────────────────────────────────


def test_cli_check_update_silent_when_up_to_date(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI must print NOTHING on stdout when up-to-date — that's
    how the agent decides 'no recommendation to surface'."""
    from typer.testing import CliRunner

    from nightly_core.cli import app

    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    monkeypatch.setattr(cu, "_fetch_latest_tag", lambda: f"v{__version__}")

    runner = CliRunner()
    result = runner.invoke(app, ["check-update", "--force"])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_cli_check_update_emits_recommendation_when_outdated(
    fake_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nightly_core.cli import app

    monkeypatch.setattr(cu, "detect_install_channel", lambda: "git")
    monkeypatch.setattr(cu, "_fetch_latest_tag", lambda: "v9.9.9")

    runner = CliRunner()
    result = runner.invoke(app, ["check-update", "--force"])
    assert result.exit_code == 0
    assert "upgrade available" in result.stdout
    assert "/nightly-update" in result.stdout
