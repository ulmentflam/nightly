"""Structured session telemetry — the queryable layer under `keepalive.log`.

`keepalive.log` records *decisions* in human-readable text. It answers
"what did the hook do" and nothing else. The bug reports that motivated
RFC 010 (#13 / #16 / #19 / #25) all needed the next question answered —
*why* was the session stopping? — and the text log cannot support it:

- How deep was the forced-continuation chain when the session died?
- Was the cascade making progress (varied picks) or stuck (one pick,
  over and over)?
- How many specialist dispatches actually finished cleanly?
- How many tool prompts got denied along the way?

This module is the answer: append-only JSONL, one file per signal class,
under `.nightly/runs/<id>/telemetry/`, plus a rolled-up `summary.json`
that `nightly status` and the morning briefing render.

Everything here is **best-effort**. Telemetry that crashes the Stop hook
would be worse than no telemetry at all, so every writer swallows
`OSError` and every reader degrades to empty. A run whose disk filled up
still keeps running; it just stops recording.

The on-disk files are the operator's own audit trail and are **never
redacted** — see `nightly_core.redaction` for the contract governing
what leaves the machine.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "EVENT_CLASSES",
    "EventKind",
    "TelemetryConfig",
    "build_summary",
    "load_telemetry_config",
    "read_events",
    "read_summary",
    "record_cascade_walk",
    "record_dispatch",
    "record_respawn",
    "record_stop_decision",
    "record_tool_denial",
    "telemetry_dir",
    "write_event",
]

_log = logging.getLogger(__name__)


EventKind = Literal["stop_decision", "tool_denial", "dispatch", "cascade", "respawn"]

EVENT_CLASSES: dict[EventKind, str] = {
    "stop_decision": "stop_reasons",
    "tool_denial": "tool_denials",
    "dispatch": "dispatch_events",
    "cascade": "cascade_walks",
    "respawn": "supervisor",
}
"""Event kind → JSONL filename stem under `<run>/telemetry/`.

