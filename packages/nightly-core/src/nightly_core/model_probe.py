"""Discover each host CLI's model-selection control at `nightly init` time.

RFC 007 routes a dispatch to a model id. Actually *applying* that id means
knowing the flag the host's headless CLI accepts — and that is exactly the
kind of fact Nightly should not hardcode. Vendors rename flags, and a
wrong flag is a hard spawn failure in the middle of the night, whereas the
right one is discoverable in a fraction of a second from the CLI itself.

So: probe. Run the host binary's `--help`, look for a model-selection
option, and record what we find. `nightly init` and `nightly doctor` both
run this and write the result into `model_tiers.<host>.flag`, so the
config carries a discovered fact rather than a maintained guess.

The probe is deliberately conservative. It reports only what it can read
out of the CLI's own help text; a host whose help exposes no recognizable
model option yields `None`, and dispatch falls through to that host's
default model with the tier still applied via the prompt-side effort
directive. Never guess a flag.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import get_args

from nightly_core.contract import MODEL_TIERS, HostId, ModelTier

__all__ = [
    "HARNESS_ENV_MARKERS",
    "HELP_INVOCATIONS",
    "TIER_FAMILIES",
    "ModelControl",
    "assign_tiers",
    "detect_harness",
    "discover_tier_bindings",
    "is_pinned",
    "merge_discovered_tiers",
    "probe_all",
    "probe_model_control",
    "tier_of_model",
]


HARNESS_ENV_MARKERS: dict[HostId, tuple[str, ...]] = {
    "claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    "codex": ("CODEX_HOME", "CODEX_SANDBOX"),
    "cursor": ("CURSOR_TRACE_ID", "CURSOR_AGENT"),
    "gemini": ("GEMINI_CLI",),
    "opencode": ("OPENCODE", "OPENCODE_BIN"),
    "antigravity": ("ANTIGRAVITY_AGENT",),
    "pi": ("PI_SESSION", "PI_AGENT"),
    "hermes": ("HERMES_SESSION", "HERMES_AGENT"),
}
"""Environment variables that identify the harness running `nightly init`.

Presence of any one marks that host as the initializing harness. Only the
Claude Code markers are verified first-hand; the rest are best-effort and
cost nothing when wrong — an unmatched harness simply falls back to
probing every installed host rather than leading with one."""


TIER_FAMILIES: dict[ModelTier, tuple[str, ...]] = {
    "lite": ("haiku", "mini", "flash", "small"),
    "coding": ("sonnet", "coder", "pro"),
    "reasoning": ("opus", "fable", "mythos", "reasoning", "ultra"),
}
"""Model-family substrings that map a discovered model id onto a tier.

