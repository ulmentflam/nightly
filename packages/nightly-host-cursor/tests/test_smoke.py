"""Smoke test — package imports cleanly."""

import nightly_host_cursor


def test_module_imports() -> None:
    assert nightly_host_cursor.__doc__ is not None
