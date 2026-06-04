"""Smoke tests for nightly-core."""

from typer.testing import CliRunner

import nightly_core
from nightly_core.cli import app

runner = CliRunner()


def test_version_attribute_present() -> None:
    """`nightly_core.__version__` is exposed and non-empty.

    Asserting against the literal version (e.g. `"0.0.1"`) couples
    every version bump to a test edit — the existing pattern in
    `test_cli.py::test_info_command_mentions_version_and_design_doc`
    is to compare against the imported `__version__` symbol so the
    test tracks the bump automatically.
    """
    assert nightly_core.__version__
    assert isinstance(nightly_core.__version__, str)


def test_cli_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"nightly {nightly_core.__version__}" in result.stdout


# `nightly info` is covered by
# test_cli.py::test_info_command_mentions_version_and_design_doc,
# which asserts on the actual `__version__` string + the design-doc
# reference. Keeping a weaker copy here would be drift surface.
