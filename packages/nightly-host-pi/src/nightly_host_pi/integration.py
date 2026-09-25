"""PiHostIntegration — the first Nightly host whose keep-alive is not a hook.

The launcher lifecycle looks like every other host: a SKILL.md per scope,
plus the conclude / update / init siblings. The difference is
`install_keepalive_hook`, which writes a **TypeScript extension** instead
of merging a command into a settings file — and writes it to a **global**
path regardless of scope.

That asymmetry is deliberate and is the single most important decision in
RFC 013. pi gates project-local extensions behind project trust, and its
non-interactive modes never prompt: without a saved decision they fall
back to `defaultProjectTrust`, whose default (`ask`) means *ignore project
resources*. A keep-alive under `.pi/extensions/` would load when an
operator tested it by hand and silently fail in every headless run — the
exact class of bug the RFC 010 supervisor exists to clean up after.

Being global means the extension is loaded in every pi session on the
machine, so it gates itself on an armed Nightly run for the session's cwd.
See `keepalive.ts`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from nightly_core import (
    CONCLUDE_SKILL_MD,
    INIT_SKILL_MD,
    UPDATE_SKILL_MD,
    AuthStatus,
    HeadlessResult,
    HostId,
    InstallScope,
    KeepaliveSupport,
    NightlyHostIntegration,
    SpecialistRole,
    SubAgentResult,
    SubprocessRunner,
    repo_root,
    run_subprocess,
)
from nightly_host_pi.skill import KEEPALIVE_TS, SKILL_MD, extension_version

__all__ = ["PiHostIntegration"]


PI_HOME_ENV = "NIGHTLY_PI_HOME"
"""Override for pi's global state root.

Exists for two reasons. Operators who relocate `~/.pi` (containers,
multi-account machines, XDG-strict setups) need a way to tell Nightly
where it went. And the test suite needs one that cannot be forgotten:
this integration writes a *global* file even at project scope, so a
module-level `Path.home()` constant — resolved at import, invisible to
`monkeypatch` — meant any test that constructed the integration without
an override wrote a real file into the developer's home. It did."""


def default_pi_home() -> Path:
    """pi's global state root: `$NIGHTLY_PI_HOME` or `~/.pi/agent`.

    Resolved per call, never cached at import — that is the whole point.
    """
    override = os.environ.get(PI_HOME_ENV)
    return Path(override).expanduser() if override else Path.home() / ".pi" / "agent"


EXTENSION_RELATIVE = Path("extensions") / "nightly" / "index.ts"
"""Where the keep-alive lands under `pi_home` — global at BOTH scopes.

An extension *directory* rather than a bare `nightly.ts` because pi's
subagent example documents that shape for multi-file extensions, and it
leaves room to grow without moving the install path later."""


