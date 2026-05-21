"""Loader for the opencode SKILL.md content shipped in this package."""

from __future__ import annotations

from importlib.resources import files

__all__ = ["SKILL_MD", "load_skill_md"]


def load_skill_md() -> str:
    """Return the packaged SKILL.md as a string."""
    return files("nightly_host_opencode").joinpath("skill.md").read_text(encoding="utf-8")


SKILL_MD: str = load_skill_md()
"""The opencode skill markdown — installed by OpencodeHostIntegration.install."""
