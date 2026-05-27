"""Check whether a newer Nightly release exists; recommend an upgrade.

Inspired by [open-gsd/get-shit-done-redux](https://github.com/open-gsd/get-shit-done-redux)'s
idempotent-installer pattern, plus the long-standing CLI convention
of surfacing a one-line update notice at session start (cargo,
gh, brew, etc).

Called at session start by every host's SKILL.md: after arming the
keep-alive, the agent runs `nightly check-update`. If the command
prints a recommendation, the agent surfaces it to the operator at
the top of its first response. Otherwise the command is silent.

Design constraints:

- **Non-blocking.** A failed check (no network, gh missing,
  malformed response) returns None and the CLI exits 0 silent. The
  agent can't crash on update detection.
- **24h TTL cache.** Operators with persistent sessions hit the
  GitHub API at most once per day per machine. The cache lives at
  `~/.cache/nightly/update-check.json` — disposable, safe to delete.
- **Install-method aware.** A git install (install.sh) gets
  `/nightly-update` recommended; a Homebrew install gets
  `brew upgrade nightly`; a dev clone is silenced (developers pull
  manually). Unknown installs get a docs link.
- **No PyPI check.** Nightly hasn't published to PyPI yet; until it
  does, GitHub releases are the canonical source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from nightly_core._version import __version__
from nightly_core.update import detect_install_method

__all__ = [
    "CACHE_PATH",
    "CACHE_TTL_SECONDS",
    "GITHUB_REPO",
    "InstallChannel",
    "UpdateCheckResult",
    "check_for_update",
    "detect_install_channel",
]


CACHE_PATH = Path.home() / ".cache" / "nightly" / "update-check.json"
"""Where the last check result lives. Disposable — `rm` clears it."""

CACHE_TTL_SECONDS = 24 * 60 * 60
"""24-hour cache TTL. Operators with long sessions hit GitHub once a day."""

GITHUB_REPO = "ulmentflam/nightly"
"""The repo whose releases we check. Forks can override via
`NIGHTLY_RELEASE_REPO` env var if they want their own check stream."""

_FETCH_TIMEOUT_SECONDS = 10
"""Per-network-call timeout. Conservative — operators shouldn't
notice the check at session start even on a flaky link."""

InstallChannel = Literal["git", "homebrew", "dev", "unknown"]


@dataclass(frozen=True)
class UpdateCheckResult:
    """Outcome of one update check.

    `latest is None` means the network probe failed (no `gh`, no
    network, API error). Callers treat None the same as "up to date" —
    silent. `is_outdated` is False in that case.
    """

    current: str
    latest: str | None
    channel: InstallChannel
    fetched_at: datetime

    @property
    def is_outdated(self) -> bool:
        if self.latest is None:
            return False
        return _normalize_version(self.current) != _normalize_version(self.latest)

    def recommendation(self) -> str | None:
        """One-line "type X to upgrade" message; None if up to date.

        The verb is install-method aware so an operator on Homebrew
        doesn't see a misleading "type /nightly-update" suggestion.
        """
        if not self.is_outdated:
            return None
        verbs: dict[InstallChannel, str] = {
            "git": "type `/nightly-update` (or run `nightly update`) to upgrade",
            "homebrew": "run `brew upgrade nightly` to upgrade",
            "dev": "git pull in your Nightly clone to upgrade",
            "unknown": "see https://github.com/ulmentflam/nightly#install",
        }
        return (
            f"Nightly upgrade available: {self.current} → "
            f"{_normalize_version(self.latest or '?')}. "
            f"{verbs[self.channel]}."
        )


# ── install-channel detection ────────────────────────────────────────────


def detect_install_channel() -> InstallChannel:
    """Distinguish git (install.sh), homebrew, dev clone, and unknown.

    - `homebrew`: this module's path includes a `Cellar/` segment, the
      universal marker for a Homebrew-installed package.
    - `git`: `detect_install_method().is_git` is True AND the source
      root is the install.sh default (`~/.local/share/nightly`).
    - `dev`: `is_git` is True but the source is somewhere else — a
      developer's workspace clone. We silence the update notice for
      these; devs pull manually.
    - `unknown`: anything else (PyPI/pipx/wheel install in the future).
    """
    here = Path(__file__).resolve()
    if any(part == "Cellar" for part in here.parts):
        return "homebrew"

    method = detect_install_method()
    if method.is_git and method.root is not None:
        default_home = (Path.home() / ".local" / "share" / "nightly").resolve()
        if method.root.resolve() == default_home:
            return "git"
        return "dev"
    return "unknown"


# ── the check itself ─────────────────────────────────────────────────────


_Fetcher = Callable[[], "str | None"]
"""Injection point for tests — stand in for `_fetch_latest_tag`."""


def check_for_update(
    *,
    force: bool = False,
    now: datetime | None = None,
    fetcher: _Fetcher | None = None,
) -> UpdateCheckResult | None:
    """Return the latest version vs current, cached for 24h.

    `force=True` bypasses the cache and refetches.
    `now` is injectable for tests.
    `fetcher` is injectable for tests — overrides the real network call.

    Returns None for dev installs (no nag) and when the check is
    fully suppressed. Returns an `UpdateCheckResult` with
    `latest=None` when the network probe failed (so the caller can
    still record the attempt for cache-TTL purposes).
    """
    moment = now or datetime.now(UTC)
    channel = detect_install_channel()

    # Developers working on Nightly itself shouldn't see update nags.
    # Their workflow is `git pull` not `/nightly-update`.
    if channel == "dev":
        return None

    cached = _read_cache(moment) if not force else None
    if cached is not None:
        return cached

    fn = fetcher or _fetch_latest_tag
    latest = fn()
    result = UpdateCheckResult(
        current=__version__,
        latest=latest,
        channel=channel,
        fetched_at=moment,
    )
    _write_cache(result)
    return result


# ── cache I/O ────────────────────────────────────────────────────────────


def _read_cache(now: datetime) -> UpdateCheckResult | None:
    """Return a cached result if it's within TTL and not stale; else None.

    Tolerates malformed cache files (parse error → ignore + refetch)
    so a corrupted cache never crashes the agent. The cache is a
    pure performance artifact; correctness lives in the API call.
    """
    if not CACHE_PATH.is_file():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        channel = data["channel"]
        current = data["current"]
        latest = data.get("latest")
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None
    if now - fetched_at > timedelta(seconds=CACHE_TTL_SECONDS):
        return None
    if channel not in {"git", "homebrew", "dev", "unknown"}:
        return None
    # If the version baked into the cache no longer matches the
    # running binary, the cache is stale by definition (an upgrade
    # already happened). Refetch.
    if current != __version__:
        return None
    return UpdateCheckResult(
        current=current,
        latest=latest,
        channel=channel,  # type: ignore[arg-type]
        fetched_at=fetched_at,
    )


def _write_cache(result: UpdateCheckResult) -> None:
    """Persist the result to disk. Best-effort — a failure here only
    means the next call refetches a day too early, which is harmless."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(
                {
                    "current": result.current,
                    "latest": result.latest,
                    "channel": result.channel,
                    "fetched_at": result.fetched_at.isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        return


# ── network ──────────────────────────────────────────────────────────────


def _fetch_latest_tag() -> str | None:
    """Probe GitHub for the latest version. Returns a tag name or None.

    Strategy chain (each step fails silent → next):
    1. `gh api repos/<repo>/releases/latest` — uses operator's auth.
    2. `gh api repos/<repo>/tags` (first entry) — tags-only fallback
       for repos that ship git tags but no published Releases.
    3. Anonymous urllib hit on `releases/latest`.
    4. Anonymous urllib hit on `tags`.

    The tags fallback exists because GitHub's `releases/latest`
    endpoint returns 404 when a repo has git tags but no Releases
    backing them. Forks/mirrors commonly ship tags-only; without
    this fallback they'd silently never see update notices. Tags
    are returned by the API in commit-date order so the first
    entry is the newest tag — but we apply a stricter v-prefix
    filter (the proposer wants release-shaped tags, not arbitrary
    branches mirrored to refs/tags/*).
    """
    if shutil.which("gh") is not None:
        # Try releases/latest first.
        tag = _gh_api(f"repos/{GITHUB_REPO}/releases/latest", jq=".tag_name")
        if tag:
            return tag
        # Then tags fallback — first release-shaped entry wins.
        tags_raw = _gh_api(f"repos/{GITHUB_REPO}/tags", jq=".[].name")
        if tags_raw:
            for line in tags_raw.splitlines():
                candidate = line.strip()
                if candidate.startswith("v") and len(candidate) > 1:
                    return candidate

    # No gh, or gh failed both calls — try anonymous urllib paths.
    latest = _urllib_get_json(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
    if isinstance(latest, dict):
        tag = latest.get("tag_name")
        if isinstance(tag, str) and tag:
            return tag

    tags = _urllib_get_json(f"https://api.github.com/repos/{GITHUB_REPO}/tags")
    if isinstance(tags, list):
        for entry in tags:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name.startswith("v") and len(name) > 1:
                return name

    return None


def _gh_api(path: str, *, jq: str | None = None) -> str | None:
    """Invoke `gh api <path>` (optionally with --jq), return stdout or None.

    Wraps `subprocess.run` with a uniform exception handler so the
    fetcher chain stays readable.
    """
    argv = ["gh", "api", path]
    if jq is not None:
        argv += ["--jq", jq]
    try:
        result = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def _urllib_get_json(url: str) -> object | None:
    """GET `url`, parse the body as JSON, return the parsed object or None.

    Best-effort: any network or parse error returns None. Used as the
    anonymous-HTTP arm of the fetcher chain.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _normalize_version(v: str) -> str:
    """Strip leading `v` and surrounding whitespace.

    `v0.0.1` and `0.0.1` compare equal. We deliberately don't do
    full semver parsing — release tags follow `v<semver>` and
    `pyproject.toml` carries `<semver>` without the prefix; a
    leading-`v` strip handles the only routine difference. If we
    ever ship pre-release tags (`v0.1.0rc1`) the comparison stays
    string-based and "any difference → outdated" is the right
    default.
    """
    return v.strip().lstrip("v")
