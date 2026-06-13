"""`nightly doctor` — diagnose & repair an existing Nightly install.

A `nightly init` from a previous version, a half-cleaned `.nightly/`,
a manually-deleted SKILL.md, a renamed companion skill file — over a
few weeks every long-lived repo collects little drift between what the
running Nightly expects and what's actually on disk. `doctor` is the
boring, idempotent broom that walks the install surface and puts it
back together without the user having to remember the exact sequence
of `init` flags that produced their setup.

What it checks (and repairs by default):

1. `.nightly/` scaffold — the five canonical subdirs from `cli.py`
   (`runs`, `plans`, `atlas`, `memory`, `prompts`).
2. `.nightly/config.yml` — written from the default template if absent.
3. AGENTS.md / CLAUDE.md rules block — re-seeded via `seed_rules`.
4. Per host already present in the repo (any of its skill files exist
   at `scope`): re-run `integration.install(scope)`. This is idempotent
   and re-drops the main SKILL.md, the `/nightly-conclude` companion,
   the `/nightly-update` companion, and the Stop-hook entry (for hosts
   in the `forced` keep-alive tier). Hosts the user never installed are
   left alone unless the caller explicitly passes them via
   `extra_hosts`.

Design parallels `update.refresh_repo_install` — both walk host loaders
and call `install("project")` — but doctor's contract is broader: it
also reconciles the non-host scaffold (`.nightly/`, config, rules) and
is the right command to run after a manual edit that may have left
things half-broken. `update` is for "I pulled a new Nightly"; doctor
is for "make my install correct."
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from nightly_core.config import load_git_config
from nightly_core.contract import HostId, InstallScope, NightlyHostIntegration
from nightly_core.paths import nightly_dir
from nightly_core.rules import seed_rules
from nightly_core.worktree import is_icloud_path

__all__ = [
    "DEFAULT_NIGHTLY_SUBDIRS",
    "DoctorCheck",
    "DoctorReport",
    "diagnose_and_repair",
]


# Re-stated here rather than imported from cli.py to keep the dependency
# direction clean (cli imports doctor, not the other way around).
DEFAULT_NIGHTLY_SUBDIRS: tuple[str, ...] = ("runs", "plans", "atlas", "memory", "prompts")


_DEFAULT_CONFIG_YML = """\
# .nightly/config.yml — written by `nightly init`. Edit as needed.
# See `.nightly/config.yml.example` (if present) for the full schema, or
# `.planning/brainstorm.html` §05 for the design rationale.

hosts:
  - claude

git:
  base_branch:   main
  branch_prefix: nightly/
  wip_prefix:    nightly/wip-
  protected:     [main, master, "release/*"]
  # Where per-task worktrees are placed. Leave unset to nest them under a
  # sibling `<repo>-nightly/` dir. Set an absolute/`~` path to keep trees off a
  # synced filesystem — REQUIRED on macOS if this repo lives in iCloud Drive
  # (~/Documents, ~/Desktop), where FileProvider silently corrupts git state.
  # Nightly auto-relocates to ~/.cache/nightly/worktrees if it detects iCloud.
  # worktree_root: ~/.cache/nightly/worktrees

refuse:
  destructive_git:        true
  production_state:       true
  external_communication: true
  network_egress_unknown: true
  scope_creep:            true
  bypass_test_or_type:    true

# pr_feedback governs the `pr_rescue` cascade step (Phase 9).
# - `enabled` flips the whole feature off without removing the block.
# - `review_bots` extends the default bot allowlist (CodeRabbit, Cursor BugBot,
#   Copilot reviewer, Greptile, Amp, etc.) with project-specific accounts.
# - `treat_bots_as_human` flips a bot login into the "human" bucket — useful
#   for an internally-trusted automation that should outrank ordinary bots.
pr_feedback:
  enabled:              true
  review_bots:          []
  treat_bots_as_human:  []

# ideate governs the proposer suite (RFC 009).
# - `category_ordering: false` reverts to score-only ordering.
# - `synthesis.enabled: false` disables the LLM-driven proposer.
ideate:
  category_ordering: true
  synthesis:
    enabled:          true
    timeout_seconds:  120
    max_proposals:    25

