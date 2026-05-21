"""Smoke test — package imports cleanly."""

import nightly_host_antigravity


def test_module_imports() -> None:
    assert nightly_host_antigravity.__doc__ is not None
