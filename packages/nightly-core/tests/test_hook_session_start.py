"""Tests for the `nightly hook session-start` CLI handler (v0.0.12).

It re-injects the session digest as SessionStart `additionalContext` after a
compaction, but only for an armed Nightly run with source=compact (or a
missing source). Everything else emits `{}` so non-Nightly sessions stay
untouched, and it never raises."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core.cli import app
from nightly_core.keepalive_hook import arm_session
from nightly_core.runs import start_run

runner = CliRunner()


def _invoke(monkeypatch: pytest.MonkeyPatch, payload: str) -> str:
    # hook_session_start reads stdin via sys.stdin.read() guarded by isatty().
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    result = runner.invoke(app, ["hook", "session-start"], input=payload)
    assert result.exit_code == 0, result.output
    return result.stdout.strip().splitlines()[-1]


@pytest.fixture
def armed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".nightly" / "runs").mkdir(parents=True)
    start_run(tmp_path)
    arm_session(tmp_path)
    monkeypatch.setattr("nightly_core.cascade.open_nightly_pr_branches", lambda root=None, **kw: [])
    return tmp_path


def test_emits_digest_for_armed_compact(armed_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(monkeypatch, '{"source": "compact", "session_id": "s1"}')
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert "Nightly session digest" in hso["additionalContext"]
    assert "re-injected after context compaction" in hso["additionalContext"]
    # Audit line appended.
    run_dir = next(p for p in (armed_repo / ".nightly" / "runs").iterdir() if p.is_dir())
    log = (run_dir / "keepalive.log").read_text(encoding="utf-8")
    assert "digest_reinject" in log


def test_emits_digest_for_missing_source(armed_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(monkeypatch, '{"session_id": "s1"}')
    payload = json.loads(out)
    assert "hookSpecificOutput" in payload


def test_other_source_emits_empty(armed_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(monkeypatch, '{"source": "startup"}')
    assert json.loads(out) == {}


def test_unarmed_run_emits_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".nightly" / "runs").mkdir(parents=True)
    start_run(tmp_path)  # no arm_session
    out = _invoke(monkeypatch, '{"source": "compact"}')
    assert json.loads(out) == {}


def test_no_run_emits_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = _invoke(monkeypatch, '{"source": "compact"}')
    assert json.loads(out) == {}


def test_garbage_stdin_emits_empty(armed_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(monkeypatch, "not json at all {{{")
    assert json.loads(out) == {}
