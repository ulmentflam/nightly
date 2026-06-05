"""Tests for nightly_core.keepalive_hook — Claude Code Stop-hook glue."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightly_core.cascade import CascadeChoice
from nightly_core.keepalive_hook import (
    LOOP_HISTORY_FILENAME,
    SESSION_ACTIVE_FILENAME,
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


def test_stale_marker_no_longer_releases(initialized_repo: Path) -> None:
    """v0.0.3: the SESSION_TTL_SECONDS staleness check was removed.

    A SESSION_ACTIVE marker backdated by days still force-continues —
    the only voluntary terminations are human-placed CONCLUDE / STOP.
    """
    arm_session(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    marker = run_dir / SESSION_ACTIVE_FILENAME
    # Backdate the marker by 5h — would have been stale under v0.0.2's 4h TTL.
    stale_time = (datetime.now(UTC) - timedelta(hours=5)).timestamp()
    import os

    os.utime(marker, (stale_time, stale_time))
    decision = compute_stop_hook_decision(initialized_repo)
    assert decision.should_block
    assert decision.reason_code == "force_continue"


def test_max_turns_no_longer_caps_force_continue(initialized_repo: Path) -> None:
    """v0.0.3: the MAX_TURNS=500 safety cap was removed.

    Even when the turn counter has run high, the hook keeps force-
    continuing. The turn counter is still bumped for telemetry, but
    no longer gates termination.
    """
    arm_session(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    # Pre-seed the counter at what used to be the cap.
    (run_dir / "keepalive.turns").write_text("500\n", encoding="utf-8")
    decision = compute_stop_hook_decision(initialized_repo)
    assert decision.should_block
    assert decision.reason_code == "force_continue"


def test_force_continue_increments_turn_counter(initialized_repo: Path) -> None:
    arm_session(initialized_repo)
    compute_stop_hook_decision(initialized_repo)
    compute_stop_hook_decision(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    assert (run_dir / "keepalive.turns").read_text(encoding="utf-8").strip() == "2"


def test_continuation_reason_includes_cascade_summary(initialized_repo: Path) -> None:
    """When the cascade returns `nothing`, the prompt should command action."""
    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo)
    assert decision.should_block
    reason = decision.payload["reason"]
    # Header is always present
    assert "Nightly keepalive" in reason
    # Empty backlog → imperative "make a recommendation now" command,
    # NOT a "consider running nightly keepalive" suggestion.
    assert "Make a recommendation" in reason or "Continue on:" in reason


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


def test_session_ttl_constant_removed_from_public_api() -> None:
    """v0.0.3: `SESSION_TTL_SECONDS` is no longer exported. The 4-hour
    staleness check it gated is gone — only human-placed markers
    terminate a session now."""
    import nightly_core.keepalive_hook as kh

    assert not hasattr(kh, "SESSION_TTL_SECONDS")


def test_max_turns_constant_removed_from_public_api() -> None:
    """v0.0.3: `MAX_TURNS` is no longer exported. The 500-turn cap was
    removed; the counter is still incremented for telemetry but no
    longer gates termination."""
    import nightly_core.keepalive_hook as kh

    assert not hasattr(kh, "MAX_TURNS")


def test_loop_threshold_constant_removed_from_public_api() -> None:
    """v0.0.3: `LOOP_THRESHOLD` is no longer exported. The
    cascade-loop guard was removed; the history file still gets
    written for diagnostics but no longer triggers a release."""
    import nightly_core.keepalive_hook as kh

    assert not hasattr(kh, "LOOP_THRESHOLD")


# ── v0.0.3: PR-backlog cap removed (was Phase 9p) ────────────────────────
# The previous MAX_OPEN_PRS=5 cap and its `pr_backlog` reason_code were
# removed per the operator's "always advance, always" directive. These
# regression tests confirm the hook no longer reads the PR count and no
# longer emits `pr_backlog` regardless of how many PRs are open.


def test_pr_backlog_constant_removed_from_public_api() -> None:
    """`MAX_OPEN_PRS` is no longer exported from `nightly_core.keepalive_hook`.

    Anyone reaching for it after v0.0.3 should get an ImportError — the
    constant doesn't exist and the gating it implemented is gone.
    """
    import nightly_core.keepalive_hook as kh

    assert not hasattr(kh, "MAX_OPEN_PRS")


def test_hook_force_continues_regardless_of_open_pr_count(
    initialized_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with 100 open Nightly PRs, the hook still force-continues an
    armed session against a paperwork cascade pick. The PR-count cap
    that used to release here is gone in v0.0.3+.
    """
    arm_session(initialized_repo)
    # Stub the cascade pick to a non-resume-priority source so the previous
    # cap-aware behavior *would* have released. We're asserting it doesn't.
    monkeypatch.setattr(
        "nightly_core.keepalive_hook.next_task",
        lambda _root=None: CascadeChoice(
            source="ideate_fallback",
            summary="ship lint cleanup",
            rationale="armed-session fallback",
        ),
    )
    decision = compute_stop_hook_decision(initialized_repo)
    assert decision.should_block
    assert decision.reason_code == "force_continue"


