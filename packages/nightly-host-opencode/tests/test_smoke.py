"""Smoke test — package imports cleanly."""

import nightly_host_opencode


def test_module_imports() -> None:
    assert nightly_host_opencode.__doc__ is not None
