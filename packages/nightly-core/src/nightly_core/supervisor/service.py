"""Registering the daemon with the OS service manager.

launchd on macOS, systemd --user on Linux. Both are asked to restart the
daemon if it dies (`KeepAlive` / `Restart=always`) — a supervisor that
silently stops supervising is worse than no supervisor, because the
operator believes they are covered.

Install is **opt-in and explicit**. Nothing here runs as a side effect of
`nightly init` or `nightly start`; the operator types `nightly supervisor
install` and confirms. Background processes are a trust boundary, and the
cost of crossing it is one command.

The plist is written as XML directly rather than via `pyobjc` (which the
RFC's risk section suggested): `plutil -lint` validates it, the format
has been stable for the entire supported macOS range, and it avoids
adding a heavyweight macOS-only dependency to a cross-platform package.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "LAUNCHD_LABEL",
    "SYSTEMD_UNIT",
    "ServiceResult",
    "install_service",
    "service_path",
    "service_status",
    "uninstall_service",
]

LAUNCHD_LABEL = "com.nightly.supervisor"
SYSTEMD_UNIT = "nightly-supervisor.service"


@dataclass(frozen=True)
class ServiceResult:
    ok: bool
    path: Path | None
    message: str


def _platform() -> str:
    return platform.system()


def service_path(*, system: str | None = None) -> Path | None:
    """Where this platform's unit file lives, or None if unsupported."""
    match system or _platform():
        case "Darwin":
            return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        case "Linux":
            return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        case _:
            return None


def _binary() -> str:
    """Absolute path to `nightly-supervisor`.

    Resolved at install time, not run time: launchd and systemd start
    with a minimal PATH that almost never includes a uv/pipx shim
    directory, so a bare command name would fail at boot with a confusing
    "no such file" long after the operator stopped watching.
    """
    found = shutil.which("nightly-supervisor")
    return found or "nightly-supervisor"


def render_launchd_plist(binary: str | None = None) -> str:
    exe = binary or _binary()
    log_dir = Path.home() / ".cache" / "nightly" / "supervisor"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>{log_dir / "daemon.log"}</string>
  <key>StandardErrorPath</key>
  <string>{log_dir / "daemon.err"}</string>
</dict>
</plist>
"""


def render_systemd_unit(binary: str | None = None) -> str:
    exe = binary or _binary()
    return f"""[Unit]
Description=Nightly respawn supervisor
Documentation=https://github.com/ulmentflam/nightly

[Service]
Type=simple
ExecStart={exe}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def install_service(*, system: str | None = None, activate: bool = True) -> ServiceResult:
    """Write the unit file and ask the service manager to load it."""
    target = service_path(system=system)
    if target is None:
        return ServiceResult(
            False,
            None,
            f"no service-manager integration for {system or _platform()} — "
            "run `nightly supervisor start` under your own process manager instead",
        )

    content = (
        render_launchd_plist() if (system or _platform()) == "Darwin" else render_systemd_unit()
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ServiceResult(False, target, f"could not write {target}: {exc}")

    if not activate:
        return ServiceResult(True, target, f"wrote {target} (not activated)")

    ok, detail = _activate(system or _platform(), target)
    return ServiceResult(ok, target, detail)


def _activate(system: str, target: Path) -> tuple[bool, str]:
    if system == "Darwin":
        return _run(["launchctl", "load", "-w", str(target)], f"loaded {LAUNCHD_LABEL}")
    if system == "Linux":
        ok, detail = _run(["systemctl", "--user", "daemon-reload"], "reloaded systemd")
        if not ok:
            return ok, detail
        return _run(
            ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], f"enabled {SYSTEMD_UNIT}"
        )
    return False, f"unsupported system {system}"


def uninstall_service(*, system: str | None = None) -> ServiceResult:
    """Unload the service and remove its unit file. Idempotent."""
    target = service_path(system=system)
    if target is None:
        return ServiceResult(False, None, f"no service-manager integration for {_platform()}")

    match system or _platform():
        case "Darwin":
            _run(["launchctl", "unload", "-w", str(target)], "unloaded")
        case "Linux":
            _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], "disabled")

    if not target.exists():
        return ServiceResult(True, target, "not installed (nothing to remove)")
    try:
        target.unlink()
    except OSError as exc:
        return ServiceResult(False, target, f"could not remove {target}: {exc}")
    return ServiceResult(True, target, f"removed {target}")


def service_status(*, system: str | None = None) -> str:
    """One line describing whether the unit is installed and loaded."""
    target = service_path(system=system)
    if target is None:
        return "unsupported platform"
    if not target.exists():
        return "not installed"
    match system or _platform():
        case "Darwin":
            ok, _ = _run(["launchctl", "list", LAUNCHD_LABEL], "")
            return "installed, loaded" if ok else "installed, not loaded"
        case "Linux":
            ok, _ = _run(["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT], "")
            return "installed, active" if ok else "installed, inactive"
        case _:
            return "installed"


def _run(argv: list[str], success: str) -> tuple[bool, str]:
    """Run a service-manager command. Missing binary is a failure, not a crash."""
    if shutil.which(argv[0]) is None:
        return False, f"{argv[0]} not on PATH"
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{argv[0]} failed: {exc!r}"
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout or "").strip() or f"{argv[0]} failed"
    return True, success
