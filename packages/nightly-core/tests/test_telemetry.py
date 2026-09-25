"""RFC 010 §A1/A2/A9 — structured telemetry writers and the summary roll-up.

The load-bearing property under test is *resilience*. Telemetry runs
inside the Stop hook, so a writer that raises would cost the model its
turn — worse than having no telemetry at all. Most of these tests are
about what happens when things go wrong: unwritable directories, half-
written lines, unparseable timestamps, garbage config.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightly_core.telemetry import (
    EVENT_CLASSES,
    TelemetryConfig,
    build_summary,
    load_telemetry_config,
    prune_telemetry,
    read_events,
    read_summary,
    record_cascade_walk,
    record_dispatch,
    record_respawn,
    record_stop_decision,
    record_tool_denial,
    telemetry_dir,
    write_event,
)


@pytest.fixture
def run_path(tmp_path: Path) -> Path:
    path = tmp_path / "runs" / "2026-06-06T20-00-00Z"
    path.mkdir(parents=True)
    return path


# ── writers ───────────────────────────────────────────────────────────────


def test_write_event_creates_the_class_file(run_path: Path) -> None:
    written = write_event(run_path, "stop_decision", {"reason_code": "force_continue"})
    assert written is not None
    assert written == telemetry_dir(run_path) / "stop_reasons.jsonl"
    assert written.is_file()


def test_event_envelope_carries_the_required_fields(run_path: Path) -> None:
    write_event(run_path, "cascade", {"source": "accepted_rfc"}, session_id="abc-123")
    event = read_events(run_path, "cascade")[0]
    assert set(event) >= {"ts", "kind", "session_id", "run_id", "details"}
    assert event["kind"] == "cascade"
    assert event["run_id"] == run_path.name
    assert event["details"]["source"] == "accepted_rfc"


def test_session_id_is_hashed_not_stored_raw(run_path: Path) -> None:
    """No debugging value in the full id, and no way to un-leak it."""
    write_event(run_path, "cascade", {"source": "ideate"}, session_id="secret-session-uuid")
    event = read_events(run_path, "cascade")[0]
    assert event["session_id"] != "secret-session-uuid"
    assert len(event["session_id"]) == 8


def test_same_session_id_hashes_stably(run_path: Path) -> None:
    write_event(run_path, "cascade", {"source": "a"}, session_id="s1")
    write_event(run_path, "cascade", {"source": "b"}, session_id="s1")
    first, second = read_events(run_path, "cascade")
    assert first["session_id"] == second["session_id"]


def test_absent_session_id_becomes_a_placeholder(run_path: Path) -> None:
    write_event(run_path, "cascade", {"source": "a"})
    assert read_events(run_path, "cascade")[0]["session_id"] == "?"


def test_events_append_rather_than_overwrite(run_path: Path) -> None:
    for i in range(3):
        write_event(run_path, "cascade", {"source": f"s{i}"})
    assert len(read_events(run_path, "cascade")) == 3


def test_disabled_writes_nothing(run_path: Path) -> None:
    assert write_event(run_path, "cascade", {"source": "a"}, enabled=False) is None
    assert not telemetry_dir(run_path).exists()


def test_unknown_kind_is_dropped_not_invented(run_path: Path) -> None:
    """An unknown kind must not create an arbitrarily-named file."""
    assert write_event(run_path, "bogus", {"x": 1}) is None  # type: ignore[arg-type]
    assert not telemetry_dir(run_path).exists()


def test_write_failure_returns_none_rather_than_raising(tmp_path: Path) -> None:
    """The hook path must survive an unwritable disk."""
    run = tmp_path / "run"
    run.mkdir()
    # A file where the telemetry directory needs to be: mkdir will fail.
    (run / "telemetry").write_text("not a directory", encoding="utf-8")
    assert write_event(run, "cascade", {"source": "a"}) is None


def test_every_event_class_has_a_distinct_file(run_path: Path) -> None:
    record_stop_decision(run_path, reason_code="force_continue")
    record_tool_denial(run_path, tool_name="Bash")
    record_dispatch(run_path, event="spawned", role="implementer")
    record_cascade_walk(run_path, source="accepted_rfc")
    record_respawn(run_path, event="respawn", attempt=1)
    written = {p.name for p in telemetry_dir(run_path).glob("*.jsonl")}
    assert written == {f"{stem}.jsonl" for stem in EVENT_CLASSES.values()}


def test_record_stop_decision_captures_chain_depth(run_path: Path) -> None:
    """Chain depth is the field the bug reports needed and the text log
    never carried."""
    record_stop_decision(run_path, reason_code="force_continue", turn=12, block_count=7)
    details = read_events(run_path, "stop_decision")[0]["details"]
    assert details["block_count"] == 7
    assert details["turn"] == 12


def test_record_stop_decision_omits_absent_optionals(run_path: Path) -> None:
    record_stop_decision(run_path, reason_code="conclude")
    details = read_events(run_path, "stop_decision")[0]["details"]
    assert details == {"reason_code": "conclude"}


def test_record_dispatch_derives_duration(run_path: Path) -> None:
    record_dispatch(run_path, event="finished_ok", role="tester", duration_seconds=12.345)
    assert read_events(run_path, "dispatch")[0]["details"]["duration_seconds"] == 12.35


# ── readers ───────────────────────────────────────────────────────────────


def test_read_events_of_a_missing_file_is_empty(run_path: Path) -> None:
    assert read_events(run_path, "cascade") == []


def test_malformed_line_is_skipped_not_fatal(run_path: Path) -> None:
    """A line truncated by a kill is exactly the situation telemetry
    exists to describe — it must not make the file unreadable."""
    write_event(run_path, "cascade", {"source": "good"})
    path = telemetry_dir(run_path) / "cascade_walks.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-06-06T20:00:00Z", "kind": "cascade", "det\n')
    write_event(run_path, "cascade", {"source": "also-good"})

    events = read_events(run_path, "cascade")
    assert len(events) == 2
    assert [e["details"]["source"] for e in events] == ["good", "also-good"]


def test_since_filters_older_events(run_path: Path) -> None:
    old = datetime(2026, 6, 1, tzinfo=UTC)
    new = datetime(2026, 6, 10, tzinfo=UTC)
    write_event(run_path, "cascade", {"source": "old"}, now=old)
    write_event(run_path, "cascade", {"source": "new"}, now=new)

    recent = read_events(run_path, "cascade", since=datetime(2026, 6, 5, tzinfo=UTC))
    assert [e["details"]["source"] for e in recent] == ["new"]


def test_unparseable_timestamp_is_included_not_dropped(run_path: Path) -> None:
    """Dropping events because their clock field is broken would shrink a
    report at the exact moment something is clearly wrong."""
    path = telemetry_dir(run_path)
    path.mkdir(parents=True)
    (path / "cascade_walks.jsonl").write_text(
        json.dumps({"ts": "not-a-date", "kind": "cascade", "details": {"source": "x"}}) + "\n",
        encoding="utf-8",
    )
    assert len(read_events(run_path, "cascade", since=datetime(2026, 6, 5, tzinfo=UTC))) == 1


# ── summary roll-up ───────────────────────────────────────────────────────


def test_summary_counts_match_the_events(run_path: Path) -> None:
    for _ in range(3):
        record_stop_decision(run_path, reason_code="force_continue", block_count=2)
    record_stop_decision(run_path, reason_code="conclude")
    record_cascade_walk(run_path, source="accepted_rfc")
    record_cascade_walk(run_path, source="accepted_rfc")
    record_cascade_walk(run_path, source="ideate")
    record_dispatch(run_path, event="spawned")
    record_dispatch(run_path, event="finished_ok")
    record_tool_denial(run_path, tool_name="Bash")

    summary = build_summary(run_path)
    assert summary["stop_reasons"]["force_continue"] == 3
    assert summary["stop_reasons"]["conclude"] == 1
    assert summary["cascade_walks"] == 3
    assert summary["cascade_pick_distribution"] == {"accepted_rfc": 2, "ideate": 1}
    assert summary["dispatch_events"] == {"spawned": 1, "finished_ok": 1, "finished_err": 0}
    assert summary["tool_denials_total"] == 1


def test_summary_seeds_known_stop_codes_at_zero(run_path: Path) -> None:
    """Zero occurrences must be distinguishable from "this build doesn't
    emit that code"."""
    record_stop_decision(run_path, reason_code="force_continue")
    stops = build_summary(run_path)["stop_reasons"]
    assert stops["host_cap"] == 0
    assert stops["stop"] == 0


