"""Smoke tests for nightly-core."""

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


# `nightly info` is covered by
# test_cli.py::test_info_command_mentions_version_and_design_doc,
# which asserts on the actual `__version__` string + the design-doc
# reference. Keeping a weaker copy here would be drift surface.
