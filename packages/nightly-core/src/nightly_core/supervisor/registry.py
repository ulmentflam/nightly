"""Which repos the supervisor watches.

One daemon per machine, not per repo — an operator with three Nightly
repos should not run three daemons. The daemon needs a list of repos to
poll, and that list has to survive the daemon restarting, so it lives on
disk under `~/.cache/nightly/supervisor/registry.json`.

`nightly start` appends the repo it just started a run in. Entries age
out after 30 days so a repo the operator abandoned stops being polled
without anyone having to remember to deregister it.

All operations are best-effort and concurrency-tolerant: two `nightly
start` calls racing, or a daemon reading mid-write, must degrade to a
stale-but-valid list rather than a crash or a truncated file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = [
    "WatchedRepo",
    "forget_repo",
    "list_repos",
    "prune_repos",
    "register_repo",
    "registry_path",
    "supervisor_home",
]

_log = logging.getLogger(__name__)

_STALE_AFTER_DAYS = 30
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def supervisor_home() -> Path:
    """`~/.cache/nightly/supervisor/` — daemon state root.

    Honors `XDG_CACHE_HOME` so the whole tree relocates with the rest of
    the operator's cache. Deliberately outside any repo: the daemon
    outlives every individual checkout it watches.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "nightly" / "supervisor"


def registry_path() -> Path:
    return supervisor_home() / "registry.json"


@dataclass(frozen=True)
class WatchedRepo:
    path: Path
    last_seen: datetime

    def is_stale(self, *, now: datetime | None = None, days: int = _STALE_AFTER_DAYS) -> bool:
        return (now or datetime.now(UTC)) - self.last_seen > timedelta(days=days)


def _read_raw() -> list[dict[str, str]]:
    try:
        parsed = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, dict):
        return []
    entries = parsed.get("watched_repos")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str)]


def _write_raw(entries: list[dict[str, str]]) -> bool:
    """Atomically replace the registry. Returns False on any failure.

    Written to a temp file and renamed so a daemon reading concurrently
    sees either the old list or the new one, never a half-written file.
    """
    path = registry_path()
    tmp = path.with_suffix(".json.tmp")
    payload = {"watched_repos": entries}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        _log.debug("registry write failed: %s", exc)
        return False
    return True


def list_repos(*, now: datetime | None = None) -> list[WatchedRepo]:
    """Every registered repo, newest first. Unparseable entries are skipped."""
    del now
    out: list[WatchedRepo] = []
    for entry in _read_raw():
        try:
            seen = datetime.strptime(entry.get("last_seen", ""), _STAMP).replace(tzinfo=UTC)
        except ValueError:
            # No usable timestamp — treat as ancient so pruning collects it,
            # but keep it watchable in the meantime.
            seen = datetime.fromtimestamp(0, tz=UTC)
        out.append(WatchedRepo(path=Path(entry["path"]), last_seen=seen))
    out.sort(key=lambda r: r.last_seen, reverse=True)
    return out


def register_repo(root: Path, *, now: datetime | None = None) -> bool:
    """Record `root` as a repo worth watching, refreshing its timestamp.

    Idempotent: re-registering an existing repo updates `last_seen`
    rather than duplicating the entry, which is what keeps a daily-driven
    repo from ever aging out.
    """
    resolved = str(Path(root).resolve())
    stamp = (now or datetime.now(UTC)).strftime(_STAMP)
    entries = [e for e in _read_raw() if e.get("path") != resolved]
    entries.append({"path": resolved, "last_seen": stamp})
    return _write_raw(entries)


def forget_repo(root: Path) -> bool:
    """Drop `root` from the registry. True if it was there."""
    resolved = str(Path(root).resolve())
    entries = _read_raw()
    remaining = [e for e in entries if e.get("path") != resolved]
    if len(remaining) == len(entries):
        return False
    _write_raw(remaining)
    return True


def prune_repos(*, now: datetime | None = None, days: int = _STALE_AFTER_DAYS) -> int:
    """Drop entries not seen in `days`, plus any whose path is gone.

    Returns how many were removed. Called at daemon startup — a repo the
    operator deleted or stopped using should stop costing poll cycles
    without requiring an explicit deregister.
    """
    moment = now or datetime.now(UTC)
    keep: list[dict[str, str]] = []
    dropped = 0
    for repo in list_repos():
        if repo.is_stale(now=moment, days=days) or not repo.path.is_dir():
            dropped += 1
            continue
        keep.append({"path": str(repo.path), "last_seen": repo.last_seen.strftime(_STAMP)})
    if dropped:
        _write_raw(keep)
    return dropped
