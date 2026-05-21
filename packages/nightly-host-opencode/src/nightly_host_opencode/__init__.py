"""Nightly host integration for opencode (primary host).

Phase 4: `install` / `uninstall` / `is_installed` / `session_id` /
`auth_status` are real. `dispatch_sub_agent` and `request_approval` arrive
in Phase 5+.
"""

from nightly_host_opencode.integration import OpencodeHostIntegration
from nightly_host_opencode.skill import SKILL_MD, load_skill_md

__all__ = ["SKILL_MD", "OpencodeHostIntegration", "load_skill_md"]
