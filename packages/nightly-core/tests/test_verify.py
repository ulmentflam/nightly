"""Tests for nightly_core.verify — lint / format detection + runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightly_core.verify import (
    VerifyReport,
    detect_checks,
    run_verify,
)

# ── detection ────────────────────────────────────────────────────────────


def test_detect_empty_repo_returns_nothing(tmp_path: Path) -> None:
    assert detect_checks(tmp_path) == []


def test_detect_pyproject_ruff_picks_up_two_checks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n", encoding="utf-8"
    )
    names = {c.name for c in detect_checks(tmp_path)}
    assert "ruff-check" in names
    assert "ruff-format" in names


def test_detect_pyproject_multiple_tools(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n[tool.black]\n", encoding="utf-8"
    )
    names = {c.name for c in detect_checks(tmp_path)}
    assert {"ruff-check", "ruff-format", "mypy", "black"}.issubset(names)


def test_detect_package_json_eslint_prettier(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name": "app", "devDependencies": '
        '{"eslint": "^9.0.0", "prettier": "^3.0.0", "typescript": "^5.0.0"}}\n',
        encoding="utf-8",
    )
    names = {c.name for c in detect_checks(tmp_path)}
    assert "eslint" in names
    assert "prettier" in names
    assert "tsc" in names


def test_detect_go_mod_adds_gofmt_and_vet(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module foo\ngo 1.22\n", encoding="utf-8")
    names = {c.name for c in detect_checks(tmp_path)}
    assert "gofmt" in names
    assert "go-vet" in names


def test_detect_cargo_toml_adds_fmt_and_clippy(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "foo"\n', encoding="utf-8"
    )
    names = {c.name for c in detect_checks(tmp_path)}
    assert "cargo-fmt" in names
    assert "cargo-clippy" in names


def test_detect_makefile_targets(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "lint:\n\techo hi\n\ncheck:\n\techo hi\n\nrandom:\n\techo hi\n",
        encoding="utf-8",
    )
    names = {c.name for c in detect_checks(tmp_path)}
    assert "make-lint" in names
    assert "make-check" in names
    # We don't enumerate every Makefile target — only the umbrella ones.
    assert "make-random" not in names


# ── execution ────────────────────────────────────────────────────────────


def test_run_verify_dry_run_does_not_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")

    def _should_not_run(*_a, **_kw):
        raise AssertionError("subprocess.run should not fire in dry-run mode")

    monkeypatch.setattr("nightly_core.verify.subprocess.run", _should_not_run)
    report = run_verify(tmp_path, dry_run=True)
    assert isinstance(report, VerifyReport)
    assert report.dry_run is True
    assert all(c.status == "skipped" for c in report.checks)


def test_run_verify_marks_missing_binary_as_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    monkeypatch.setattr("nightly_core.verify.shutil.which", lambda _: None)
    report = run_verify(tmp_path)
    assert all(c.status == "not_found" for c in report.checks)
    assert not report.ok


def test_run_verify_passes_when_subprocess_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n", encoding="utf-8")
    monkeypatch.setattr("nightly_core.verify.shutil.which", lambda _: "/usr/bin/mypy")

    def fake_run(*_a, **_kw):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nightly_core.verify.subprocess.run", fake_run)
    report = run_verify(tmp_path)
    assert report.ok
    assert all(c.status == "ok" for c in report.checks)


def test_run_verify_fails_when_subprocess_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    monkeypatch.setattr("nightly_core.verify.shutil.which", lambda _: "/usr/bin/ruff")

    def fake_run(*_a, **_kw):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="E501 too long\n", stderr=""
        )

    monkeypatch.setattr("nightly_core.verify.subprocess.run", fake_run)
    report = run_verify(tmp_path)
    assert not report.ok
    assert all(c.status == "failed" for c in report.checks)
    assert any("E501" in c.output for c in report.failed)


def test_gofmt_treats_nonempty_stdout_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gofmt -l exits 0 even when files need reformatting — its convention
    is that any non-empty stdout means failure. verify must catch that."""
    (tmp_path / "go.mod").write_text("module foo\n", encoding="utf-8")
    monkeypatch.setattr("nightly_core.verify.shutil.which", lambda _: "/usr/bin/gofmt")

    def fake_run(args, **_kw):
        # gofmt -l reports a list of unformatted file paths on stdout, exit 0.
        if "gofmt" in args[0]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="main.go\n", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nightly_core.verify.subprocess.run", fake_run)
    report = run_verify(tmp_path)
    gofmt = next(c for c in report.checks if c.name == "gofmt")
    assert gofmt.status == "failed"


def test_run_verify_only_filter_narrows_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n", encoding="utf-8"
    )
    monkeypatch.setattr("nightly_core.verify.shutil.which", lambda _: "/x")
    monkeypatch.setattr(
        "nightly_core.verify.subprocess.run",
        lambda *_a, **_kw: subprocess.CompletedProcess([], 0, "", ""),
    )
    report = run_verify(tmp_path, only=["mypy"])
    assert {c.name for c in report.checks} == {"mypy"}


def test_run_verify_handles_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    monkeypatch.setattr("nightly_core.verify.shutil.which", lambda _: "/usr/bin/ruff")

    def fake_run(*_a, **kw):
        raise subprocess.TimeoutExpired(cmd="ruff", timeout=kw.get("timeout", 1))

    monkeypatch.setattr("nightly_core.verify.subprocess.run", fake_run)
    report = run_verify(tmp_path)
    assert not report.ok
    assert all("timed out" in c.output for c in report.failed)
