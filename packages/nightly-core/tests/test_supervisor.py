"""RFC 010 Phase B — the respawn supervisor.

The trigger tests carry most of the weight. A supervisor that fires when
it shouldn't launches a second agent against a repo that already has one
running, which is worse than the stranded session it was trying to fix —
so "does not fire" is tested at least as hard as "fires".
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightly_core.config import SupervisorConfig, load_supervisor_config
from nightly_core.supervisor.daemon import (
    bump_respawn_count,
    poll_once,
    read_respawn_count,
    read_state,
    run_forever,
)
from nightly_core.supervisor.registry import (
    WatchedRepo,
    forget_repo,
    list_repos,
    prune_repos,
    register_repo,
    supervisor_home,
)
from nightly_core.supervisor.respawn import RespawnOutcome, respawn_host
from nightly_core.supervisor.service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT,
    render_launchd_plist,
    render_systemd_unit,
    service_path,
)
from nightly_core.supervisor.trigger import (
    SUPERVISOR_ABORTED_FILENAME,
    heartbeat_age_seconds,
    should_respawn,
)

NOW = datetime(2026, 6, 6, 20, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_supervisor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the developer's real `~/.cache/nightly/supervisor/`."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def run_path(tmp_path: Path) -> Path:
    """A live-looking run: armed, marker present, heartbeat stale."""
    path = tmp_path / ".nightly" / "runs" / "2026-06-06T19-00-00Z"
    path.mkdir(parents=True)
    (path / "SESSION_ACTIVE").write_text("stamp\n", encoding="utf-8")
    (path / "RESPAWN_REQUESTED").write_text("stamp\n", encoding="utf-8")
    _set_heartbeat(path, age_seconds=600)
    return path


def _set_heartbeat(run_path: Path, *, age_seconds: float) -> None:
    """Age the heartbeat relative to the fixed `NOW` the trigger is given."""
    log = run_path / "keepalive.log"
    log.write_text("beat\n", encoding="utf-8")
    when = (NOW - timedelta(seconds=age_seconds)).timestamp()
    os.utime(log, (when, when))


def _set_heartbeat_absolute(run_path: Path, *, age_seconds: float) -> None:
    """Age the heartbeat relative to the real clock.

    For the paths that don't accept an injected `now` — `run_forever`
    prunes and polls against `datetime.now()`."""
    log = run_path / "keepalive.log"
    log.write_text("beat\n", encoding="utf-8")
    when = (datetime.now(UTC) - timedelta(seconds=age_seconds)).timestamp()
    os.utime(log, (when, when))


# ── trigger: fires ────────────────────────────────────────────────────────


def test_fires_when_marker_present_and_heartbeat_stale(run_path: Path) -> None:
    verdict = should_respawn(run_path, now=NOW)
    assert verdict.fire
    assert verdict.reason == "fire"


def test_verdict_is_truthy(run_path: Path) -> None:
    assert should_respawn(run_path, now=NOW)


# ── trigger: does not fire ────────────────────────────────────────────────


def test_does_not_fire_while_the_heartbeat_is_fresh(run_path: Path) -> None:
    """The core distinction: a session blocking inside a forced chain has
    the marker present and is very much alive."""
    _set_heartbeat(run_path, age_seconds=10)
    verdict = should_respawn(run_path, now=NOW)
    assert not verdict.fire
    assert verdict.reason == "heartbeat_fresh"


def test_does_not_fire_without_the_respawn_marker(run_path: Path) -> None:
    (run_path / "RESPAWN_REQUESTED").unlink()
    assert should_respawn(run_path, now=NOW).reason == "no_marker"


def test_does_not_fire_on_an_unarmed_session(run_path: Path) -> None:
    """Non-Nightly sessions must never be supervised."""
    (run_path / "SESSION_ACTIVE").unlink()
    assert should_respawn(run_path, now=NOW).reason == "no_session"


def test_conclude_outranks_everything(run_path: Path) -> None:
    (run_path / "CONCLUDE").write_text("", encoding="utf-8")
    assert should_respawn(run_path, now=NOW).reason == "concluded"


def test_stop_outranks_everything(run_path: Path) -> None:
    (run_path / "STOP").write_text("", encoding="utf-8")
    assert should_respawn(run_path, now=NOW).reason == "stopped"


def test_does_not_fire_when_disabled(run_path: Path) -> None:
    assert should_respawn(run_path, enabled=False, now=NOW).reason == "disabled"


def test_does_not_fire_past_the_budget(run_path: Path) -> None:
    verdict = should_respawn(run_path, respawn_count=5, max_respawns=5, now=NOW)
    assert verdict.reason == "budget_exhausted"


def test_abort_marker_is_sticky(run_path: Path) -> None:
    """Without this the daemon re-fires every poll once the cap is passed."""
    (run_path / SUPERVISOR_ABORTED_FILENAME).write_text("", encoding="utf-8")
    assert should_respawn(run_path, now=NOW).reason == "budget_exhausted"


def test_does_not_fire_without_a_heartbeat_file(run_path: Path) -> None:
    """A run whose hook never fired is not dead — it may have just started."""
    (run_path / "keepalive.log").unlink()
    assert should_respawn(run_path, now=NOW).reason == "heartbeat_fresh"


def test_missing_run_directory_is_handled(tmp_path: Path) -> None:
    assert should_respawn(tmp_path / "nope", now=NOW).reason == "missing_run"


def test_heartbeat_age_is_none_when_absent(tmp_path: Path) -> None:
    assert heartbeat_age_seconds(tmp_path) is None


# ── registry ──────────────────────────────────────────────────────────────


def test_register_then_list(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert register_repo(repo, now=NOW)
    assert [r.path for r in list_repos()] == [repo.resolve()]


def test_register_is_idempotent(tmp_path: Path) -> None:
    """Re-registering refreshes the timestamp; it must not duplicate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    register_repo(repo, now=NOW)
    register_repo(repo, now=NOW + timedelta(days=1))
    repos = list_repos()
    assert len(repos) == 1
    assert repos[0].last_seen == NOW + timedelta(days=1)


