"""Smoke tests for the `nightly vault` CLI subcommands."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core.cli import app
from nightly_core.config import VaultConfig, load_vault_config

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vault_sample_run"
SAMPLE_RUN_ID = "2026-05-27T16-30-35Z"


@pytest.fixture
def repo_with_run(tmp_path: Path, monkeypatch) -> Path:
    """Init `.nightly/runs/<id>/` + chdir + stub repo_root() so the CLI
    runs against `tmp_path` instead of the real repo."""
    runs_dir = tmp_path / ".nightly" / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(FIXTURE_DIR, runs_dir / SAMPLE_RUN_ID)

    import nightly_core.cli as cli_mod

    monkeypatch.setattr(cli_mod, "repo_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_vault_index_runs(repo_with_run: Path):
    runner = CliRunner()
    # `index` doesn't auto-project, so the vault is empty here.
    result = runner.invoke(app, ["vault", "index"])
    assert result.exit_code == 0, result.output
    assert "indexed" in result.output


def test_vault_build_creates_artifacts(repo_with_run: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "build"])
    assert result.exit_code == 0, result.output
    assert "vault built" in result.output
    # Encyclopedia + dashboard should be on disk
    assert (repo_with_run / ".nightly" / "vault" / "_site" / "index.html").is_file()
    assert (repo_with_run / ".nightly" / "vault" / "_dashboard" / "index.html").is_file()


def test_vault_sync_prs_no_gh(repo_with_run: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync-prs"])
    assert result.exit_code == 0
    assert "synced 0 PR" in result.output


def test_load_vault_config_defaults_when_no_file(tmp_path: Path):
    assert load_vault_config(tmp_path) == VaultConfig()


def test_load_vault_config_reads_block(tmp_path: Path):
    nightly = tmp_path / ".nightly"
    nightly.mkdir()
    (nightly / "config.yml").write_text(
        "vault:\n  enabled: false\n  open_on_brief: true\n",
        encoding="utf-8",
    )
    cfg = load_vault_config(tmp_path)
    assert cfg == VaultConfig(enabled=False, open_on_brief=True)