def test_summary_tracks_max_chain_depth(run_path: Path) -> None:
    record_stop_decision(run_path, reason_code="force_continue", block_count=3)
    record_stop_decision(run_path, reason_code="force_continue", block_count=9)
    record_stop_decision(run_path, reason_code="force_continue", block_count=1)
    summary = build_summary(run_path)
    assert summary["max_consecutive_blocks"] == 9
    assert summary["consecutive_blocks_at_last_stop"] == 1


def test_summary_reports_respawns_and_abort(run_path: Path) -> None:
    record_respawn(run_path, event="respawn", attempt=1)
    record_respawn(run_path, event="respawn", attempt=2)
    record_respawn(run_path, event="abort", attempt=3)
    respawns = build_summary(run_path)["respawns"]
    assert respawns["count"] == 2
    assert respawns["aborted"] is True
    assert respawns["last_at"] is not None


def test_summary_of_an_empty_run_is_all_zeroes(run_path: Path) -> None:
    summary = build_summary(run_path)
    assert summary["cascade_walks"] == 0
    assert summary["respawns"] == {"count": 0, "last_at": None, "aborted": False}


def test_summary_is_written_and_readable(run_path: Path) -> None:
    record_cascade_walk(run_path, source="ideate")
    build_summary(run_path)
    assert read_summary(run_path) == build_summary(run_path, write=False)


