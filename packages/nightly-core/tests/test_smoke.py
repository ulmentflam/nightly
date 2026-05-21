"""Smoke tests for nightly-core. Real coverage lands with Phase 1+."""

from typer.testing import CliRunner

import nightly_core
from nightly_core.cli import app

runner = CliRunner()


def test_version_attribute_present() -> None:
    assert nightly_core.__version__ == "0.0.1"


def test_cli_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "nightly 0.0.1" in result.stdout


def test_cli_info_command() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "Phase" in result.stdout
    assert "brainstorm.html" in result.stdout
