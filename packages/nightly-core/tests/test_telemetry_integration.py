"""RFC 010 §A3/A7/A8 — telemetry and redaction where they meet the rest of Nightly.

The unit tests cover the writers and the scrubber in isolation. These
cover the wiring: does the Stop hook actually emit, does the briefing
actually render it, and — the one that matters most — does a bug report
actually ship redacted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightly_core.briefing import _build_telemetry_alert, build_context, render_briefing
from nightly_core.bug import build_report, write_report
from nightly_core.keepalive_hook import (
    SESSION_ACTIVE_FILENAME,
    StopHookDecision,
    compute_stop_hook_decision,
    log_telemetry,
)
from nightly_core.runs import start_run
from nightly_core.telemetry import read_events, read_summary, record_respawn, record_stop_decision


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".nightly" / "runs").mkdir(parents=True)
    return tmp_path


# ── hook wiring (A3) ──────────────────────────────────────────────────────


def test_hook_decision_carries_telemetry_fields(repo: Path) -> None:
    """The fields must ride on the decision so the writer stays out of
    the pure decision path."""
    run = start_run(repo)
    (run.path / SESSION_ACTIVE_FILENAME).write_text("stamp\n", encoding="utf-8")

    decision = compute_stop_hook_decision(repo, stop_hook_active=True)
    assert decision.reason_code == "force_continue"
    assert decision.telemetry["block_count"] >= 1
    assert "turn" in decision.telemetry


def test_log_telemetry_writes_stop_and_cascade_events(repo: Path) -> None:
    run = start_run(repo)
    decision = StopHookDecision(
        payload={"decision": "block", "reason": "go"},
        reason_code="force_continue",
        message="msg",
        telemetry={
            "turn": 4,
            "block_count": 2,
            "cascade_source": "accepted_rfc",
            "cascade_summary": "RFC 010 A1",
            "cascade_repeats": 1,
        },
    )
    log_telemetry(decision, repo, hook_input={"session_id": "sess-1"})

    stops = read_events(run.path, "stop_decision")
    walks = read_events(run.path, "cascade")
    assert stops[0]["details"]["block_count"] == 2
    assert walks[0]["details"]["source"] == "accepted_rfc"


def test_log_telemetry_refreshes_the_summary(repo: Path) -> None:
    run = start_run(repo)
    decision = StopHookDecision(
        payload={},
        reason_code="conclude",
        message="msg",
        telemetry={"turn": 1},
    )
    log_telemetry(decision, repo)
    summary = read_summary(run.path)
    assert summary is not None
    assert summary["stop_reasons"]["conclude"] == 1


def test_log_telemetry_skips_a_cascade_event_when_no_pick(repo: Path) -> None:
    """Off-ramp boundaries never walked the cascade; recording a walk
    would skew the pick distribution."""
    run = start_run(repo)
    log_telemetry(
        StopHookDecision(payload={}, reason_code="stop", message="m", telemetry={}),
        repo,
    )
    assert read_events(run.path, "cascade") == []


def test_log_telemetry_respects_the_disable_switch(repo: Path) -> None:
    run = start_run(repo)
    (repo / ".nightly" / "config.yml").write_text(
        "telemetry:\n  enabled: false\n", encoding="utf-8"
    )
    log_telemetry(
        StopHookDecision(payload={}, reason_code="conclude", message="m", telemetry={}),
        repo,
    )
    assert read_summary(run.path) is None


def test_log_telemetry_is_a_noop_without_a_run(repo: Path) -> None:
    log_telemetry(StopHookDecision(payload={}, reason_code="no_run", message="m"), repo)


# ── briefing (A8) ─────────────────────────────────────────────────────────


def test_briefing_context_has_no_telemetry_when_none_recorded(repo: Path) -> None:
    run = start_run(repo)
    ctx = build_context(run)
    assert ctx.telemetry is None
    assert ctx.telemetry_alert is None


def test_briefing_renders_the_telemetry_section(repo: Path) -> None:
    run = start_run(repo)
    record_stop_decision(run.path, reason_code="force_continue", block_count=2)
    from nightly_core.telemetry import build_summary, record_cascade_walk

    record_cascade_walk(run.path, source="accepted_rfc")
    build_summary(run.path)

    html = render_briefing(run)
    assert "session telemetry" in html
    assert "accepted_rfc" in html


def test_briefing_omits_the_section_entirely_when_telemetry_is_absent(repo: Path) -> None:
    run = start_run(repo)
    assert "session telemetry" not in render_briefing(run)


def test_alert_fires_on_respawn(repo: Path) -> None:
    run = start_run(repo)
    record_respawn(run.path, event="respawn", attempt=1)
    from nightly_core.telemetry import build_summary

    build_summary(run.path)
    ctx = build_context(run)
    assert ctx.telemetry_alert is not None
    assert "respawned 1 time" in ctx.telemetry_alert


def test_abort_outranks_respawn_in_the_alert() -> None:
    """Only the worst condition is reported — a stacked list buries it."""
    alert = _build_telemetry_alert({"respawns": {"count": 5, "aborted": True}})
    assert alert is not None
    assert "exhausted" in alert


def test_deep_chain_alerts_even_without_a_respawn() -> None:
    alert = _build_telemetry_alert({"max_consecutive_blocks": 9, "respawns": {"count": 0}})
    assert alert is not None
    assert "9 consecutive" in alert


def test_smooth_session_gets_no_alert() -> None:
    assert _build_telemetry_alert({"max_consecutive_blocks": 2, "respawns": {"count": 0}}) is None


# ── bug report (A7) ───────────────────────────────────────────────────────


def test_bug_report_body_is_redacted_by_default(repo: Path) -> None:
    """This is the guard on the surface that publishes to a public issue."""
    start_run(repo)
    report = build_report(root=repo, summary=f"broke while reading {Path.home()}/private/notes.md")
    assert "private/notes.md" not in report.body
    assert report.redaction is not None
    assert report.redaction.count > 0


def test_bug_report_states_that_it_was_redacted(repo: Path) -> None:
    """A maintainer seeing `~/<path:…>` must know it is redaction, not
    corruption; the operator must be told to read before submitting."""
    start_run(repo)
    report = build_report(root=repo, summary=f"path {Path.home()}/x/y.py")
    assert "redacted before sharing" in report.body
    assert "review the body above" in report.body.lower()


def test_bug_report_raw_body_available_for_tests_only(repo: Path) -> None:
    start_run(repo)
    marker = f"{Path.home()}/unmistakable/path.py"
    report = build_report(root=repo, summary=marker, redact_body=False)
    assert marker in report.body
    assert report.redaction is None


def test_write_report_emits_the_local_decoder_map(repo: Path) -> None:
    start_run(repo)
    report = build_report(root=repo, summary=f"path {Path.home()}/x/y.py")
    written = write_report(report)
    sidecar = written.with_suffix(".md.redaction-map.json")
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "LOCAL ONLY" in payload["note"]


def test_llm_backstop_is_off_when_the_caller_says_so(repo: Path) -> None:
    """Explicit `--no-llm-backstop` must not spawn a host CLI."""
    start_run(repo)
    report = build_report(root=repo, summary="plain", llm_backstop=False)
    assert report.redaction is not None
    assert "llm_backstop" not in report.redaction.passes_applied


def test_include_telemetry_appends_the_section(repo: Path) -> None:
    run = start_run(repo)
    record_stop_decision(run.path, reason_code="force_continue", block_count=3)
    from nightly_core.telemetry import build_summary

    build_summary(run.path)

    report = build_report(root=repo, include_telemetry=True, llm_backstop=False)
    assert "Session telemetry" in report.body
    assert "max_consecutive_blocks" in report.body


def test_telemetry_section_is_absent_by_default(repo: Path) -> None:
    """The payload is large; operators opt in."""
    run = start_run(repo)
    record_stop_decision(run.path, reason_code="force_continue")
    from nightly_core.telemetry import build_summary

    build_summary(run.path)
    assert "Session telemetry" not in build_report(root=repo, llm_backstop=False).body


def test_include_telemetry_explains_itself_when_there_is_none(repo: Path) -> None:
    start_run(repo)
    report = build_report(root=repo, include_telemetry=True, llm_backstop=False)
    assert "No telemetry for this run" in report.body