def test_hook_does_not_call_count_open_nightly_prs(
    initialized_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook must no longer reach for the PR count helper at all.

    Patches the helper to raise — if the hook still touches it, the
    decision computation will crash. Confirms the dependency was fully
    removed, not just the gating logic.
    """
    arm_session(initialized_repo)

    def _explode(_root: Path | None = None) -> int:
        msg = "count_open_nightly_prs must not be called from the Stop hook"
        raise AssertionError(msg)

    # Patch on the cascade module since the hook no longer re-imports it.
    monkeypatch.setattr("nightly_core.cascade.count_open_nightly_prs", _explode)
    decision = compute_stop_hook_decision(initialized_repo)
    # No crash; normal force-continue.
    assert decision.should_block
    assert decision.reason_code == "force_continue"


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


# ── stop_hook_active — yield to the host's consecutive-block cap ──────────


def test_stop_hook_active_short_circuits_when_session_armed(
    initialized_repo: Path,
) -> None:
    """When the host signals it's about to override us, emit `{}` even if
    we *would have* force-continued. Past failure: Claude Code logged
    `A hook blocked the turn from ending 9 consecutive times — overriding`
    because Nightly's hook kept returning `block` regardless."""
    arm_session(initialized_repo)
    # Sanity: without the signal, we'd force-continue
    baseline = compute_stop_hook_decision(initialized_repo)
    assert baseline.should_block

    decision = compute_stop_hook_decision(initialized_repo, stop_hook_active=True)
    assert decision.payload == {}
    assert decision.reason_code == "host_cap"
    assert "stop_hook_active" in decision.message


def test_stop_hook_active_short_circuits_without_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The yield must work even when there's no active run — we should
    not let the cap-yield branch trip over disk-read assumptions of
    later branches."""
    monkeypatch.chdir(tmp_path)
    decision = compute_stop_hook_decision(tmp_path, stop_hook_active=True)
    assert decision.payload == {}
    assert decision.reason_code == "host_cap"


def test_stop_hook_active_false_preserves_normal_decision(
    initialized_repo: Path,
) -> None:
    """The default (`stop_hook_active=False`) keeps prior behavior."""
    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo, stop_hook_active=False)
    assert decision.should_block
    assert decision.reason_code == "force_continue"


def test_parse_hook_input_round_trips_stop_hook_active() -> None:
    """parse_hook_input must surface the flag so the CLI can consult it."""
    assert parse_hook_input('{"stop_hook_active": true}') == {"stop_hook_active": True}
    assert parse_hook_input('{"stop_hook_active": false}') == {"stop_hook_active": False}
    # Missing field is fine — CLI defaults to False via bool().
    assert "stop_hook_active" not in parse_hook_input('{"session_id": "x"}')


def test_heartbeat_log_records_host_cap_yield(initialized_repo: Path) -> None:
    """The audit trail must show *why* we stopped force-continuing so a
    post-mortem can distinguish a host-cap yield from a CONCLUDE drain
    or a stale marker. They look identical on the wire (`{}`)."""
    arm_session(initialized_repo)
    decision = compute_stop_hook_decision(initialized_repo, stop_hook_active=True)
    log_path = log_heartbeat(decision, initialized_repo, hook_input={"session_id": "abc"})
    assert log_path is not None
    content = log_path.read_text(encoding="utf-8")
    assert "decision=host_cap" in content
    assert "session=abc" in content


# ── v0.0.3: cascade-loop guard removed; history-only diagnostics retained ─


def test_repeated_picks_no_longer_release(initialized_repo: Path) -> None:
    """v0.0.3: repeated cascade picks no longer release the session.

    The previous LOOP_THRESHOLD-based guard was removed per the
    operator's "the only termination should be human intervention"
    directive. A stuck cascade now keeps force-continuing; the
    operator must place a CONCLUDE / STOP marker to end the session
    (or wait for the host's 9-block override, which is out of our
    control and addressed by RFC 010's respawn supervisor).
    """
    arm_session(initialized_repo)
    # Empty repo → cascade returns the same `nothing` pick every turn.
    # In v0.0.2 this would have released after 3 consecutive same-picks.
    # In v0.0.3+ it just keeps force-continuing.
    for i in range(10):
        d = compute_stop_hook_decision(initialized_repo)
        assert d.should_block, f"turn {i + 1} should still force-continue"
        assert d.reason_code == "force_continue"


def test_loop_history_still_written_for_diagnostics(initialized_repo: Path) -> None:
    """The fingerprint history file is still written even though it
    no longer triggers a release — operators can inspect it after a
    session to see what the cascade was returning."""
    arm_session(initialized_repo)
    compute_stop_hook_decision(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    history = run_dir / LOOP_HISTORY_FILENAME
    assert history.is_file()
    lines = history.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("nothing|") or lines[0].startswith("ideate")


def test_loop_history_remains_bounded(initialized_repo: Path) -> None:
    """The history file must still be bounded — long runs can't grow it
    without limit even though it's diagnostic-only now."""
    from nightly_core.keepalive_hook import _LOOP_HISTORY_KEEP

    arm_session(initialized_repo)
    run_dir = next(p for p in (initialized_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    (run_dir / LOOP_HISTORY_FILENAME).write_text(
        "\n".join(f"old|-|entry {i}" for i in range(50)) + "\n",
        encoding="utf-8",
    )
    compute_stop_hook_decision(initialized_repo)
    lines = (run_dir / LOOP_HISTORY_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) <= _LOOP_HISTORY_KEEP


def test_hook_stop_cli_honors_stop_hook_active(
    initialized_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the CLI: stdin payload with `stop_hook_active=true`
    → stdout `{}` even when the session is armed."""
    import io

    from typer.testing import CliRunner

    from nightly_core.cli import app

    arm_session(initialized_repo)
    runner = CliRunner()
    # Force `sys.stdin.isatty()` to False inside hook_stop so it reads our payload.
    monkeypatch.setattr("sys.stdin", io.StringIO('{"stop_hook_active": true, "session_id": "abc"}'))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    result = runner.invoke(app, ["hook", "stop"], input='{"stop_hook_active": true}')
    assert result.exit_code == 0, result.output
    # Stdout should be exactly `{}` (allow-stop) — no `decision: block` shape.
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {}
