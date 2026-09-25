"""Nightly host integration for pi (earendil-works/pi).

The only host whose keep-alive is an in-process extension rather than a
Stop-hook subprocess — see `keepalive.ts` and RFC 013.
"""

# NB: the `respawn` *function* is deliberately not re-exported here — it
# would shadow the `nightly_host_pi.respawn` submodule, which the
# supervisor and the tests both need to reach by name.

from nightly_host_pi.integration import (
    EXTENSION_RELATIVE,
    PI_HOME_ENV,
    PiHostIntegration,
    default_pi_home,
)
from nightly_host_pi.skill import (
    KEEPALIVE_TS,
    SKILL_MD,
    VERSION_MARKER_PREFIX,
    extension_version,
    load_skill_md,
    parse_json_stream,
    render_keepalive_ts,
)

__all__ = [
    "EXTENSION_RELATIVE",
    "KEEPALIVE_TS",
    "PI_HOME_ENV",
    "SKILL_MD",
    "VERSION_MARKER_PREFIX",
    "PiHostIntegration",
    "default_pi_home",
    "extension_version",
    "load_skill_md",
    "parse_json_stream",
    "render_keepalive_ts",
]
