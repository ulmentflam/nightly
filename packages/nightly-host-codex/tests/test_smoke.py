"""Smoke test — package imports cleanly."""

import nightly_host_codex


def test_module_imports() -> None:
    assert nightly_host_codex.__doc__ is not None