Kept as an explicit map rather than deriving the filename from the kind
so the on-disk names can stay stable (and plural, and readable) while
the kind slugs stay singular in the event payload."""


_SUMMARY_FILENAME = "summary.json"

_STOP_REASON_CODES = (
    "force_continue",
    "host_cap",
    "inactive",
    "stop",
    "conclude",
    "no_run",
)
"""Stop-reason slugs pre-seeded into the summary so a reader can tell
"zero occurrences" from "this build doesn't emit that code". `host_cap`
is retired as a live decision path (v0.0.10) but stays in the seed: old
runs' telemetry still carries it, and a non-zero count on a fresh run
would be a genuine regression signal."""


# ── configuration ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TelemetryConfig:
    """The `telemetry:` block of `.nightly/config.yml`."""

    enabled: bool = True
    """Whether to write the structured JSONL files at all.

    Off is a real choice, not a debug flag: the files carry plan slugs and
    cascade summaries, and an operator on a shared machine may not want
    them at rest. Turning this off costs the briefing's telemetry section
    and makes `nightly bug --include-telemetry` a no-op; nothing else
    depends on it."""

    retention_days: int = 30
    """Prune telemetry from runs older than this on supervisor startup.

    Bounds unbounded growth without touching the runs themselves — only
    the `telemetry/` subfolder is removed, so the run's plans, briefing,
    and keepalive log survive as the permanent record."""


def load_telemetry_config(root: Path | None = None) -> TelemetryConfig:
    """Parse the `telemetry:` block from `<root>/.nightly/config.yml`.

    Missing / unreadable / malformed file yields all-defaults, matching
    every other loader in `nightly_core.config`. A non-positive
    `retention_days` degrades to the default rather than meaning "prune
    everything immediately" — an accidental `0` should not delete the
    operator's forensics.
    """
    from nightly_core.config import _coerce_bool, _load_block  # noqa: PLC0415

    defaults = TelemetryConfig()
    block = _load_block("telemetry", root)
    if block is None:
        return defaults

    enabled = _coerce_bool(block.get("enabled"), defaults.enabled)
    try:
        days = int(block.get("retention_days", defaults.retention_days))
    except (TypeError, ValueError):
        days = defaults.retention_days
    return TelemetryConfig(
        enabled=enabled,
        retention_days=days if days > 0 else defaults.retention_days,
    )


# ── writers ───────────────────────────────────────────────────────────────


def telemetry_dir(run_path: Path) -> Path:
    """Path to `<run>/telemetry/`. Does not create it."""
    return run_path / "telemetry"


def _redact_session_id(session_id: str | None) -> str:
    """Reduce a host session id to a short stable hash prefix.

    Session ids are host-generated UUIDs — not secret, but they correlate
    a Nightly run with the host's own transcript store, so we keep only
    enough to distinguish two sessions within one run. Done at *write*
    time rather than share time because there is no debugging value in
    the full id and no way to un-leak it once written.
    """
    if not session_id:
        return "?"
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]


def write_event(  # noqa: PLR0913 - the event envelope has this many fields
    run_path: Path,
    kind: EventKind,
    details: dict[str, Any],
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    now: datetime | None = None,
    enabled: bool = True,
) -> Path | None:
    """Append one event to `<run>/telemetry/<class>.jsonl`.

    Returns the file written, or None when telemetry is disabled or the
    write failed. Never raises — a caller in the Stop-hook path must not
    lose its turn because the disk is full.

    The summary is *not* recomputed here. Callers that want a fresh
    `summary.json` call `build_summary` explicitly; the record_* helpers
    below do it for them.
    """
    if not enabled:
        return None
    stem = EVENT_CLASSES.get(kind)
    if stem is None:  # unknown kind — drop rather than invent a filename
        _log.debug("ignoring telemetry event of unknown kind %r", kind)
        return None

    event = {
        "ts": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind,
        "session_id": _redact_session_id(session_id),
        "run_id": run_id or run_path.name,
        "details": details,
    }
    directory = telemetry_dir(run_path)
    path = directory / f"{stem}.jsonl"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError as exc:
        _log.debug("telemetry write failed for %s: %s", path, exc)
        return None
    return path


def record_stop_decision(  # noqa: PLR0913 - one keyword per recorded signal
    run_path: Path,
    *,
    reason_code: str,
    turn: int | None = None,
    block_count: int | None = None,
    context_tokens: int | None = None,
    cascade_source: str | None = None,
    cascade_repeats: int | None = None,
    session_id: str | None = None,
    now: datetime | None = None,
    enabled: bool = True,
) -> Path | None:
    """Record one Stop-hook decision.

    `block_count` is the forced-continuation chain depth — the field the
    bug reports most needed and the text log never carried. A session
    that died at chain depth 8 hit Claude Code's without-progress cap; one
    that died at depth 2 died of something else entirely, and the
    distinction decides whether raising `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`
    would have helped.
    """
    details: dict[str, Any] = {"reason_code": reason_code}
    if turn is not None:
        details["turn"] = turn
    if block_count is not None:
        details["block_count"] = block_count
    if context_tokens is not None:
        details["context_tokens"] = context_tokens
    if cascade_source is not None:
        details["cascade_source"] = cascade_source
    if cascade_repeats is not None:
        details["cascade_repeats"] = cascade_repeats
    return write_event(
        run_path,
        "stop_decision",
        details,
        session_id=session_id,
        now=now,
        enabled=enabled,
    )


def record_tool_denial(  # noqa: PLR0913 - one keyword per recorded signal
    run_path: Path,
    *,
    tool_name: str,
    reason: str | None = None,
    session_id: str | None = None,
    now: datetime | None = None,
    enabled: bool = True,
) -> Path | None:
    """Record a refused tool prompt (refusal policy or host permission)."""
    details: dict[str, Any] = {"tool_name": tool_name}
    if reason:
        details["reason"] = reason
    return write_event(
        run_path,
        "tool_denial",
        details,
        session_id=session_id,
        now=now,
        enabled=enabled,
    )


def record_dispatch(  # noqa: PLR0913 - one keyword per recorded signal
    run_path: Path,
    *,
    event: Literal["spawned", "finished_ok", "finished_err"],
    role: str | None = None,
    slug: str | None = None,
    tier: str | None = None,
    duration_seconds: float | None = None,
    now: datetime | None = None,
    enabled: bool = True,
) -> Path | None:
    """Record a specialist dispatch lifecycle event."""
    details: dict[str, Any] = {"event": event}
    for key, value in (
        ("role", role),
        ("slug", slug),
        ("tier", tier),
    ):
        if value is not None:
            details[key] = value
    if duration_seconds is not None:
        details["duration_seconds"] = round(duration_seconds, 2)
    return write_event(run_path, "dispatch", details, now=now, enabled=enabled)


def record_cascade_walk(  # noqa: PLR0913 - one keyword per recorded signal
    run_path: Path,
    *,
    source: str,
    summary: str | None = None,
    repeats: int | None = None,
    now: datetime | None = None,
    enabled: bool = True,
) -> Path | None:
    """Record one `nightly next` cascade walk and what it picked.

    The distribution of `source` across a session is the single most
    diagnostic signal in the whole telemetry set: heavily skewed to one
    source means the session was holding, not working.
    """
    details: dict[str, Any] = {"source": source}
    if summary is not None:
        details["summary"] = summary
    if repeats is not None:
        details["repeats"] = repeats
    return write_event(run_path, "cascade", details, now=now, enabled=enabled)


def record_respawn(  # noqa: PLR0913 - one keyword per recorded signal
    run_path: Path,
    *,
    event: Literal["respawn", "abort", "backoff", "skipped"],
    attempt: int | None = None,
    backoff_seconds: int | None = None,
    detail: str | None = None,
    now: datetime | None = None,
    enabled: bool = True,
) -> Path | None:
    """Record a supervisor action against this run."""
    details: dict[str, Any] = {"event": event}
    if attempt is not None:
        details["attempt"] = attempt
    if backoff_seconds is not None:
        details["backoff_seconds"] = backoff_seconds
    if detail:
        details["detail"] = detail
    return write_event(run_path, "respawn", details, now=now, enabled=enabled)


# ── readers ───────────────────────────────────────────────────────────────


def read_events(
    run_path: Path,
    kind: EventKind,
    *,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read events of one class, oldest first.

    Malformed lines are skipped rather than raising — a partially-written
    final line (killed mid-append) must not make the whole file
    unreadable, which is exactly the situation telemetry exists to
    describe.
    """
    stem = EVENT_CLASSES.get(kind)
    if stem is None:
        return []
    path = telemetry_dir(run_path) / f"{stem}.jsonl"
    out: list[dict[str, Any]] = []
    for event in _iter_jsonl(path):
        if since is not None and not _at_or_after(event.get("ts"), since):
            continue
        out.append(event)
    return out


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each well-formed JSON object in `path`. Silent on any error."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        return


