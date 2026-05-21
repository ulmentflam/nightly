"""Nightly host integration for Google Antigravity (secondary host).

Phase 6: `install` / `uninstall` / `is_installed` / `session_id` /
`auth_status` are real. `dispatch_sub_agent`, `request_approval`, and
`brain/<GUID>/` artifact mirroring arrive in Phase 7+.
"""

from nightly_host_antigravity.integration import AntigravityHostIntegration
from nightly_host_antigravity.skill import SKILL_MD, load_skill_md

__all__ = ["SKILL_MD", "AntigravityHostIntegration", "load_skill_md"]