Ordered by preference within each tier: the first family that matches a
discovered id wins. `reasoning` leads with `opus` rather than the
higher-capability `fable`/`mythos` deliberately — the reasoning tier is
the *judgment* tier, not the most-expensive-available tier, and Opus is
the intended default for it. An operator who wants the ceiling edits one
line in `.nightly/config.yml`."""


HELP_INVOCATIONS: dict[HostId, tuple[tuple[str, ...], ...]] = {
    # Most hosts put the model flag on the top-level help; the ones whose
    # headless entry point is a subcommand hide it there instead, so try
    # the subcommand first and fall back to the root.
    "claude": (("--help",),),
    "codex": (("exec", "--help"), ("--help",)),
    "opencode": (("run", "--help"), ("--help",)),
    "gemini": (("--help",),),
    "cursor": (("agent", "--help"), ("--help",)),
    "antigravity": (("--help",),),
    "pi": (("run", "--help"), ("--help",)),
    "hermes": (("run", "--help"), ("--help",)),
}
"""Per-host argv suffixes to try when reading help text, in order."""


# Matches an option line like:
#   --model <model>    Model for the current session
#   -m, --model MODEL  Model to use
# Anchored at the start of an option so prose mentioning "--model" in a
# description doesn't produce a false positive.
_MODEL_OPTION = re.compile(
    r"^\s*(?:(-[A-Za-z])\s*,\s*)?(--model(?:[-_][a-z]+)?)\b",
    re.MULTILINE,
)

_HELP_TIMEOUT_S = 5.0

# Probing shells out once per installed host, which is cheap in absolute
# terms but not free — and `nightly init` / `doctor` can run many times in
# one process (notably across a test session). The result is a property of
# the machine, not of the repo, so memoize the default probe for the life
# of the process. Callers that pass explicit hosts/runner/which bypass the
# cache entirely, which is what keeps tests deterministic.
_DEFAULT_DISCOVERY: tuple[dict[HostId, dict[ModelTier, str]], dict[HostId, str]] | None = None


@dataclass(frozen=True)
class ModelControl:
    """What a host CLI accepts for selecting a model."""

    host: HostId
    binary: str
    """Absolute path to the resolved binary."""

    flag: str | None
    """The discovered model-selection flag (e.g. `--model`), or None when
    the CLI's help exposes no recognizable model option. None means
    "dispatch on this host uses its default model"."""

    short_flag: str | None = None
    """Short alias (e.g. `-m`) when the help lists one. Recorded for
    diagnostics; `build_argv` always emits the long form."""

    probed_via: tuple[str, ...] = ()
    """The argv suffix whose help text produced the match — useful when a
    flag turns out to live on a subcommand rather than the root."""

    @property
    def supported(self) -> bool:
        return self.flag is not None


def _run_help(argv: Sequence[str]) -> str:
    """Return combined stdout+stderr of a help invocation, or '' on failure.

    Help output legitimately lands on either stream depending on the CLI,
    and a non-zero exit is common for `--help` on some parsers — so the
    exit code is ignored and only the text matters.
    """
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=_HELP_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{proc.stdout}\n{proc.stderr}"


def probe_model_control(
    host: HostId,
    *,
    runner: Callable[[Sequence[str]], str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> ModelControl | None:
    """Discover `host`'s model-selection flag, or None if it isn't installed.

    Returns None when the binary is absent or the host has no headless
    CLI at all — "not installed" and "installed but no model flag" are
    different answers, and only the latter is a `ModelControl` with
    `flag=None`.

    `runner` and `which` are injectable for tests; production leaves both
    unset.
    """
    invocations = HELP_INVOCATIONS.get(host)
    if not invocations:
        return None

    resolve = which or shutil.which
    binary = resolve(host)
    if binary is None:
        return None

    run = runner or _run_help
    for suffix in invocations:
        match = _MODEL_OPTION.search(run([binary, *suffix]))
        if match:
            return ModelControl(
                host=host,
                binary=binary,
                flag=match.group(2),
                short_flag=match.group(1),
                probed_via=suffix,
            )
    return ModelControl(host=host, binary=binary, flag=None)


def probe_all(
    hosts: Sequence[HostId] | None = None,
    *,
    runner: Callable[[Sequence[str]], str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[HostId, ModelControl]:
    """Probe every host in `hosts` (default: all of them).

    Uninstalled hosts are omitted from the result entirely, so callers can
    treat membership as "this host is present and was inspected".
    """
    targets = hosts if hosts is not None else get_args(HostId)
    found: dict[HostId, ModelControl] = {}
    for host in targets:
        control = probe_model_control(host, runner=runner, which=which)
        if control is not None:
            found[host] = control
    return found


# ── model vocabulary + tier assignment ───────────────────────────────────

# Model ids and aliases quoted in a CLI's help text, e.g.
#   "Provide an alias for the latest model (e.g. 'fable', 'opus', or
#    'sonnet') or a model's full name (e.g. 'claude-fable-5')."
# Both straight and typographic quotes appear in the wild.
# Quote class covers straight and typographic quotes (\u2018\u2019\u201c\u201d), which both
# appear in vendor help text. Written as escapes to keep the source ASCII.
_QUOTES = "'\"\u2018\u2019\u201c\u201d"
_QUOTED_TOKEN = re.compile(rf"[{_QUOTES}]([A-Za-z][A-Za-z0-9._-]{{1,60}})[{_QUOTES}]")

# Tokens that show up quoted in help text but are never model names.
_NOT_A_MODEL = frozenset(
    {
        "true",
        "false",
        "null",
        "none",
        "auto",
        "default",
        "json",
        "text",
        "stream-json",
        "yes",
        "no",
        "on",
        "off",
    }
)


def probe_models(
    host: HostId,
    *,
    runner: Callable[[Sequence[str]], str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Model ids and aliases this host's CLI advertises in its own help.

    Reads the vocabulary out of the harness rather than carrying a table
    that goes stale the day a vendor ships a new model. Returns [] when
    the host is absent or its help enumerates nothing recognizable —
    callers fall back to the seeded defaults in that case.

    Order is preserved as the help text lists it, deduped, so a caller
    that wants "the first plausible option" gets the CLI's own ordering.
    """
    invocations = HELP_INVOCATIONS.get(host)
    if not invocations:
        return []
    resolve = which or shutil.which
    binary = resolve(host)
    if binary is None:
        return []

    run = runner or _run_help
    seen: dict[str, None] = {}
    for suffix in invocations:
        text = run([binary, *suffix])
        match = _MODEL_OPTION.search(text)
        if not match:
            continue
        for token in _models_from_help(text, match.start()):
            seen.setdefault(token, None)
        if seen:
            break
    return list(seen)


