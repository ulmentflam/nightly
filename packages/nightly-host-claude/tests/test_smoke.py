"""Smoke test — package imports cleanly."""

import nightly_host_claude


def test_module_imports() -> None:
    assert nightly_host_claude.__doc__ is not None