def test_read_summary_is_none_when_absent(run_path: Path) -> None:
    assert read_summary(run_path) is None


def test_write_false_does_not_touch_disk(run_path: Path) -> None:
    build_summary(run_path, write=False)
    assert read_summary(run_path) is None


# ── config ────────────────────────────────────────────────────────────────


def test_telemetry_config_defaults(tmp_path: Path) -> None:
    assert load_telemetry_config(tmp_path) == TelemetryConfig(enabled=True, retention_days=30)


def test_telemetry_config_reads_the_block(tmp_path: Path) -> None:
    nightly = tmp_path / ".nightly"
    nightly.mkdir()
    (nightly / "config.yml").write_text(
        "telemetry:\n  enabled: false\n  retention_days: 7\n", encoding="utf-8"
    )
    cfg = load_telemetry_config(tmp_path)
    assert cfg.enabled is False
    assert cfg.retention_days == 7


def test_zero_retention_degrades_to_default(tmp_path: Path) -> None:
    """An accidental `0` must not mean "delete the forensics now"."""
    nightly = tmp_path / ".nightly"
    nightly.mkdir()
    (nightly / "config.yml").write_text("telemetry:\n  retention_days: 0\n", encoding="utf-8")
    assert load_telemetry_config(tmp_path).retention_days == 30


def test_malformed_config_degrades_to_defaults(tmp_path: Path) -> None:
    nightly = tmp_path / ".nightly"
    nightly.mkdir()
    (nightly / "config.yml").write_text("telemetry: [oops\n", encoding="utf-8")
    assert load_telemetry_config(tmp_path).enabled is True


# ── retention ─────────────────────────────────────────────────────────────


def test_prune_removes_only_old_telemetry_folders(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    old = runs / "old-run"
    new = runs / "new-run"
    for run in (old, new):
        run.mkdir(parents=True)
        write_event(run, "cascade", {"source": "x"})

    stale = (datetime.now(UTC) - timedelta(days=90)).timestamp()
    import os

    os.utime(old, (stale, stale))

    assert prune_telemetry(runs, retention_days=30) == 1
    assert not telemetry_dir(old).exists()
    assert telemetry_dir(new).exists()


def test_prune_never_touches_the_run_itself(tmp_path: Path) -> None:
    """Plans, briefings, and keepalive.log are the permanent record."""
    runs = tmp_path / "runs"
    run = runs / "old-run"
    run.mkdir(parents=True)
    write_event(run, "cascade", {"source": "x"})
    (run / "keepalive.log").write_text("heartbeat\n", encoding="utf-8")

    stale = (datetime.now(UTC) - timedelta(days=90)).timestamp()
    import os

    os.utime(run, (stale, stale))

    prune_telemetry(runs, retention_days=30)
    assert run.is_dir()
    assert (run / "keepalive.log").is_file()


def test_prune_of_a_missing_root_is_a_noop(tmp_path: Path) -> None:
    assert prune_telemetry(tmp_path / "nope", retention_days=30) == 0