def _models_from_help(text: str, option_start: int) -> list[str]:
    """Quoted model tokens inside the model option's own help paragraph.

    Scoped to a window after the option so quoted tokens belonging to
    unrelated flags elsewhere on a long help page don't leak in.
    """
    window = text[option_start : option_start + 600]
    return [tok for tok in _QUOTED_TOKEN.findall(window) if tok.lower() not in _NOT_A_MODEL]


def tier_of_model(model: str) -> ModelTier | None:
    """Which tier a model id belongs to, by family substring.

    None for an id whose family isn't recognized — the honest answer, and
    the one that keeps callers from guessing. Consumers treat None as
    "no opinion" rather than "wrong".
    """
    lowered = model.lower()
    for tier in MODEL_TIERS:
        if any(family in lowered for family in TIER_FAMILIES[tier]):
            return tier
    return None


def assign_tiers(models: Sequence[str]) -> dict[ModelTier, str]:
    """Sort discovered model ids into the three tiers.

    Two preferences, applied in order:

    1. **Family preference.** Within a tier, the family listed first in
       `TIER_FAMILIES` wins — so `opus` takes the reasoning slot over
       `fable` even when the harness offers both.
    2. **Fully-qualified over alias.** Given `opus` and `claude-opus-5`,
       the longer, version-pinned id wins. Aliases float to whatever the
       vendor most recently shipped, which is convenient interactively and
       exactly wrong for an unattended overnight run that should be
       reproducible.

    Tiers with no matching model are simply absent from the result; the
    caller merges over its own defaults.
    """
    best: dict[ModelTier, tuple[int, int, str]] = {}
    for model in models:
        tier = tier_of_model(model)
        if tier is None:
            continue
        lowered = model.lower()
        family_rank = next(
            (i for i, fam in enumerate(TIER_FAMILIES[tier]) if fam in lowered),
            len(TIER_FAMILIES[tier]),
        )
        # Longer id == more specific == preferred, so negate for min-ranking.
        candidate = (family_rank, -len(model), model)
        if tier not in best or candidate < best[tier]:
            best[tier] = candidate
    return {tier: value[2] for tier, value in best.items()}


def detect_harness(env: dict[str, str] | None = None) -> HostId | None:
    """The host whose TUI is running this process, from environment markers.

    `nightly init` leads with this so the config it writes reflects the
    harness the operator actually works in: init from Claude Code and the
    tiers are bound to Claude models; init from Codex and they are bound
    to whatever Codex advertises. Returns None when nothing matches, which
    is the ordinary case for a plain shell.
    """
    import os  # noqa: PLC0415 - lazy; keeps module import side-effect free

    environ = os.environ if env is None else env
    for host, markers in HARNESS_ENV_MARKERS.items():
        if any(environ.get(marker) for marker in markers):
            return host
    return None