def test_forget_removes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    register_repo(repo, now=NOW)
    assert forget_repo(repo)
    assert list_repos() == []


def test_forget_unknown_repo_is_false(tmp_path: Path) -> None:
    assert not forget_repo(tmp_path / "never-registered")


def test_prune_drops_stale_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    register_repo(repo, now=NOW - timedelta(days=90))
    assert prune_repos(now=NOW) == 1
    assert list_repos() == []


def test_prune_drops_repos_that_no_longer_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    register_repo(repo, now=NOW)
    repo.rmdir()
    assert prune_repos(now=NOW) == 1


def test_prune_keeps_live_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    register_repo(repo, now=NOW)
    assert prune_repos(now=NOW) == 0
    assert len(list_repos()) == 1


def test_corrupt_registry_degrades_to_empty(tmp_path: Path) -> None:
    home = supervisor_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "registry.json").write_text("{ not json", encoding="utf-8")
    assert list_repos() == []


def test_registry_write_is_atomic(tmp_path: Path) -> None:
    """A concurrent reader must never see a half-written file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    register_repo(repo, now=NOW)
    payload = json.loads((supervisor_home() / "registry.json").read_text(encoding="utf-8"))
    assert "watched_repos" in payload
    assert not list((supervisor_home()).glob("*.tmp"))


# ── respawn launcher ──────────────────────────────────────────────────────


def _fake_completed(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


def test_unsupported_host_raises_with_a_useful_message(tmp_path: Path) -> None:
    """A Codex operator installing the supervisor must learn immediately."""
    with pytest.raises(NotImplementedError, match="RFC 010"):
        respawn_host(tmp_path, host="codex")


def test_tmux_strategy_wins_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nightly_core.supervisor.respawn.shutil.which", lambda name: f"/bin/{name}")
    calls: list[list[str]] = []

    def _runner(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _fake_completed()

    outcome = respawn_host(tmp_path, runner=_runner)
    assert outcome.ok
    assert outcome.strategy == "tmux"
    assert calls[0][0] == "tmux"


def test_falls_back_to_headless_when_no_terminal_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nightly_core.supervisor.respawn.shutil.which",
        lambda name: "/bin/claude" if name == "claude" else None,
    )
    outcome = respawn_host(tmp_path, runner=lambda argv, cwd=None: _fake_completed())
    assert outcome.ok
    assert outcome.strategy == "headless"


def test_headless_is_not_reported_as_a_full_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has no Stop hook holding it open — one turn, not a resumed night."""
    monkeypatch.setattr(
        "nightly_core.supervisor.respawn.shutil.which",
        lambda name: "/bin/claude" if name == "claude" else None,
    )
    outcome = respawn_host(tmp_path, runner=lambda argv, cwd=None: _fake_completed())
    assert not outcome.is_full_resume


