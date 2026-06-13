"""Tests for the v0.0.12 context-compaction additions to keepalive_hook:
context-size estimation, interval/planning-phase digest writes, budget
steering (context-diet prepend), and the ctx= heartbeat field."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightly_core.cascade import CascadeChoice
from nightly_core.keepalive_hook import (
    CONTEXT_FILENAME,
    arm_session,
    compute_stop_hook_decision,
    context_diet_block,
    estimate_context_tokens,
    log_heartbeat,
)
from nightly_core.runs import start_run


@pytest.fixture
def armed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".nightly" / "runs").mkdir(parents=True)
    start_run(tmp_path)
    arm_session(tmp_path)
    return tmp_path


def _stub_pick(monkeypatch: pytest.MonkeyPatch, choice: CascadeChoice) -> None:
    monkeypatch.setattr("nightly_core.keepalive_hook.next_task", lambda _root=None: choice)


def _usage_line(input_t: int, cache_create: int, cache_read: int, output_t: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": input_t,
                    "cache_creation_input_tokens": cache_create,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": output_t,
                }
            },
        }
    )


# ── estimate_context_tokens ────────────────────────────────────────────────


def test_estimate_sums_usage_fields(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_usage_line(100, 200, 300, 50) + "\n", encoding="utf-8")
    assert estimate_context_tokens(transcript) == 650


def test_estimate_finds_last_usage_when_trailing_lines_lack_it(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _usage_line(10, 0, 0, 0) + "\n"
        + _usage_line(1000, 0, 0, 23) + "\n"  # the latest assistant usage
        + json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    # Scans backwards: skips the user line, returns the most-recent usage.
    assert estimate_context_tokens(transcript) == 1023


def test_estimate_tolerates_garbage_lines(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "not json at all\n"
        + "{ broken json\n"
        + _usage_line(5, 5, 5, 5) + "\n"
        + "trailing garbage\n",
        encoding="utf-8",
    )
    assert estimate_context_tokens(transcript) == 20


def test_estimate_missing_file_returns_none(tmp_path: Path) -> None:
    assert estimate_context_tokens(tmp_path / "nope.jsonl") is None


def test_estimate_none_path_returns_none() -> None:
    assert estimate_context_tokens(None) is None


def test_estimate_no_usage_returns_none(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n",
        encoding="utf-8",
    )
    assert estimate_context_tokens(transcript) is None


def test_estimate_huge_leading_line_safe(tmp_path: Path) -> None:
    """A multi-MiB leading line never blows up the parse: it falls outside
    the 256 KiB tail window (and would hit the per-line cap anyway), and the
    valid usage line at the end is still found."""
    transcript = tmp_path / "t.jsonl"
    huge = json.dumps({"type": "user", "blob": "x" * (5 * 1024 * 1024)})
    transcript.write_text(
        huge + "\n" + _usage_line(7, 0, 0, 0) + "\n",
        encoding="utf-8",
    )
    assert estimate_context_tokens(transcript) == 7


def test_estimate_only_reads_tail_window(tmp_path: Path) -> None:
    """A usage line far before the 256 KiB tail window is NOT found."""
    transcript = tmp_path / "t.jsonl"
    early = _usage_line(999, 0, 0, 0)
    padding = "\n".join(json.dumps({"type": "user", "pad": "x" * 200}) for _ in range(3000))
    transcript.write_text(early + "\n" + padding + "\n", encoding="utf-8")
    # The early usage is outside the tail window and no other usage exists.
    assert estimate_context_tokens(transcript) is None


# ── context_diet_block formatting ──────────────────────────────────────────


def test_context_diet_block_formats_thousands() -> None:
    block = context_diet_block(301_000, 256_000)
    assert "~301K" in block
    assert "256K" in block
    assert "soft" in block.lower()
    assert "Do not stop the session over context size" in block


# ── heartbeat ctx= field ───────────────────────────────────────────────────


def test_heartbeat_includes_ctx_field(armed_repo: Path) -> None:
    decision = compute_stop_hook_decision(armed_repo)
    log_heartbeat(decision, armed_repo, context_tokens=12345)
    run_dir = next(p for p in (armed_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    log = (run_dir / "keepalive.log").read_text(encoding="utf-8")
    assert "ctx=12345" in log


def test_heartbeat_ctx_unknown_when_none(armed_repo: Path) -> None:
    decision = compute_stop_hook_decision(armed_repo)
    log_heartbeat(decision, armed_repo, context_tokens=None)
    run_dir = next(p for p in (armed_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    log = (run_dir / "keepalive.log").read_text(encoding="utf-8")
    assert "ctx=?" in log


# ── budget steering: diet prepend ──────────────────────────────────────────


def test_over_budget_prepends_diet_to_normal_reason(
    armed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = armed_repo / "t.jsonl"
    transcript.write_text(_usage_line(300_000, 0, 0, 1000) + "\n", encoding="utf-8")
    _stub_pick(
        monkeypatch,
        CascadeChoice(source="github_issue", summary="issue #1", rationale="top issue"),
    )
    decision = compute_stop_hook_decision(armed_repo, transcript_path=transcript)
    reason = decision.payload["reason"]
    assert reason.startswith("⚠ CONTEXT BUDGET")
    assert "Continue on:" in reason  # original reason preserved below the diet


def test_over_budget_prepends_diet_to_planning_phase(
    armed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = armed_repo / "t.jsonl"
    transcript.write_text(_usage_line(300_000, 0, 0, 1000) + "\n", encoding="utf-8")
    _stub_pick(
        monkeypatch,
        CascadeChoice(source="nothing", summary="no work", rationale="proposers empty"),
    )
    decision = compute_stop_hook_decision(armed_repo, transcript_path=transcript)
    reason = decision.payload["reason"]
    assert reason.startswith("⚠ CONTEXT BUDGET")
    assert "GENUINE WORK IS NEVER EXHAUSTED" in reason


def test_under_budget_does_not_prepend_diet(
    armed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = armed_repo / "t.jsonl"
    transcript.write_text(_usage_line(1000, 0, 0, 100) + "\n", encoding="utf-8")
    _stub_pick(
        monkeypatch,
        CascadeChoice(source="github_issue", summary="issue #1", rationale="top issue"),
    )
    decision = compute_stop_hook_decision(armed_repo, transcript_path=transcript)
    assert "CONTEXT BUDGET" not in decision.payload["reason"]


def test_budget_zero_disables_steering(
    armed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (armed_repo / ".nightly" / "config.yml").write_text(
        "context:\n  budget_tokens: 0\n", encoding="utf-8"
    )
    transcript = armed_repo / "t.jsonl"
    transcript.write_text(_usage_line(900_000, 0, 0, 0) + "\n", encoding="utf-8")
    _stub_pick(
        monkeypatch,
        CascadeChoice(source="github_issue", summary="issue #1", rationale="top issue"),
    )
    decision = compute_stop_hook_decision(armed_repo, transcript_path=transcript)
    assert "CONTEXT BUDGET" not in decision.payload["reason"]


# ── digest writes ──────────────────────────────────────────────────────────


def test_context_estimate_persisted(armed_repo: Path) -> None:
    transcript = armed_repo / "t.jsonl"
    transcript.write_text(_usage_line(2000, 0, 0, 50) + "\n", encoding="utf-8")
    compute_stop_hook_decision(armed_repo, transcript_path=transcript)
    run_dir = next(p for p in (armed_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    assert (run_dir / CONTEXT_FILENAME).read_text(encoding="utf-8").strip() == "2050"


def test_digest_written_on_interval(
    armed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pick(
        monkeypatch,
        CascadeChoice(source="github_issue", summary="issue #1", rationale="top issue"),
    )
    # Default digest_every_turns=1 → digest written every turn.
    compute_stop_hook_decision(armed_repo)
    run_dir = next(p for p in (armed_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    assert (run_dir / "digest.md").is_file()


def test_digest_interval_disabled_skips_normal_turn(
    armed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (armed_repo / ".nightly" / "config.yml").write_text(
        "context:\n  digest_every_turns: 0\n", encoding="utf-8"
    )
    # A non-planning pick with interval disabled → no digest write.
    _stub_pick(
        monkeypatch,
        CascadeChoice(source="github_issue", summary="issue #1", rationale="top issue"),
    )
    compute_stop_hook_decision(armed_repo)
    run_dir = next(p for p in (armed_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    assert not (run_dir / "digest.md").is_file()


def test_digest_always_written_on_planning_reroute(
    armed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with the interval disabled, a planning-phase reroute writes it."""
    (armed_repo / ".nightly" / "config.yml").write_text(
        "context:\n  digest_every_turns: 0\n", encoding="utf-8"
    )
    _stub_pick(
        monkeypatch,
        CascadeChoice(source="nothing", summary="no work", rationale="proposers empty"),
    )
    decision = compute_stop_hook_decision(armed_repo)
    assert "GENUINE WORK IS NEVER EXHAUSTED" in decision.payload["reason"]
    run_dir = next(p for p in (armed_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    assert (run_dir / "digest.md").is_file()
