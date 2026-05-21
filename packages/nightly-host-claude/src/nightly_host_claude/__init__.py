"""Nightly host integration for Claude Code (primary host).

Phase 1: `install` / `uninstall` / `is_installed` / `session_id` /
`auth_status` are real. `dispatch_sub_agent` and `request_approval` arrive
in Phase 2.
"""

from nightly_host_claude.integration import ClaudeHostIntegration
from nightly_host_claude.skill import SKILL_MD, load_skill_md

__all__ = ["SKILL_MD", "ClaudeHostIntegration", "load_skill_md"]
