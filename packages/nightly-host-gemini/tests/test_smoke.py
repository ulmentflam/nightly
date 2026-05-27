"""Smoke test — package imports cleanly."""

import nightly_host_gemini


def test_module_imports() -> None:
    assert nightly_host_gemini.__doc__ is not None
