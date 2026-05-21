"""Nightly host integration for Cursor (secondary host).

Phase 6: `install` / `uninstall` / `is_installed` / `session_id` /
`auth_status` are real. `dispatch_sub_agent` and `request_approval` arrive
in Phase 7+ when the Cursor Background Agents REST integration lands.
"""

from nightly_host_cursor.integration import CursorHostIntegration
from nightly_host_cursor.skill import SKILL_MD, load_skill_md

__all__ = ["SKILL_MD", "CursorHostIntegration", "load_skill_md"]
