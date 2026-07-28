"""`nightly doctor` — diagnose & repair an existing Nightly install.

A `nightly init` from a previous version, a half-cleaned `.nightly/`,
a manually-deleted SKILL.md, a renamed companion skill file — over a
few weeks every long-lived repo collects little drift between what the
running Nightly expects and what's actually on disk. `doctor` is the
boring, idempotent broom that walks the install surface and puts it
back together without the user having to remember the exact sequence
of `init` flags that produced their setup.

Checks fall into two groups. **Repairing** checks fix what they find:

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

**Advisory** checks report and never write, because the right fix is a
judgment call the operator owns:

5. Config schema drift — blocks an existing `config.yml` never learned,
   which default silently forever.
6. Model-tier bindings — hosts with no tier→model mapping, where
   routing is inert.
7. Tier/model agreement — a tier bound to a different band's model,
   which is silent and expensive.
8. Push readiness — Nightly work that cannot leave the machine
   (unpushed branches, a locked signing agent).
9. Worktree location — a repo under iCloud, where git state corrupts.

Adding a check means writing a `_check_*` helper **and** appending it in
`diagnose_and_repair`; a helper that exists but is never called runs zero
times and reports nothing. `test_every_check_helper_is_wired` pins that.

Design parallels `update.refresh_repo_install` — both walk host loaders
and call `install("project")` — but doctor's contract is broader: it
also reconciles the non-host scaffold (`.nightly/`, config, rules) and
is the right command to run after a manual edit that may have left
things half-broken. `update` is for "I pulled a new Nightly"; doctor
is for "make my install correct."
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

from nightly_core.config import DEFAULT_CONFIG_YML, load_git_config
from nightly_core.contract import MODEL_TIERS, HostId, InstallScope, NightlyHostIntegration
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


_DEFAULT_CONFIG_YML = DEFAULT_CONFIG_YML


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
        from nightly_core.config import load_compact_config  # noqa: PLC0415

        cfg = load_compact_config(root)
        compact_state = "enabled" if cfg.enabled else "disabled"
        return DoctorCheck(
            name="config",
            description=".nightly/config.yml",
            status="ok",
            detail=f"compact: {compact_state} (cap {round(cfg.context_token_cap / 1000)}K)",
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


def _check_config_blocks(root: Path) -> DoctorCheck:
    """Name the config blocks an existing `config.yml` has never heard of.

    `_check_config` writes the full template only when the file is
    *absent*. A repo initialized before a feature shipped therefore never
    learns that feature's knobs exist: every loader defaults gracefully,
    so nothing breaks and nothing complains. The operator's config quietly
    diverges from the schema for as long as the repo lives — this repo was
    missing eight blocks, half of them predating the current release.

    Advisory, and deliberately never repaired. `config.yml` is
    hand-edited and comment-rich; appending to it risks clobbering
    ordering or duplicating a key the operator deliberately removed. A
    wrong merge into the file that governs every other behavior is worse
    than a message telling them what to copy.
    """
    import yaml  # noqa: PLC0415 - lazy, doctor is not a hot path

    name, desc = "config_blocks", "config.yml schema drift"
    config = nightly_dir(root) / "config.yml"
    if not config.is_file():
        # `_check_config` owns the absent case and writes the template.
        return DoctorCheck(name=name, description=desc, status="skipped", detail="no config yet")

    try:
        present = yaml.safe_load(config.read_text(encoding="utf-8"))
        expected = yaml.safe_load(DEFAULT_CONFIG_YML)
    except (OSError, yaml.YAMLError):
        return DoctorCheck(
            name=name, description=desc, status="skipped", detail="config unreadable"
        )
    if not isinstance(present, dict) or not isinstance(expected, dict):
        return DoctorCheck(name=name, description=desc, status="skipped", detail="not a mapping")

    # Derived from the template itself, so this can never drift from the
    # schema the way a hand-maintained list would.
    missing = sorted(set(expected) - set(present))
    if not missing:
        return DoctorCheck(name=name, description=desc, status="ok", detail="all blocks present")
    return DoctorCheck(
        name=name,
        description=desc,
        status="warning",
        detail=(
            f"not configured (defaults apply): {', '.join(missing)} — "
            "see `.nightly/config.yml` in a freshly-initialized repo for the "
            "annotated blocks to copy"
        ),
    )


def _configured_hosts(root: Path) -> tuple[HostId, ...]:
    """Host ids listed under `hosts:` in `.nightly/config.yml`.

    Falls back to `("claude",)` — the default `nightly init` writes — when
    the file is missing, malformed, or lists nothing recognizable. Unknown
    ids are dropped silently here; `_check_host` is the surface that
    reports on host validity.
    """
    import yaml  # noqa: PLC0415 - lazy, doctor is not on a hot path

    try:
        data = yaml.safe_load((nightly_dir(root) / "config.yml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ("claude",)
    raw = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return ("claude",)
    known = set(get_args(HostId))
    hosts = tuple(h for h in (str(x).strip() for x in raw) if h in known)
    return cast("tuple[HostId, ...]", hosts) or ("claude",)


def _check_model_tiers(root: Path) -> DoctorCheck:
    """Warn when the installed hosts have no `model_tiers:` binding — RFC 007.

    Advisory, never repaired. A config predating RFC 007 still works: every
    dispatch falls through to the host CLI's own default model, exactly as
    before. But it also means tier routing is silently inert, which is
    worth saying out loud rather than letting the operator assume their
    `lite` researcher is actually running on a lite model.
    """
    from nightly_core.config import load_model_tier_config  # noqa: PLC0415

    cfg = load_model_tier_config(root)
    if not cfg.enabled:
        return DoctorCheck(
            name="model_tiers",
            description="model-tier routing",
            status="skipped",
            detail="disabled via model_tiers.enabled: false",
        )

    hosts = _configured_hosts(root)
    unbound = sorted(h for h in hosts if not cfg.models.get(h))
    if not unbound:
        bound = ", ".join(
            f"{tier}={cfg.binding(host, tier).model}"
            for host in sorted(hosts)[:1]
            for tier in MODEL_TIERS
        )
        return DoctorCheck(
            name="model_tiers",
            description="model-tier routing",
            status="ok",
            detail=bound,
        )
    return DoctorCheck(
        name="model_tiers",
        description="model-tier routing",
        status="warning",
        detail=(
            f"no tier→model binding for: {', '.join(unbound)} "
            "(dispatches use the host CLI's default model)"
        ),
    )


def _git_out(root: Path, *args: str) -> str | None:
    """Run a read-only git command; None on any failure. Never raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _signing_is_broken(root: Path) -> bool:
    """True when commits are configured to be signed but signing will fail.

    Only the SSH-agent path is detectable locally and cheaply: if
    `gpg.format` is `ssh` and the agent holds no identities, every commit
    will fail. A 1Password or Secretive agent that has auto-locked is the
    common cause, and it fails the push too, since the same agent holds
    the auth key.
    """
    if (_git_out(root, "config", "--get", "commit.gpgsign") or "").strip() != "true":
        return False
    if (_git_out(root, "config", "--get", "gpg.format") or "").strip() != "ssh":
        return False
    try:
        proc = subprocess.run(
            ["ssh-add", "-l"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "no identities" in proc.stdout.lower()


def _branches_without_upstream(root: Path) -> list[str]:
    """Nightly branches that have never been pushed anywhere."""
    listing = _git_out(
        root, "for-each-ref", "--format=%(refname:short)|%(upstream)", "refs/heads/nightly/"
    )
    if not listing:
        return []
    out = []
    for line in (ln for ln in listing.splitlines() if ln.strip()):
        branch, _, upstream = line.partition("|")
        if not upstream.strip():
            out.append(branch)
    return out


def _check_tier_sanity(root: Path) -> DoctorCheck:
    """Warn when a tier is bound to a model from a different band.

    A swapped or typo'd binding — `lite: claude-opus-5`, or a reasoning
    tier left on a lite model — is silent and expensive. Routing keeps
    working, dispatches keep succeeding, and the only symptom is the
    bill (or, in the reverse direction, a reviewer that misses bugs).
    Nothing else in the system will ever complain.

    Deliberately *not* a check that the model id exists. The vocabulary a
    host CLI advertises in `--help` is a sample, not an enumeration —
    `claude --help` names four tokens while every production id Nightly
    ships as a default is absent from that list. A membership test would
    flag correct configuration as broken, which is worse than no check.
    Family matching only, and an unrecognized family is skipped rather
    than guessed at.
    """
    from nightly_core.config import load_model_tier_config  # noqa: PLC0415
    from nightly_core.model_probe import tier_of_model  # noqa: PLC0415

    name, desc = "model_tier_sanity", "tier/model agreement"
    cfg = load_model_tier_config(root)
    if not cfg.enabled:
        return DoctorCheck(name=name, description=desc, status="skipped", detail="routing disabled")

    mismatches: list[str] = []
    for host in sorted(_configured_hosts(root)):
        for tier in MODEL_TIERS:
            model = cfg.binding(host, tier).model
            if not model:
                continue
            actual = tier_of_model(model)
            if actual is not None and actual != tier:
                mismatches.append(f"{host}.{tier}={model} looks like a {actual}-tier model")

    if not mismatches:
        return DoctorCheck(name=name, description=desc, status="ok", detail="tiers look consistent")
    return DoctorCheck(name=name, description=desc, status="warning", detail="; ".join(mismatches))


def _check_push_readiness(root: Path) -> DoctorCheck:
    """Can this machine's Nightly work actually reach the remote?

    Advisory, never repaired — pushing is the operator's call, and a
    locked signing agent is theirs to unlock.

    This check exists because the failure is silent and expensive: an
    overnight run can complete real work, commit it, and be unable to
    push, and nothing in `status` or `doctor` said so. The work looks
    done from inside the session and is invisible from outside it. An
    operator who reads a morning briefing without noticing has lost the
    night.
    """
    name, desc = "push_readiness", "unpushed Nightly work"

    listing = _git_out(
        root, "for-each-ref", "--format=%(refname:short)|%(upstream:track)", "refs/heads/nightly/"
    )
    if listing is None:
        return DoctorCheck(name=name, description=desc, status="skipped", detail="git unavailable")

    ahead: list[str] = []
    for line in (ln for ln in listing.splitlines() if ln.strip()):
        branch, _, track = line.partition("|")
        track = track.strip()
        # `[gone]` upstreams are merged-and-deleted branches — local
        # cruft, not lost work. An empty track means in sync.
        if "ahead" in track:
            ahead.append(f"{branch} {track}")

    never_pushed = _branches_without_upstream(root)

    signer_broken = _signing_is_broken(root)
    problems: list[str] = []
    if ahead:
        problems.append(f"unpushed: {', '.join(ahead)}")
    if never_pushed:
        problems.append(f"never pushed: {', '.join(never_pushed)}")
    if signer_broken:
        problems.append("commit signing configured but the ssh agent holds no identities")

    if not problems:
        return DoctorCheck(name=name, description=desc, status="ok", detail="all branches pushed")
    return DoctorCheck(
        name=name,
        description=desc,
        status="warning",
        detail="; ".join(problems),
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


_REQUIRED_SKILL_TOKENS: tuple[tuple[str, str, tuple[str, ...] | None], ...] = (
    ("seed-rfc", "seed-rfc toolkit row (RFC 005)", None),
    ("/compact", "session compaction boundary trigger (RFC 006)", ("claude",)),
    ("context_token_cap", "session compaction threshold trigger (RFC 006)", ("claude",)),
)
"""Substring tokens the main SKILL.md must contain.

When the installed file exists but is missing a token, the doctor
marks the host as needing repair so re-running `integration.install`
refreshes the file from the package source. Catches the failure
mode where a user upgraded the binary but their installed SKILL.md
is still the previous version — the file-presence check wouldn't
notice. Each tuple is `(token, human_label, allowed_hosts)` where
allowed_hosts is a tuple of host IDs or None if checked on all hosts."""


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
        for entry in _REQUIRED_SKILL_TOKENS:
            token, label, allowed_hosts = entry
            if allowed_hosts is not None:
                host_id = getattr(integration, "host_id", getattr(integration, "_name", None))
                if host_id not in allowed_hosts:
                    continue
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
    checks.append(_check_config_blocks(root))
    checks.append(_check_model_tiers(root))
    checks.append(_check_tier_sanity(root))
    checks.append(_check_push_readiness(root))
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