# agents governs interactive specialist dispatch.
# - `background_dispatch: true` (default) — `nightly dispatch start`
#   spawns specialists as detached host processes so the operator's
#   chat stays free.
# - `background_dispatch: false` — fall back to the host's Task tool,
#   which blocks the calling chat until the sub-agent returns.
agents:
  background_dispatch: true
"""


CheckStatus = Literal["ok", "repaired", "missing", "skipped", "error", "warning"]


@dataclass(frozen=True)
class DoctorCheck:
    """One row of the doctor report — name, status, optional detail."""

    name: str
    description: str
    status: CheckStatus
    detail: str = ""


@dataclass(frozen=True)
class DoctorReport:
    """Aggregate result; printed by the CLI."""

    checks: tuple[DoctorCheck, ...]
    dry_run: bool

    @property
    def repaired(self) -> tuple[DoctorCheck, ...]:
        return tuple(c for c in self.checks if c.status == "repaired")

    @property
    def missing(self) -> tuple[DoctorCheck, ...]:
        return tuple(c for c in self.checks if c.status == "missing")

    @property
    def errors(self) -> tuple[DoctorCheck, ...]:
        return tuple(c for c in self.checks if c.status == "error")

    @property
    def healthy(self) -> bool:
        """True iff no missing items and no errors after this run."""
        return not self.missing and not self.errors


# ── per-area helpers ──────────────────────────────────────────────────────


def _check_nightly_scaffold(root: Path, *, dry_run: bool) -> DoctorCheck:
    """Ensure `.nightly/` plus its canonical subdirs exist."""
    nightly = nightly_dir(root)
    missing_subs = [sub for sub in DEFAULT_NIGHTLY_SUBDIRS if not (nightly / sub).is_dir()]
    if not missing_subs:
        return DoctorCheck(
            name="nightly_scaffold",
            description=".nightly/ scaffold",
            status="ok",
        )
    if dry_run:
        return DoctorCheck(
            name="nightly_scaffold",
            description=".nightly/ scaffold",
            status="missing",
            detail=f"would create: {', '.join(missing_subs)}",
        )
    for sub in missing_subs:
        (nightly / sub).mkdir(parents=True, exist_ok=True)
    return DoctorCheck(
        name="nightly_scaffold",
        description=".nightly/ scaffold",
        status="repaired",
        detail=f"created: {', '.join(missing_subs)}",
    )


def _check_config(root: Path, *, dry_run: bool) -> DoctorCheck:
    """Ensure `.nightly/config.yml` exists; never clobbers user edits."""
    config = nightly_dir(root) / "config.yml"
    if config.is_file():
        return DoctorCheck(
            name="config",
            description=".nightly/config.yml",
            status="ok",
        )
    if dry_run:
        return DoctorCheck(
            name="config",
            description=".nightly/config.yml",
            status="missing",
            detail="would write default config",
        )
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_DEFAULT_CONFIG_YML, encoding="utf-8")
    return DoctorCheck(
        name="config",
        description=".nightly/config.yml",
        status="repaired",
        detail="wrote default config",
    )


def _check_worktree_location(root: Path) -> DoctorCheck:
    """Warn (non-fatally) when the repo sits under iCloud/FileProvider sync.

    Worktrees default to a sibling `<repo>-nightly/`, so an iCloud repo means
    iCloud worktrees — where `fileproviderd` silently corrupts git state.
    Nightly auto-relocates to `~/.cache/nightly/worktrees` at runtime, but
    pinning `git.worktree_root` to a non-synced path is clearer. This is purely
    diagnostic (no repair), so it never writes and never fails the run; off
    macOS / non-iCloud repos report `ok`.
    """
    name, desc = "worktree_location", "worktree location"
    if not is_icloud_path(root):
        return DoctorCheck(name=name, description=desc, status="ok")
    git_cfg = load_git_config(root)
    if git_cfg.worktree_root and not is_icloud_path(Path(git_cfg.worktree_root).expanduser()):
        return DoctorCheck(
            name=name,
            description=desc,
            status="ok",
            detail=f"repo under iCloud; worktrees pinned to {git_cfg.worktree_root}",
        )
    return DoctorCheck(
        name=name,
        description=desc,
        status="warning",
        detail=(
            "repo under iCloud Drive — set git.worktree_root to a non-synced path "
            "(meanwhile Nightly auto-relocates worktrees to ~/.cache/nightly/worktrees)"
        ),
    )


def _check_rules(root: Path, *, dry_run: bool) -> DoctorCheck:
    """Re-seed AGENTS.md / CLAUDE.md rules block.

    Uses `create_if_absent=False` because doctor's job is to repair what's
    there, not to introduce new rules files into a repo that intentionally
    doesn't have them. If the file exists and contains the marker, the
    block is replaced; if the file exists without the marker, the block
    is appended (preserving the rest); if the file is absent, doctor
    leaves it alone — mirrors `update.refresh_repo_install`.
    """
    if dry_run:
        outcomes = seed_rules(root, create_if_absent=False)
        will_change = [o for o in outcomes if o.action in {"created", "updated"}]
        if not will_change:
            return DoctorCheck(
                name="rules",
                description="AGENTS.md / CLAUDE.md rules block",
                status="ok",
            )
        names = ", ".join(o.path.name for o in will_change)
        return DoctorCheck(
            name="rules",
            description="AGENTS.md / CLAUDE.md rules block",
            status="missing",
            detail=f"would re-seed: {names}",
        )

    # Non-dry-run path: seed_rules already wrote. We just classify the
    # outcome — `unchanged` / `skipped` means nothing changed; otherwise
    # we report what got refreshed.
    outcomes = seed_rules(root, create_if_absent=False)
    changed = [o for o in outcomes if o.action in {"created", "updated"}]
    if not changed:
        return DoctorCheck(
            name="rules",
            description="AGENTS.md / CLAUDE.md rules block",
            status="ok",
        )
    names = ", ".join(o.path.name for o in changed)
    return DoctorCheck(
        name="rules",
        description="AGENTS.md / CLAUDE.md rules block",
        status="repaired",
        detail=f"re-seeded: {names}",
    )


def _host_is_present(
    host_id: str,
    integration: NightlyHostIntegration,
    scope: InstallScope,
) -> bool:
    """A host counts as present if any of its skill files exist at `scope` OR
    the corresponding host directory exists.

    Reading `is_installed` alone misses the cases the doctor command is
    designed for: main SKILL.md missing but companions still there, or
    vice versa. Checking all skill surfaces + directories catches partial drift
    and supports auto-detection.
    """
    paths: list[Path | None] = []
    if hasattr(integration, "skill_path"):
        paths.append(integration.skill_path(scope))  # type: ignore[attr-defined]
    paths.append(integration.conclude_skill_path(scope))
    paths.append(integration.update_skill_path(scope))
    paths.append(integration.bug_skill_path(scope))
    paths.append(integration.init_skill_path(scope))
    if any(p is not None and p.is_file() for p in paths):
        return True

    # Check config directory presence
    dirs_by_host = {
        "claude": [Path(".claude")] if scope == "project" else [Path.home() / ".claude"],
        "codex": [Path(".codex")] if scope == "project" else [Path.home() / ".codex"],
        "cursor": [Path(".cursor")] if scope == "project" else [Path.home() / ".cursor"],
        "opencode": [Path(".opencode")] if scope == "project" else [Path.home() / ".opencode"],
        "antigravity": (
            [Path(".gemini/antigravity"), Path(".gemini")]
            if scope == "project"
            else [Path.home() / ".gemini/antigravity", Path.home() / ".gemini"]
        ),
        "gemini": (
            [Path(".gemini/commands"), Path(".gemini")]
            if scope == "project"
            else [Path.home() / ".gemini"]
        ),
    }

    dirs_to_check = dirs_by_host.get(host_id, [])
    for d in dirs_to_check:
        full_path = d if d.is_absolute() else integration.root / d
        if full_path.is_dir():
            return True

    return False


_REQUIRED_SYNTHESIS_PROMPT_ANCHORS: tuple[str, ...] = (
    "objectives",
    "rationale",
    "JSON array",
    "`cleaning`",
    "`refactoring`",
    "`housekeeping`",
    "`convenience`",
    "`capability`",
    "Destructive git",
    "Production state",
    "Scope creep",
)
"""RFC 009 §C3 — sanity anchors the installed `synthesis_prompt.md`
must contain. If any are missing the prompt has drifted from the
shape the parser + cascade expect; the doctor reports it as
`missing` and the remediation is `nightly update` (the prompt
ships inside the wheel so per-package update is the canonical
refresh)."""


def _check_synthesis_prompt() -> DoctorCheck:
    """RFC 009 §C3 — verify the installed synthesis prompt template
    still contains the load-bearing constraint strings.

    The template ships inside the `nightly-core` wheel via
    `importlib.resources`, so the only realistic drift mode is "the
    operator's installed Nightly is stale" — which the existing
    `nightly update` + `check_update` chain already handles. This
    check is a sanity tripwire: if the prompt is on disk but missing
    the anchor strings (manual edit, monkey-patched fixture leak,
    future RFC accidentally dropping a constraint), the doctor flags
    it so the operator knows synthesis output may be untrustworthy.
    """
    from nightly_core.proposers.synthesis import load_synthesis_prompt  # noqa: PLC0415

    try:
        prompt = load_synthesis_prompt()
    except (OSError, FileNotFoundError) as exc:
        return DoctorCheck(
            name="synthesis_prompt",
            description="RFC 009 synthesis_prompt.md",
            status="error",
            detail=f"unable to load: {exc!r}",
        )
    missing = [token for token in _REQUIRED_SYNTHESIS_PROMPT_ANCHORS if token not in prompt]
    if not missing:
        return DoctorCheck(
            name="synthesis_prompt",
            description="RFC 009 synthesis_prompt.md",
            status="ok",
        )
    return DoctorCheck(
        name="synthesis_prompt",
        description="RFC 009 synthesis_prompt.md",
        status="missing",
        detail=(
            f"prompt missing required anchors: {', '.join(missing)}. "
            "Re-install via `nightly update` to refresh."
        ),
    )


_REQUIRED_SKILL_TOKENS: tuple[tuple[str, str], ...] = (
    ("seed-rfc", "seed-rfc toolkit row (RFC 005)"),
)
"""Substring tokens the main SKILL.md must contain.