def test_tmux_is_reported_as_a_full_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nightly_core.supervisor.respawn.shutil.which", lambda name: f"/bin/{name}")
    outcome = respawn_host(tmp_path, runner=lambda argv, cwd=None: _fake_completed())
    assert outcome.is_full_resume


def test_failure_when_nothing_is_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nightly_core.supervisor.respawn.shutil.which", lambda _name: None)
    outcome = respawn_host(tmp_path, runner=lambda argv, cwd=None: _fake_completed())
    assert not outcome.ok
    assert outcome.strategy == "failed"


def test_respawn_command_accepts_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A respawned overnight session has nobody to answer prompts; without
    this it stalls on its first edit."""
    monkeypatch.setattr("nightly_core.supervisor.respawn.shutil.which", lambda name: f"/bin/{name}")
    captured: list[list[str]] = []
    respawn_host(
        tmp_path,
        runner=lambda argv, cwd=None: (captured.append(argv), _fake_completed())[1],
    )
    assert "acceptEdits" in " ".join(captured[0])


# ── poll loop ─────────────────────────────────────────────────────────────


def _repo_with_run(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    repo = tmp_path / name
    runs = repo / ".nightly" / "runs"
    runs.mkdir(parents=True)
    run = runs / "2026-06-06T19-00-00Z"
    run.mkdir()
    (runs / "CURRENT").write_text(run.name + "\n", encoding="utf-8")
    (run / "SESSION_ACTIVE").write_text("s\n", encoding="utf-8")
    (run / "RESPAWN_REQUESTED").write_text("s\n", encoding="utf-8")
    _set_heartbeat(run, age_seconds=600)
    return repo, run


def test_poll_fires_for_a_dead_run(tmp_path: Path) -> None:
    repo, _run = _repo_with_run(tmp_path)
    fired: list[Path] = []

    def _launcher(path: Path, *, host: str) -> RespawnOutcome:
        fired.append(path)
        return RespawnOutcome(True, "tmux", "ok")

    result = poll_once(
        repos=[WatchedRepo(path=repo, last_seen=NOW)],
        launcher=_launcher,
        now=NOW,
    )
    assert result.fired == 1
    assert fired == [repo]


def test_poll_does_not_fire_for_a_live_run(tmp_path: Path) -> None:
    repo, run = _repo_with_run(tmp_path)
    _set_heartbeat(run, age_seconds=5)
    fired: list[Path] = []

    result = poll_once(
        repos=[WatchedRepo(path=repo, last_seen=NOW)],
        launcher=lambda path, *, host: (fired.append(path), RespawnOutcome(True, "tmux", ""))[1],
        now=NOW,
    )
    assert result.fired == 0
    assert fired == []


def test_poll_skips_a_repo_with_no_current_run(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    (repo / ".nightly" / "runs").mkdir(parents=True)
    result = poll_once(repos=[WatchedRepo(path=repo, last_seen=NOW)], now=NOW)
    assert result.fired == 0
    assert result.verdicts[0][1].reason == "missing_run"


def test_poll_increments_the_respawn_count(tmp_path: Path) -> None:
    repo, run = _repo_with_run(tmp_path)
    poll_once(
        repos=[WatchedRepo(path=repo, last_seen=NOW)],
        launcher=lambda path, *, host: RespawnOutcome(True, "tmux", "ok"),
        now=NOW,
    )
    assert read_respawn_count(run) == 1


def test_budget_exhaustion_writes_the_abort_marker(tmp_path: Path) -> None:
    repo, run = _repo_with_run(tmp_path)
    (repo / ".nightly" / "config.yml").write_text(
        "supervisor:\n  max_respawns: 1\n  backoff_seconds: [0]\n", encoding="utf-8"
    )
    poll_once(
        repos=[WatchedRepo(path=repo, last_seen=NOW)],
        launcher=lambda path, *, host: RespawnOutcome(True, "tmux", "ok"),
        now=NOW,
    )
    assert (run / SUPERVISOR_ABORTED_FILENAME).is_file()


def test_no_further_respawn_after_abort(tmp_path: Path) -> None:
    repo, _run = _repo_with_run(tmp_path)
    (repo / ".nightly" / "config.yml").write_text(
        "supervisor:\n  max_respawns: 1\n  backoff_seconds: [0]\n", encoding="utf-8"
    )
    watched = [WatchedRepo(path=repo, last_seen=NOW)]
    launcher_calls: list[Path] = []

    def _launcher(path: Path, *, host: str) -> RespawnOutcome:
        launcher_calls.append(path)
        return RespawnOutcome(True, "tmux", "ok")

    poll_once(repos=watched, launcher=_launcher, now=NOW)
    poll_once(repos=watched, launcher=_launcher, now=NOW)
    assert len(launcher_calls) == 1


def test_respawn_is_recorded_in_run_telemetry(tmp_path: Path) -> None:
    repo, run = _repo_with_run(tmp_path)
    poll_once(
        repos=[WatchedRepo(path=repo, last_seen=NOW)],
        launcher=lambda path, *, host: RespawnOutcome(True, "tmux", "ok"),
        now=NOW,
    )
    from nightly_core.telemetry import read_summary

    summary = read_summary(run)
    assert summary is not None
    assert summary["respawns"]["count"] == 1


def test_unsupported_host_aborts_rather_than_looping(tmp_path: Path) -> None:
    """Retrying an unimplementable respawn every 30s all night is useless."""
    repo, run = _repo_with_run(tmp_path)
    (repo / ".nightly" / "config.yml").write_text(
        "supervisor:\n  host: codex\n  backoff_seconds: [0]\n", encoding="utf-8"
    )

    def _launcher(path: Path, *, host: str) -> RespawnOutcome:
        raise NotImplementedError("codex unsupported")

    poll_once(repos=[WatchedRepo(path=repo, last_seen=NOW)], launcher=_launcher, now=NOW)
    assert (run / SUPERVISOR_ABORTED_FILENAME).is_file()


def test_a_launcher_exception_does_not_kill_the_poll(tmp_path: Path) -> None:
    repo, _run = _repo_with_run(tmp_path)

    def _launcher(path: Path, *, host: str) -> RespawnOutcome:
        raise RuntimeError("boom")

    result = poll_once(repos=[WatchedRepo(path=repo, last_seen=NOW)], launcher=_launcher, now=NOW)
    assert result.checked == 1


def test_bump_respawn_count_persists(tmp_path: Path) -> None:
    """The count lives under the run so a daemon restart cannot reset it."""
    run = tmp_path / "run"
    run.mkdir()
    assert bump_respawn_count(run) == 1
    assert bump_respawn_count(run) == 2
    assert read_respawn_count(run) == 2


def test_run_forever_bounded_writes_state(tmp_path: Path) -> None:
    repo, run = _repo_with_run(tmp_path)
    # Real clock, not NOW: `run_forever` prunes the registry at startup
    # against `datetime.now()`, so a fixture-dated entry would be dropped
    # as stale before the first poll.
    register_repo(repo)
    _set_heartbeat_absolute(run, age_seconds=600)
    polls = run_forever(
        poll_interval=0,
        max_polls=1,
        launcher=lambda path, *, host: RespawnOutcome(True, "tmux", "ok"),
    )
    assert polls == 1
    state = read_state()
    assert state.polls == 1
    assert state.respawns == 1


# ── config ────────────────────────────────────────────────────────────────


def test_supervisor_config_defaults(tmp_path: Path) -> None:
    assert load_supervisor_config(tmp_path) == SupervisorConfig()


def test_supervisor_config_parses_the_block(tmp_path: Path) -> None:
    nightly = tmp_path / ".nightly"
    nightly.mkdir()
    (nightly / "config.yml").write_text(
        "supervisor:\n"
        "  enabled: false\n"
        "  max_respawns: 3\n"
        "  heartbeat_stale_seconds: 120\n"
        "  backoff_seconds: [0, 30]\n"
        "  host: claude\n",
        encoding="utf-8",
    )
    cfg = load_supervisor_config(tmp_path)
    assert cfg.enabled is False
    assert cfg.max_respawns == 3
    assert cfg.heartbeat_stale_seconds == 120
    assert cfg.backoff_seconds == (0, 30)


def test_garbage_numbers_degrade_to_defaults(tmp_path: Path) -> None:
    """A detached daemon has no terminal to complain to."""
    nightly = tmp_path / ".nightly"
    nightly.mkdir()
    (nightly / "config.yml").write_text(
        "supervisor:\n  max_respawns: banana\n  heartbeat_stale_seconds: -5\n", encoding="utf-8"
    )
    cfg = load_supervisor_config(tmp_path)
    assert cfg.max_respawns == 5
    assert cfg.heartbeat_stale_seconds == 90


# ── service files ─────────────────────────────────────────────────────────


def test_launchd_plist_is_well_formed() -> None:
    import plistlib

    parsed = plistlib.loads(render_launchd_plist("/usr/local/bin/nightly-supervisor").encode())
    assert parsed["Label"] == LAUNCHD_LABEL
    assert parsed["ProgramArguments"] == ["/usr/local/bin/nightly-supervisor"]
    assert parsed["KeepAlive"] is True


def test_launchd_plist_uses_an_absolute_binary_path() -> None:
    """launchd starts with a minimal PATH; a bare name fails at boot."""
    parsed_arg = render_launchd_plist("/opt/x/nightly-supervisor")
    assert "<string>/opt/x/nightly-supervisor</string>" in parsed_arg


def test_systemd_unit_restarts_always() -> None:
    unit = render_systemd_unit("/usr/bin/nightly-supervisor")
    assert "Restart=always" in unit
    assert "ExecStart=/usr/bin/nightly-supervisor" in unit


def test_service_path_per_platform() -> None:
    assert service_path(system="Darwin").name == f"{LAUNCHD_LABEL}.plist"  # type: ignore[union-attr]
    assert service_path(system="Linux").name == SYSTEMD_UNIT  # type: ignore[union-attr]
    assert service_path(system="Windows") is None


# ── Phase C: rules propagation and the doctor nudge ───────────────────────


def test_rule_14_is_in_the_rules_body() -> None:
    """The operator-only doctrine must reach every host's AGENTS.md /
    CLAUDE.md, which only happens if it is inside the shared block."""
    from nightly_core.rules import NIGHTLY_RULES_BODY

    assert "Never install, uninstall, or start the respawn supervisor" in NIGHTLY_RULES_BODY


def test_rule_14_names_the_forbidden_verbs() -> None:
    from nightly_core.rules import NIGHTLY_RULES_BODY

    for verb in ("nightly supervisor install", "uninstall", "nightly-supervisor"):
        assert verb in NIGHTLY_RULES_BODY


def test_rule_14_permits_the_read_only_verbs() -> None:
    """Forbidding diagnosis alongside installation would leave the agent
    unable to explain a session that was respawned."""
    from nightly_core.rules import NIGHTLY_RULES_BODY

    assert "nightly supervisor status" in NIGHTLY_RULES_BODY
    assert "read-only and safe to run" in NIGHTLY_RULES_BODY


def test_doctor_stays_quiet_when_nothing_has_ever_died(tmp_path: Path) -> None:
    """Recommending a daemon to someone who has never needed one is noise."""
    from nightly_core.doctor import _check_supervisor

    (tmp_path / ".nightly" / "runs").mkdir(parents=True)
    check = _check_supervisor(tmp_path)
    assert check.status == "ok"
    assert "nothing to fix" in (check.detail or "")


def test_doctor_nudges_once_a_run_has_ended_mid_chain(tmp_path: Path) -> None:
    from nightly_core.doctor import _check_supervisor

    runs = tmp_path / ".nightly" / "runs"
    run = runs / "2026-06-06T19-00-00Z"
    run.mkdir(parents=True)
    (run / "RESPAWN_REQUESTED").write_text("stamp\n", encoding="utf-8")

    check = _check_supervisor(tmp_path)
    assert "supervisor install" in (check.detail or "")


def test_doctor_supervisor_check_never_fails(tmp_path: Path) -> None:
    """A `missing` status would be an instruction the agent is forbidden
    from following (rule 14)."""
    from nightly_core.doctor import _check_supervisor

    runs = tmp_path / ".nightly" / "runs"
    run = runs / "r"
    run.mkdir(parents=True)
    (run / "RESPAWN_REQUESTED").write_text("x\n", encoding="utf-8")
    assert _check_supervisor(tmp_path).status == "ok"


@pytest.mark.parametrize("host", ["codex", "cursor", "antigravity", "opencode", "gemini"])
def test_every_non_claude_host_ships_a_loud_respawn_stub(host: str, tmp_path: Path) -> None:
    """Silent no-ops would let an operator believe they were covered."""
    import importlib

    module = importlib.import_module(f"nightly_host_{host}.respawn")
    with pytest.raises(NotImplementedError, match="RFC 010"):
        module.respawn(tmp_path)