def _at_or_after(stamp: Any, cutoff: datetime) -> bool:
    """True when the ISO-8601 `stamp` is at or after `cutoff`.

    An unparseable timestamp is *included*: dropping events because their
    clock field is malformed would silently shrink a bug report at the
    exact moment something is clearly wrong.
    """
    if not isinstance(stamp, str):
        return True
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return True
    return parsed >= cutoff


def build_summary(run_path: Path, *, write: bool = True) -> dict[str, Any]:
    """Roll every event class for a run into the `summary.json` shape.

    Cheap by construction — runs are bounded (one overnight session) and
    the files are small. Recomputing from scratch on every event beats
    maintaining an incremental counter that can drift out of sync with
    the events it claims to summarize.
    """
    stop_events = read_events(run_path, "stop_decision")
    cascade_events = read_events(run_path, "cascade")
    dispatch_events = read_events(run_path, "dispatch")
    denial_events = read_events(run_path, "tool_denial")
    respawn_events = read_events(run_path, "respawn")

    stop_counts: Counter[str] = Counter()
    max_chain = 0
    chain_at_last_stop: int | None = None
    for event in stop_events:
        details = event.get("details") or {}
        code = str(details.get("reason_code", "?"))
        stop_counts[code] += 1
        blocks = details.get("block_count")
        if isinstance(blocks, int):
            max_chain = max(max_chain, blocks)
            chain_at_last_stop = blocks

    dispatch_counts: Counter[str] = Counter()
    for event in dispatch_events:
        details = event.get("details") or {}
        dispatch_counts[str(details.get("event", "?"))] += 1

    pick_counts: Counter[str] = Counter()
    for event in cascade_events:
        details = event.get("details") or {}
        pick_counts[str(details.get("source", "?"))] += 1

    respawn_count = 0
    last_respawn: str | None = None
    aborted = False
    for event in respawn_events:
        details = event.get("details") or {}
        kind = details.get("event")
        if kind == "respawn":
            respawn_count += 1
            ts = event.get("ts")
            if isinstance(ts, str):
                last_respawn = ts
        elif kind == "abort":
            aborted = True

    summary: dict[str, Any] = {
        "stop_reasons": {code: stop_counts.get(code, 0) for code in _STOP_REASON_CODES}
        | {k: v for k, v in stop_counts.items() if k not in _STOP_REASON_CODES},
        "max_consecutive_blocks": max_chain,
        "consecutive_blocks_at_last_stop": chain_at_last_stop,
        "tool_denials_total": len(denial_events),
        "dispatch_events": {
            "spawned": dispatch_counts.get("spawned", 0),
            "finished_ok": dispatch_counts.get("finished_ok", 0),
            "finished_err": dispatch_counts.get("finished_err", 0),
        },
        "cascade_walks": len(cascade_events),
        "cascade_pick_distribution": dict(sorted(pick_counts.items())),
        "respawns": {
            "count": respawn_count,
            "last_at": last_respawn,
            "aborted": aborted,
        },
    }

    if write:
        path = telemetry_dir(run_path) / _SUMMARY_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            _log.debug("summary write failed for %s: %s", path, exc)
    return summary


def read_summary(run_path: Path) -> dict[str, Any] | None:
    """Read `<run>/telemetry/summary.json`, or None when absent/unreadable."""
    path = telemetry_dir(run_path) / _SUMMARY_FILENAME
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def prune_telemetry(runs_root: Path, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete `telemetry/` folders from runs older than `retention_days`.

    Returns the number of run folders pruned. Only the `telemetry/`
    subfolder is removed — the run's plans, briefing, and `keepalive.log`
    are the permanent record and are never touched by retention.

    Age comes from the run directory's mtime rather than parsing its
    ISO-8601 name: a run id that fails to parse would otherwise be
    immortal, and mtime is the more conservative signal anyway (a run
    touched recently is recent).
    """
    if retention_days <= 0 or not runs_root.is_dir():
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    pruned = 0
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        directory = telemetry_dir(entry)
        if not directory.is_dir():
            continue
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        import shutil  # noqa: PLC0415

        with contextlib.suppress(OSError):
            shutil.rmtree(directory)
            pruned += 1
    return pruned