When the installed file exists but is missing a token, the doctor
marks the host as needing repair so re-running `integration.install`
refreshes the file from the package source. Catches the failure
mode where a user upgraded the binary but their installed SKILL.md
is still the previous version — the file-presence check wouldn't
notice. Each tuple is `(token, human_label)`."""


def _host_needs_repair(
    integration: NightlyHostIntegration,
    scope: InstallScope,
) -> tuple[bool, list[str]]:
    """Return (needs_repair, missing_pieces_list) for a host at `scope`."""
    missing: list[str] = []
    main = (
        integration.skill_path(scope)  # type: ignore[attr-defined]
        if hasattr(integration, "skill_path")
        else None
    )
    if main is not None and not main.is_file():
        missing.append("main skill")
    elif main is not None:
        # File exists — check it carries the tokens current Nightly
        # expects. Missing tokens trigger a re-install.
        try:
            content = main.read_text(encoding="utf-8")
        except OSError:
            content = ""
        for token, label in _REQUIRED_SKILL_TOKENS:
            if token not in content:
                missing.append(label)
    conclude = integration.conclude_skill_path(scope)
    if conclude is not None and not conclude.is_file():
        missing.append("conclude skill")
    upd = integration.update_skill_path(scope)
    if upd is not None and not upd.is_file():
        missing.append("update skill")
    bug = integration.bug_skill_path(scope)
    if bug is not None and not bug.is_file():
        missing.append("bug skill")
    init = integration.init_skill_path(scope)
    if init is not None and not init.is_file():
        missing.append("init skill")
    if (
        scope == "project"
        and integration.keepalive_support == "forced"
        and not integration.is_keepalive_hook_installed(scope)
    ):
        missing.append("stop hook")
    return (bool(missing), missing)


def _check_host(
    host_id: HostId | str,
    integration: NightlyHostIntegration,
    *,
    scope: InstallScope,
    dry_run: bool,
    force: bool,
) -> DoctorCheck:
    """Reconcile a host's full install surface.

    `force=True` (extra_hosts caller) installs even when the host is
    absent from the repo. `force=False` only repairs hosts that already
    have at least one skill file present.
    """
    present = _host_is_present(host_id, integration, scope)
    if not present and not force:
        return DoctorCheck(
            name=f"host:{host_id}",
            description=f"host {host_id}",
            status="skipped",
            detail="not installed in this repo",
        )

    needs, missing_pieces = _host_needs_repair(integration, scope)
    if not needs:
        return DoctorCheck(
            name=f"host:{host_id}",
            description=f"host {host_id}",
            status="ok",
        )

    if dry_run:
        return DoctorCheck(
            name=f"host:{host_id}",
            description=f"host {host_id}",
            status="missing",
            detail=f"would repair: {', '.join(missing_pieces)}",
        )

    try:
        asyncio.run(integration.install(scope))
    except Exception as exc:  # surface, don't crash on per-host quirks
        return DoctorCheck(
            name=f"host:{host_id}",
            description=f"host {host_id}",
            status="error",
            detail=f"install failed: {exc!r}",
        )
    return DoctorCheck(
        name=f"host:{host_id}",
        description=f"host {host_id}",
        status="repaired",
        detail=f"repaired: {', '.join(missing_pieces)}",
    )


# ── public entry point ────────────────────────────────────────────────────


HostLoader = Callable[[Path | None], NightlyHostIntegration]


def diagnose_and_repair(
    root: Path,
    *,
    dry_run: bool = False,
    scope: InstallScope = "project",
    extra_hosts: Iterable[str] = (),
    host_loader: Mapping[str, HostLoader] | None = None,
) -> DoctorReport:
    """Walk the install surface and repair (or report) drift.

    - `dry_run=True` just diagnoses — every "would change" item shows up
      as `missing` and nothing is written.
    - `extra_hosts` forces those hosts to be (re-)installed even if no
      skill files exist for them in the repo. Pass an empty iterable to
      stick to the "repair what's already there" default.
    - `host_loader` is injected by tests; production calls leave it None
      and we lazy-import the CLI registry to avoid a top-of-module cycle.
      Typed as `Mapping[str, HostLoader]` so callers can pass either a
      plain dict or the CLI's `dict[HostId, HostLoader]` registry (HostId
      is a `str` Literal — covariant via `Mapping` but not `dict`).
    """
    loaders: Mapping[str, HostLoader]
    if host_loader is None:
        from nightly_core.cli import _HOST_LOADERS  # noqa: PLC0415 - lazy

        # `_HOST_LOADERS` is keyed by `HostId` (a `Literal[str]`), but
        # `Mapping` is invariant in its key type even when the runtime
        # value is a `str`. Cast at the boundary — the iteration below
        # treats the keys as plain strings.
        loaders = cast("Mapping[str, HostLoader]", _HOST_LOADERS)
    else:
        loaders = host_loader

    extra_set = {h.strip() for h in extra_hosts if h and h.strip()}

    checks: list[DoctorCheck] = []
    checks.append(_check_nightly_scaffold(root, dry_run=dry_run))
    checks.append(_check_config(root, dry_run=dry_run))
    checks.append(_check_worktree_location(root))
    checks.append(_check_rules(root, dry_run=dry_run))
    checks.append(_check_synthesis_prompt())

    for host_id, loader in loaders.items():
        try:
            integration = loader(root)
        except Exception as exc:
            checks.append(
                DoctorCheck(
                    name=f"host:{host_id}",
                    description=f"host {host_id}",
                    status="error",
                    detail=f"loader failed: {exc!r}",
                )
            )
            continue
        force = host_id in extra_set
        checks.append(_check_host(host_id, integration, scope=scope, dry_run=dry_run, force=force))

    return DoctorReport(checks=tuple(checks), dry_run=dry_run)
