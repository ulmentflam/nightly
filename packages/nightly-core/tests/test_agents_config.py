"""Tests for the `agents:` config block (v0.0.7+)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core.cli import app
from nightly_core.config import AgentsConfig, load_agents_config


def _write_config(root: Path, body: str) -> None:
    (root / ".nightly").mkdir(parents=True, exist_ok=True)
    (root / ".nightly" / "config.yml").write_text(body, encoding="utf-8")


# ── load_agents_config ───────────────────────────────────────────────────


def test_load_agents_config_defaults_when_file_missing(tmp_path: Path) -> None:
    """No config file → all defaults."""
    cfg = load_agents_config(tmp_path)
    assert cfg.background_dispatch is True
    assert isinstance(cfg, AgentsConfig)


def test_load_agents_config_defaults_when_block_missing(tmp_path: Path) -> None:
    """Config exists but has no `agents:` block → defaults."""
    _write_config(tmp_path, "git:\n  base_branch: main\n")
    cfg = load_agents_config(tmp_path)
    assert cfg.background_dispatch is True


def test_load_agents_config_respects_explicit_false(tmp_path: Path) -> None:
    _write_config(tmp_path, "agents:\n  background_dispatch: false\n")
    cfg = load_agents_config(tmp_path)
    assert cfg.background_dispatch is False


def test_load_agents_config_respects_explicit_true(tmp_path: Path) -> None:
    """Even when explicit, `true` round-trips cleanly."""
    _write_config(tmp_path, "agents:\n  background_dispatch: true\n")
    cfg = load_agents_config(tmp_path)
    assert cfg.background_dispatch is True


def test_load_agents_config_handles_malformed_yaml(tmp_path: Path) -> None:
    """Malformed YAML → defaults, not exception."""
    _write_config(tmp_path, "agents: {{{ malformed\n")
    cfg = load_agents_config(tmp_path)
    assert cfg.background_dispatch is True


def test_load_agents_config_handles_non_mapping_block(tmp_path: Path) -> None:
    """`agents:` block as a non-dict (string, list) → defaults."""
    _write_config(tmp_path, "agents: foo\n")
    cfg = load_agents_config(tmp_path)
    assert cfg.background_dispatch is True


def test_load_agents_config_accepts_root_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`root=None` resolves via the cwd-derived repo root — matches the
    other `load_*_config` helper signatures."""
    monkeypatch.chdir(tmp_path)
    cfg = load_agents_config(None)
    assert cfg.background_dispatch is True


# ── nightly status surface ──────────────────────────────────────────────


def test_status_prints_background_dispatch_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nightly status` shows the agents preference; default reads as
    `dispatch=background`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("nightly_core.cli.repo_root", lambda: tmp_path)
    (tmp_path / ".nightly").mkdir()
    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "agents:    dispatch=background" in result.output


def test_status_prints_foreground_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator with `background_dispatch: false` sees `dispatch=foreground`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("nightly_core.cli.repo_root", lambda: tmp_path)
    _write_config(tmp_path, "agents:\n  background_dispatch: false\n")
    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "agents:    dispatch=foreground" in result.output
    assert "Task tool" in result.output


# ── Default config template carries the agents block ─────────────────────


def test_default_config_template_includes_agents_block_cli() -> None:
    """`nightly init` writes a default config that documents the agents
    preference — operators see it on day one."""
    from nightly_core.cli import _DEFAULT_CONFIG_YML

    assert "agents:" in _DEFAULT_CONFIG_YML
    assert "background_dispatch: true" in _DEFAULT_CONFIG_YML


def test_default_config_template_includes_agents_block_doctor() -> None:
    """`nightly doctor` writes the same template when scaffolding."""
    from nightly_core.doctor import _DEFAULT_CONFIG_YML

    assert "agents:" in _DEFAULT_CONFIG_YML
    assert "background_dispatch: true" in _DEFAULT_CONFIG_YML


# ── Skill text references the preference ────────────────────────────────


def test_claude_skill_documents_dispatch_mode_preference() -> None:
    """Operators reading the Claude skill should see the v0.0.7+
    dispatch-mode preference paragraph + config reference."""
    from nightly_host_claude.skill import SKILL_MD

    assert "agents.background_dispatch" in SKILL_MD
    assert "Dispatch mode preference" in SKILL_MD
    # The default is documented as the recommended path.
    assert "stays free" in SKILL_MD