class PiHostIntegration(NightlyHostIntegration):
    """Nightly host integration for pi (earendil-works/pi)."""

    host_id: HostId = "pi"
    keepalive_support: KeepaliveSupport = "forced"
    keepalive_mechanism = "extension"
    """Descriptive only — `keepalive_support` still answers "can this host be
    forced to continue?" (yes, more reliably than any hook host). This says
    *how*, so `nightly status` and the README matrix can stop implying pi
    installs a hook script. See RFC 013 §14."""

    PROJECT_SKILL_RELATIVE = Path(".pi/skills/nightly/SKILL.md")
    PROJECT_CONCLUDE_RELATIVE = Path(".pi/skills/nightly-conclude/SKILL.md")
    PROJECT_UPDATE_RELATIVE = Path(".pi/skills/nightly-update/SKILL.md")
    PROJECT_INIT_RELATIVE = Path(".pi/skills/nightly-init/SKILL.md")

    USER_SKILL_RELATIVE = Path("skills/nightly/SKILL.md")
    USER_CONCLUDE_RELATIVE = Path("skills/nightly-conclude/SKILL.md")
    USER_UPDATE_RELATIVE = Path("skills/nightly-update/SKILL.md")
    USER_INIT_RELATIVE = Path("skills/nightly-init/SKILL.md")
    """User-scope paths are relative to `pi_home`, not absolute class
    constants. Absolute constants bake `Path.home()` in at import time,
    which no test can redirect — the reason an early test run wrote real
    files into the developer's `~/.pi/agent/skills/`."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        subprocess_runner: SubprocessRunner | None = None,
        pi_home: Path | None = None,
        extension_path: Path | None = None,
    ) -> None:
        self._root = (root or repo_root()).resolve()
        self._subprocess_runner = subprocess_runner
        # `pi_home` relocates every global path at once — user-scope skills,
        # the extension, and trust.json. One knob rather than three, because
        # a test that redirects only some of them writes the rest into the
        # developer's real ~/.pi, which is exactly the accident this
        # parameter exists to prevent.
        self._pi_home = pi_home
        self._extension_path = extension_path

    @property
    def root(self) -> Path:
        return self._root

    @property
    def pi_home(self) -> Path:
        """pi's global state root.

        Resolved lazily so `$NIGHTLY_PI_HOME` set after construction is
        still honored — which is what lets a pytest fixture redirect it
        for integrations the test never constructed itself."""
        return self._pi_home or default_pi_home()

    # ── launcher lifecycle ────────────────────────────────────────────────
    def skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_SKILL_RELATIVE
        return self.pi_home / self.USER_SKILL_RELATIVE

    def conclude_skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_CONCLUDE_RELATIVE
        return self.pi_home / self.USER_CONCLUDE_RELATIVE

    def update_skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_UPDATE_RELATIVE
        return self.pi_home / self.USER_UPDATE_RELATIVE

    def init_skill_path(self, scope: InstallScope) -> Path:
        if scope == "project":
            return self._root / self.PROJECT_INIT_RELATIVE
        return self.pi_home / self.USER_INIT_RELATIVE

    def extension_path(self) -> Path:
        """Absolute path to the keep-alive extension. Global at every scope."""
        if self._extension_path is not None:
            return self._extension_path
        return self.pi_home / EXTENSION_RELATIVE

    async def install(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SKILL_MD, encoding="utf-8")
        for sibling_path, sibling_md in (
            (self.conclude_skill_path(scope), CONCLUDE_SKILL_MD),
            (self.update_skill_path(scope), UPDATE_SKILL_MD),
            (self.init_skill_path(scope), INIT_SKILL_MD),
        ):
            sibling_path.parent.mkdir(parents=True, exist_ok=True)
            sibling_path.write_text(sibling_md, encoding="utf-8")
        self.install_keepalive_hook(scope)

    async def uninstall(self, scope: InstallScope) -> None:
        target = self.skill_path(scope)
        self.uninstall_keepalive_hook(scope)
        for sibling in (
            self.conclude_skill_path(scope),
            self.update_skill_path(scope),
            self.init_skill_path(scope),
        ):
            if sibling.exists():
                sibling.unlink()
                self._trim_skill_parents(sibling)
        if not target.exists():
            return
        target.unlink()
        self._trim_skill_parents(target)

    @staticmethod
    def _trim_skill_parents(skill_file: Path) -> None:
        parent = skill_file.parent
        stop_at = "skills"
        while parent.name and not any(parent.iterdir()):
            removed = parent
            parent = parent.parent
            removed.rmdir()
            if removed.name == stop_at:
                break

    def is_installed(self, scope: InstallScope) -> bool:
        return self.skill_path(scope).is_file()

    # ── keep-alive: extension, not hook ──────────────────────────────────
    def install_keepalive_hook(self, scope: InstallScope) -> None:
        """Write the keep-alive extension. Global at BOTH scopes.

        Unlike the hook hosts, this does **not** short-circuit on
        `scope != "project"` — the extension is global by design, so a
        `--scope user` install needs it just as much as a project one.
        """
        del scope  # the install location does not vary by scope
        path = self.extension_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(KEEPALIVE_TS, encoding="utf-8")

    def uninstall_keepalive_hook(self, scope: InstallScope) -> None:
        """Remove the extension and prune its directory if now empty."""
        del scope
        path = self.extension_path()
        if not path.is_file():
            return
        path.unlink()
        parent = path.parent
        # Only prune the directory we created, and only when empty — an
        # operator may have added files of their own alongside it.
        if parent.name == "nightly" and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    def is_keepalive_hook_installed(self, scope: InstallScope = "project") -> bool:
        del scope
        return self.extension_path().is_file()

    def installed_extension_version(self) -> str | None:
        """Version marker of the extension on disk, or None if absent/foreign.

        `nightly doctor` compares this to the running package's version.
        None means "reinstall" just as surely as a mismatch does — an
        absent marker is a hand-edited or truncated file."""
        path = self.extension_path()
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return extension_version(source)

    # ── project trust ─────────────────────────────────────────────────────
    def trust_path(self) -> Path:
        return self.pi_home / "trust.json"

    def is_project_trusted(self) -> bool:
        """Whether pi will load this repo's project-local resources.

        Mirrors pi's own resolution: the closest saved decision on the
        current path or any ancestor wins. We deliberately do NOT consult
        `defaultProjectTrust` — a global `always` would make this report
        "trusted" for a repo with no decision, which is true for pi but
        useless for the doctor check, whose whole job is telling the
        operator whether *this* repo is covered explicitly.
        """
        decisions = self._read_trust()
        if not decisions:
            return False
        current = self._root
        for candidate in (current, *current.parents):
            value = decisions.get(str(candidate))
            if value is not None:
                return bool(value) if isinstance(value, bool) else str(value).lower() == "yes"
        return False

    def _read_trust(self) -> dict[str, object]:
        try:
            parsed = json.loads(self.trust_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        # pi's file is keyed by canonical directory; tolerate either a flat
        # map or a nested {"trusted": {...}} shape without asserting which.
        inner = parsed.get("trusted")
        if isinstance(inner, dict):
            return inner
        return {k: v for k, v in parsed.items() if isinstance(k, str)}

    def trust_project(self) -> Path:
        """Record a `yes` trust decision for this repo in pi's trust.json.

        Called by `nightly init --host pi` only when the operator opts in.
        Merges rather than replaces — clobbering an operator's other trust
        decisions to add our own would be indefensible.
        """
        path = self.trust_path()
        decisions = self._read_trust()
        decisions[str(self._root)] = "yes"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"trusted": decisions}, indent=2), encoding="utf-8")
        return path

    # ── session identity ──────────────────────────────────────────────────
    def session_id(self) -> str:
        for var in _SESSION_ID_ENV_VARS:
            value = os.environ.get(var)
            if value:
                return value
        return f"detached-{uuid.uuid4()}"

    # ── auth_status ───────────────────────────────────────────────────────
    async def auth_status(self) -> AuthStatus:
        """Heuristic: `pi` binary present and `pi --version` exits 0.

        pi authenticates via `/login` (OAuth or API key) into
        `~/.pi/agent/`, or via provider env vars. We confirm reachability
        and leave `plan` unknown rather than guessing across pi's many
        supported providers.
        """
        binary = shutil.which("pi")
        if binary is None:
            return AuthStatus(ok=False)
        try:
            subprocess.run(  # noqa: ASYNC221 - one-shot init probe
                [binary, "--version"],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return AuthStatus(ok=False)
        return AuthStatus(ok=True, plan="unknown")

    # ── headless mode ─────────────────────────────────────────────────────
    async def run_headless(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        timeout_s: float | None = None,
    ) -> HeadlessResult:
        """Spawn `pi -p --mode json -a` and normalize the result.

        `-a` / `--approve` is not optional here. Headless pi never prompts
        for project trust; without the flag it falls back to
        `defaultProjectTrust` (default `ask` = ignore project resources),
        so the dispatched agent would silently fail to find the Nightly
        skill it was just told to run.

        `--mode json` emits a JSONL event stream. The raw stream is
        returned as `output`; callers wanting just the assistant text use
        `parse_json_stream`.
        """
        binary = shutil.which("pi")
        if binary is None:
            return HeadlessResult(
                host_id=self.host_id,
                output="",
                exit_code=-1,
                elapsed_ms=0,
                error="pi binary not found on PATH",
            )
        argv = [binary, "-p", "--mode", "json", "-a", prompt]
        return await run_subprocess(
            host_id=self.host_id,
            argv=argv,
            cwd=cwd,
            stdin=None,
            timeout_s=timeout_s,
            runner=self._subprocess_runner,
        )

    # ── runtime primitives ────────────────────────────────────────────────
    async def dispatch_sub_agent(
        self,
        *,
        role: SpecialistRole,
        prompt: str,
        cwd: str,
        allowed_tools: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> SubAgentResult:
        raise NotImplementedError(
            "In-session sub-agent dispatch is not a pi primitive — pi's own "
            "subagent extension spawns separate `pi` processes, which is what "
            "`nightly dispatch start <slug> --host pi` already does. Use the "
            "background dispatch path."
        )

    async def request_approval(self, q: str, choices: list[str]) -> str:
        raise NotImplementedError(
            "Native pi UI approval is not wired. Refusals are recorded to "
            ".nightly/runs/<run-id>/proposed/approvals/ for retro review. "
            "Nightly's autonomy contract forbids prompting mid-session anyway."
        )


_SESSION_ID_ENV_VARS = ("PI_SESSION", "PI_AGENT")
"""Env vars pi exposes for the active session. Mirrors the fingerprints
`nightly_core.model_probe` already uses to detect a pi harness."""
