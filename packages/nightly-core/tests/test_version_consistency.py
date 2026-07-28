"""Every packaged version string agrees with `_version.__version__`.

This guard exists because they stopped agreeing. `_version.py` was bumped
to 0.0.14 while all eight `pyproject.toml` files sat at 0.0.12 — so
`nightly version` reported 0.0.14 while anything built from the workspace
was stamped 0.0.12. Nothing caught it, because nothing was looking.

The failure is quiet by construction: a source install reads
`_version.py`, a wheel reads `pyproject.toml`, and neither one can see
the other disagreeing. It only surfaces when someone tries to work out
which version a bug report came from.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from nightly_core._version import __version__

# tests/ -> nightly-core/ -> packages/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

PYPROJECTS = sorted(
    [REPO_ROOT / "pyproject.toml", *(REPO_ROOT / "packages").glob("*/pyproject.toml")]
)


def test_the_workspace_layout_is_what_this_test_assumes() -> None:
    """If the repo is restructured, fail loudly here rather than silently
    checking an empty list and passing."""
    assert (REPO_ROOT / "packages").is_dir(), REPO_ROOT
    # Root + nightly-core + six host packages.
    assert len(PYPROJECTS) >= 8, [str(p) for p in PYPROJECTS]


@pytest.mark.parametrize("path", PYPROJECTS, ids=lambda p: p.parent.name)
def test_pyproject_version_matches_version_module(path: Path) -> None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    declared = data.get("project", {}).get("version")
    assert declared == __version__, (
        f"{path.relative_to(REPO_ROOT)} declares {declared!r}, "
        f"but nightly_core._version.__version__ is {__version__!r}. "
        "Bump both together."
    )


def test_version_is_a_plain_release_triple() -> None:
    """The release tag is `v` + this string, and the Homebrew formula's
    tarball URL is built from the tag. A stray suffix breaks that chain."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__
