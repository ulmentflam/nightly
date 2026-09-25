"""Workspace-wide pytest fixtures.

Applies to every package's `tests/` directory, because `testpaths` is
`["packages"]` and pytest collects the nearest conftest chain upward.

Its one job today is keeping the suite out of the developer's home
directory. Most host integrations only touch `$HOME` at `--scope user`,
which tests rarely exercise — but the pi integration writes its keep-alive
extension to a **global** path at *every* scope by design (RFC 013), so any
test that constructs it without an override reaches the real `~/.pi`.

That is not hypothetical. It happened during RFC 013's own implementation:
a full-suite run created `~/.pi/agent/extensions/nightly/index.ts` and
`~/.pi/agent/skills/` on the author's machine. Individual test files were
clean; only the combination reproduced it, which is exactly the kind of
pollution that survives review. Hence a blanket redirect rather than a
per-test fixture someone has to remember.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_pi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point pi's global state root at tmp for every test.

    `NIGHTLY_PI_HOME` is read per call by
    `nightly_host_pi.integration.default_pi_home`, so this covers
    integrations the test never constructed itself — the ones reached
    indirectly through `nightly doctor --all`, `nightly init`, or the
    update path.
    """
    monkeypatch.setenv("NIGHTLY_PI_HOME", str(tmp_path / "pi-home"))
