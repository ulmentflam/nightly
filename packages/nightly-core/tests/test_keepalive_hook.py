"""Tests for nightly_core.keepalive_hook — Claude Code Stop-hook glue."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightly_core.keepalive_hook import (
    MAX_TURNS,
    SESSION_ACTIVE_FILENAME,
    SESSION_TTL_SECONDS,
    STOP_FILENAME,
    arm_session,
    compute_stop_hook_decision,
    disarm_session,
    log_heartbeat,
    parse_hook_input,
    request_stop,
)
from nightly_core.runs import start_run


@pytest.fixture
def initialized_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp repo with a fresh run, but the SESSION_ACTIVE marker not yet armed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".nightly" / "runs").mkdir(parents=True)
    start_run(tmp_path)
    return tmp_path


def test_no_run_allows_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    decision = compute_stop_hook_decision(tmp_path)
    assert decision.payload == {}
    assert decision.reason_code == "no_run"


def test_inactive_session_allows_stop(initialized_repo: Path) -> None:
    """A run exists but SESSION_ACTIVE was never armed — stop allowed."""
    decision = compute_stop_hook_decision(initialized_repo)
    assert decision.payload == {}
    assert decision.reason_code == "inactive"


def test_armed_session_blocks_stop(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo)
    assert decision.should_block
    assert decision.reason_code == "force_continue"
    assert "reason" in decision.payload
    assert decision.payload["decision"] == "block"


def test_conclude_overrides_active_session(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    # find current run dir
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    (run_dir / "CONCLUDE").write_text("", encoding="utf-8")
    decision = compute_stop_hook_decision(initialized_repo)
    assert not decision.should_block
    assert decision.reason_code == "conclude"


def test_stop_sentinel_overrides_active_session(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    request_stop(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo)
    assert not decision.should_block
    assert decision.reason_code == "stop"


def test_stale_marker_allows_stop(initialized_repo: Path) -> None:
    """If SESSION_ACTIVE is older than the TTL, the hook lets the session die."""
    arm_session(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    marker = run_dir / SESSION_ACTIVE_FILENAME
    # Backdate the marker by 5h (TTL is 4h)
    stale_time = (datetime.now(UTC) - timedelta(hours=5)).timestamp()
    import os

    os.utime(marker, (stale_time, stale_time))
    decision = compute_stop_hook_decision(initialized_repo)
    assert not decision.should_block
    assert decision.reason_code == "stale"


def test_max_turns_caps_force_continue(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    # Pre-seed the turn counter at the cap.
    (run_dir / "keepalive.turns").write_text(f"{MAX_TURNS}\n", encoding="utf-8")
    decision = compute_stop_hook_decision(initialized_repo)
    assert not decision.should_block
    assert decision.reason_code == "max_turns"


def test_force_continue_increments_turn_counter(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    compute_stop_hook_decision(initialized_repo)
    compute_stop_hook_decision(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    assert (run_dir / "keepalive.turns").read_text(encoding="utf-8").strip() == "2"


def test_continuation_reason_includes_cascade_summary(initialized_repo: Path) -> None:
    """When the cascade returns `nothing`, the prompt should point at keepalive."""
    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo)
    assert decision.should_block
    reason = decision.payload["reason"]
    assert "Nightly keepalive" in reason
    # Empty backlog → `nothing` → hint at think-harder strategies
    assert "nightly keepalive" in reason or "Continue on:" in reason


def test_arm_disarm_lifecycle(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    assert (run_dir / SESSION_ACTIVE_FILENAME).is_file()

    disarm_session(initialized_repo)
    assert not (run_dir / SESSION_ACTIVE_FILENAME).is_file()


def test_arm_session_returns_none_without_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert arm_session(tmp_path) is None


def test_request_stop_writes_sentinel(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    marker = request_stop(initialized_repo)
    assert marker is not None
    assert marker.name == STOP_FILENAME
    assert marker.is_file()


def test_heartbeat_log_appends_one_line(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo)
    log_path = log_heartbeat(decision, initialized_repo, hook_input={"session_id": "abc123"})
    assert log_path is not None
    content = log_path.read_text(encoding="utf-8")
    assert "decision=force_continue" in content
    assert "session=abc123" in content


def test_parse_hook_input_handles_empty_and_garbage() -> None:
    assert parse_hook_input("") == {}
    assert parse_hook_input("   \n") == {}
    assert parse_hook_input("not json at all {{{") == {}
    assert parse_hook_input("[1, 2, 3]") == {}  # non-dict JSON
    assert parse_hook_input('{"session_id": "x"}') == {"session_id": "x"}


def test_session_ttl_constant_is_4_hours() -> None:
    """Lock in the TTL — changing this is a behavior change worth a code review."""
    assert SESSION_TTL_SECONDS == 4 * 60 * 60


def test_max_turns_constant_is_500() -> None:
    """Lock in the safety cap — changing this changes the runaway-loop behavior."""
    assert MAX_TURNS == 500


def test_decision_payload_is_valid_json() -> None:
    """The hook stdout must be valid JSON Claude Code can parse."""
    decision_unblocked = compute_stop_hook_decision(None)
    # Must serialize cleanly
    json.dumps(decision_unblocked.payload)


# ── Phase 9i: cross-host wire formats ─────────────────────────────────────


def test_format_decision_allow_stop_is_universal(initialized_repo: Path) -> None:
    """Empty payload (`{}`) is identical across all hosts when allowing stop."""
    from nightly_core.keepalive_hook import format_decision

    decision = compute_stop_hook_decision(initialized_repo)  # inactive → allow
    assert format_decision(decision, fmt="claude_code") == {}
    assert format_decision(decision, fmt="cursor") == {}
    assert format_decision(decision, fmt="gemini_cli") == {}


def test_format_decision_claude_code_emits_block(initialized_repo: Path) -> None:
    from nightly_core.keepalive_hook import format_decision

    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo)
    payload = format_decision(decision, fmt="claude_code")
    assert payload["decision"] == "block"
    assert "reason" in payload


def test_format_decision_cursor_emits_followup_message(initialized_repo: Path) -> None:
    from nightly_core.keepalive_hook import format_decision

    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo)
    payload = format_decision(decision, fmt="cursor")
    assert "followup_message" in payload
    assert payload["followup_message"]  # non-empty
    assert "decision" not in payload  # not Claude's shape


def test_format_decision_gemini_cli_emits_deny(initialized_repo: Path) -> None:
    from nightly_core.keepalive_hook import format_decision

    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo)
    payload = format_decision(decision, fmt="gemini_cli")
    assert payload["decision"] == "deny"  # not "block"
    assert "reason" in payload


def test_hook_formats_exhaustive() -> None:
    """Lock the set of known formats so adding a new one is a deliberate change."""
    from nightly_core.keepalive_hook import HOOK_FORMATS

    assert set(HOOK_FORMATS) == {"claude_code", "cursor", "gemini_cli"}
