"""Nightly host integration for vanilla Google Gemini CLI.

Writes a `/nightly` custom command to `.gemini/commands/nightly.toml`
(project) or `~/.gemini/commands/nightly.toml` (user). Companion
commands (`/nightly-conclude`, `/nightly-update`, `/nightly-bug`,
`/nightly-init`) ship alongside. The `AfterAgent` keep-alive hook
merges into `.gemini/settings.json` (shared with Antigravity).
"""

from nightly_host_gemini.integration import GeminiHostIntegration
from nightly_host_gemini.skill import SKILL_MD, load_skill_md, md_to_gemini_toml

__all__ = [
    "SKILL_MD",
    "GeminiHostIntegration",
    "load_skill_md",
    "md_to_gemini_toml",
]