def discover_tier_bindings(
    hosts: Sequence[HostId] | None = None,
    *,
    runner: Callable[[Sequence[str]], str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[dict[HostId, dict[ModelTier, str]], dict[HostId, str]]:
    """Probe installed hosts and return `(tier_models, model_flags)`.

    This is the whole `nightly init` discovery step in one call: for every
    host present on PATH, read its model vocabulary and its
    model-selection flag out of its own CLI, then rank the vocabulary into
    lite / coding / reasoning.

    Hosts that are absent, or whose help yields nothing usable, are simply
    missing from the returned mappings — the config writer merges these
    over the seeded defaults rather than replacing them, so discovery can
    only ever add certainty.
    """
    global _DEFAULT_DISCOVERY  # noqa: PLW0603 - process-lifetime memo of a machine fact

    uncached = hosts is None and runner is None and which is None
    if uncached and _DEFAULT_DISCOVERY is not None:
        return _DEFAULT_DISCOVERY

    targets = list(hosts) if hosts is not None else list(get_args(HostId))
    # Lead with the initializing harness so its findings are the ones an
    # operator sees first in the init output.
    harness = detect_harness()
    if harness is not None and harness in targets:
        targets.remove(harness)
        targets.insert(0, harness)

    resolve = which or shutil.which
    run = runner or _run_help

    tier_models: dict[HostId, dict[ModelTier, str]] = {}
    model_flags: dict[HostId, str] = {}
    for host in targets:
        invocations = HELP_INVOCATIONS.get(host)
        binary = resolve(host) if invocations else None
        if binary is None:
            continue
        # One help read per invocation yields both facts — the flag and
        # the vocabulary come out of the same text, so reading twice would
        # double the subprocess cost for nothing.
        for suffix in invocations or ():
            text = run([binary, *suffix])
            match = _MODEL_OPTION.search(text)
            if not match:
                continue
            model_flags[host] = match.group(2)
            tiers = assign_tiers(_models_from_help(text, match.start()))
            if tiers:
                tier_models[host] = tiers
            break
    if uncached:
        _DEFAULT_DISCOVERY = (tier_models, model_flags)
    return tier_models, model_flags


# A pinned id carries a version/family suffix (`claude-opus-5`,
# `gpt-5.6`); a bare alias (`opus`, `sonnet`) does not.
_PINNED = re.compile(r"[A-Za-z]+[-.][A-Za-z0-9]")


def is_pinned(model: str) -> bool:
    """True when `model` names a specific version rather than a floating alias.

    Aliases resolve to whatever the vendor most recently shipped. That is
    convenient interactively and wrong for an unattended overnight run,
    where the model a task ran on should still be knowable in the morning.
    """
    return bool(_PINNED.search(model))


def merge_discovered_tiers(
    seeded: dict[HostId, dict[ModelTier, str]],
    discovered: dict[HostId, dict[ModelTier, str]],
) -> dict[HostId, dict[ModelTier, str]]:
    """Overlay probe results onto seeded defaults, preferring pinned ids.

    Precedence per host+tier:

    1. A **pinned** discovered id wins — the harness told us exactly what
       it offers, versioned.
    2. Otherwise the seeded default stands, if there is one. A seeded
       `claude-opus-5` beats a discovered bare `opus` precisely because
       the alias floats.
    3. Otherwise the discovered alias is used — for a host Nightly ships
       no defaults for, a floating alias still beats no binding at all.
    """
    merged = {host: dict(tiers) for host, tiers in seeded.items()}
    for host, tiers in discovered.items():
        target = merged.setdefault(host, {})
        for tier, model in tiers.items():
            if is_pinned(model) or tier not in target:
                target[tier] = model
    return merged
