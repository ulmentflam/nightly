"""Tests for nightly_core.dispatch — background specialist spawning."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core import dispatch
from nightly_core.cli import app
from nightly_core.dispatch import (
    BackgroundDispatchResult,
    build_argv,
    is_alive,
    list_dispatches,
    read_dispatch_state,
    refresh,
    start_background,
    supported_hosts,
    write_dispatch_state,
)
from nightly_core.runs import new_task, start_run


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def repo_with_task(repo: Path) -> tuple[Path, str]:
    """Initialize a run with one task seeded — most dispatch tests need one."""
    run = start_run(repo)
    new_task(run, slug="alpha")
    return repo, "alpha"


runner = CliRunner()


# ── build_argv ───────────────────────────────────────────────────────────


def test_build_argv_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dispatch.shutil,
        "which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    argv = build_argv("claude", "do the thing", session_id="sess-1")
    assert argv is not None
    assert argv[0] == "/usr/local/bin/claude"
    assert "-p" in argv
    assert "do the thing" in argv
    assert "--permission-mode" in argv
    assert "acceptEdits" in argv
    assert "--session-id" in argv
    assert "sess-1" in argv


def test_build_argv_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None
    )
    argv = build_argv("codex", "do the thing")
    assert argv is not None
    assert argv[:2] == ["/usr/bin/codex", "exec"]
    assert "--sandbox" in argv
    assert "workspace-write" in argv
    assert "--ask-for-approval" in argv
    assert "never" in argv
    assert "do the thing" in argv


def test_build_argv_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/bin/opencode" if name == "opencode" else None
    )
    argv = build_argv("opencode", "do the thing")
    assert argv is not None
    assert argv == ["/usr/bin/opencode", "run", "do the thing", "--format", "json"]


def test_build_argv_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/bin/gemini" if name == "gemini" else None
    )
    argv = build_argv("gemini", "do the thing")
    assert argv is not None
    assert argv == ["/usr/bin/gemini", "--prompt", "do the thing"]


def test_build_argv_returns_none_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch.shutil, "which", lambda _: None)
    assert build_argv("claude", "x") is None


def test_build_argv_returns_none_for_unsupported_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cursor + antigravity have no usable headless CLI today."""
    monkeypatch.setattr(dispatch.shutil, "which", lambda _: "/anywhere")
    assert build_argv("cursor", "x") is None
    assert build_argv("antigravity", "x") is None


def test_supported_hosts_set() -> None:
    """Locks the v1 host set so dropping support is a deliberate change."""
    assert set(supported_hosts()) == {"claude", "codex", "opencode", "gemini"}


# ── start_background ─────────────────────────────────────────────────────


class _FakePopen:
    """Captures Popen() arguments and returns a fake process with a PID."""

    def __init__(self, *, pid: int = 12345):
        self.pid = pid
        self.captured: dict | None = None

    def __call__(self, argv, **kwargs):
        # Important: write to the stdout handle so the test can verify
        # the spawned process has a writable log.
        stdout = kwargs.get("stdout")
        if stdout is not None and hasattr(stdout, "write"):
            stdout.write(b"fake spawn\n")
        self.captured = {"argv": list(argv), **kwargs}
        proc = type("FakeProc", (), {"pid": self.pid})()
        return proc


def test_start_background_spawns_and_records_state(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, slug = repo_with_task
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    fake = _FakePopen(pid=99001)

    result = start_background(
        slug,
        role="implementer",
        host="claude",
        prompt="advance the plan",
        root=repo,
        popen_factory=fake,
    )

    assert result.pid == 99001
    assert result.status == "running"
    assert result.role == "implementer"
    assert result.host == "claude"
    # State file persisted on disk
    state = read_dispatch_state(slug, root=repo)
    assert state is not None
    assert state.pid == 99001
    # Log file exists and got the header
    assert result.log_path.is_file()
    log_text = result.log_path.read_text(encoding="utf-8", errors="replace")
    assert "dispatch started" in log_text
    assert "fake spawn" in log_text
    # Spawn invocation included detach flags
    assert fake.captured is not None
    assert fake.captured["start_new_session"] is True
    assert fake.captured["stdin"] is subprocess.DEVNULL


def test_start_background_raises_for_unsupported_host(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, slug = repo_with_task
    # cursor has no headless backend; build_argv returns None.
    monkeypatch.setattr(dispatch.shutil, "which", lambda _: "/anywhere")
    with pytest.raises(RuntimeError, match="no background dispatch backend"):
        start_background(
            slug,
            role="implementer",
            host="cursor",
            prompt="x",
            root=repo,
            popen_factory=_FakePopen(),
        )


def test_start_background_raises_when_slug_unknown(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatching against a non-existent slug should error cleanly,
    not silently create state under a fabricated path."""
    start_run(repo)  # active run but no tasks
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    with pytest.raises(RuntimeError, match="not found"):
        start_background(
            "ghost",
            role="implementer",
            host="claude",
            prompt="x",
            root=repo,
            popen_factory=_FakePopen(),
        )


# ── read / list / write ──────────────────────────────────────────────────


def test_read_dispatch_state_returns_none_when_absent(
    repo_with_task: tuple[Path, str],
) -> None:
    repo, slug = repo_with_task
    assert read_dispatch_state(slug, root=repo) is None


def test_read_dispatch_state_tolerates_malformed_json(
    repo_with_task: tuple[Path, str],
) -> None:
    repo, slug = repo_with_task
    state_dir = repo / ".nightly" / "runs"
    # `runs/` contains a CURRENT pointer file alongside the run dirs —
    # filter to directories only.
    run_dir = next(p for p in state_dir.iterdir() if p.is_dir())
    task_dir = next((run_dir / "tasks").iterdir())
    (task_dir / "dispatch.json").write_text("{not valid", encoding="utf-8")
    assert read_dispatch_state(slug, root=repo) is None


def test_list_dispatches_returns_only_those_with_state(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = start_run(repo)
    new_task(run, slug="alpha")
    new_task(run, slug="beta")
    new_task(run, slug="gamma")
    # Only dispatch alpha + gamma
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    fake = _FakePopen(pid=100)
    start_background("alpha", role="implementer", host="claude", prompt="x", root=repo, popen_factory=fake)
    start_background("gamma", role="tester", host="claude", prompt="x", root=repo, popen_factory=fake)

    dispatches = list_dispatches(root=repo)
    slugs = {d.slug for d in dispatches}
    assert slugs == {"alpha", "gamma"}


def test_write_dispatch_state_round_trips(
    repo_with_task: tuple[Path, str],
) -> None:
    repo, slug = repo_with_task
    written = BackgroundDispatchResult(
        slug=slug,
        role="reviewer",
        host="codex",
        pid=4242,
        log_path=Path("/tmp/log.log"),
        started_at=datetime(2026, 5, 28, tzinfo=UTC),
        argv=("codex", "exec", "--json", "review"),
        cwd=Path("/tmp/work"),
        status="running",
        exit_code=None,
        finished_at=None,
    )
    write_dispatch_state(written, root=repo)
    read_back = read_dispatch_state(slug, root=repo)
    assert read_back is not None
    assert read_back.slug == slug
    assert read_back.role == "reviewer"
    assert read_back.host == "codex"
    assert read_back.pid == 4242
    assert read_back.argv == ("codex", "exec", "--json", "review")
    assert read_back.cwd == Path("/tmp/work")


# ── is_alive + refresh ───────────────────────────────────────────────────


def test_is_alive_returns_false_for_zero_pid() -> None:
    assert is_alive(0) is False
    assert is_alive(-1) is False


def test_is_alive_returns_false_for_missing_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(dispatch.os, "kill", boom)
    assert is_alive(99999) is False


def test_is_alive_returns_true_when_signal_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch.os, "kill", lambda _pid, _sig: None)
    assert is_alive(12345) is True


def test_is_alive_treats_permission_error_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PermissionError means the PID exists but is owned by another
    user — that still counts as live."""

    def perm(_pid, _sig):
        raise PermissionError

    monkeypatch.setattr(dispatch.os, "kill", perm)
    assert is_alive(12345) is True


def test_refresh_transitions_running_to_completed_when_pid_dead(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, slug = repo_with_task
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    fake = _FakePopen(pid=88)
    state = start_background(
        slug, role="implementer", host="claude", prompt="x", root=repo, popen_factory=fake
    )

    # Simulate the PID dying.
    def dead(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(dispatch.os, "kill", dead)
    refreshed = refresh(state, root=repo)
    assert refreshed.status == "completed"
    assert refreshed.finished_at is not None
    # Persisted on disk
    read_back = read_dispatch_state(slug, root=repo)
    assert read_back is not None
    assert read_back.status == "completed"


def test_refresh_no_op_when_already_finished(
    repo_with_task: tuple[Path, str],
) -> None:
    """A `completed` dispatch shouldn't re-poll the PID — the result is
    final."""
    _repo, slug = repo_with_task
    done = BackgroundDispatchResult(
        slug=slug,
        role="tester",
        host="claude",
        pid=99,
        log_path=Path("/tmp/x.log"),
        started_at=datetime(2026, 5, 28, tzinfo=UTC),
        status="completed",
        finished_at=datetime(2026, 5, 28, 0, 1, tzinfo=UTC),
    )
    out = refresh(done)
    assert out is done


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_dispatch_status_empty(repo_with_task: tuple[Path, str]) -> None:
    _repo, _ = repo_with_task
    result = runner.invoke(app, ["dispatch", "status"])
    assert result.exit_code == 0
    assert "no dispatches" in result.output


def test_cli_dispatch_status_single_unknown_slug(
    repo_with_task: tuple[Path, str],
) -> None:
    result = runner.invoke(app, ["dispatch", "status", "ghost"])
    assert result.exit_code == 1
    assert "no dispatch recorded" in result.output


def test_cli_dispatch_status_lists_active(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, slug = repo_with_task
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    monkeypatch.setattr(dispatch.os, "kill", lambda _pid, _sig: None)  # alive
    fake = _FakePopen(pid=77)
    start_background(
        slug, role="implementer", host="claude", prompt="x", root=repo, popen_factory=fake
    )

    result = runner.invoke(app, ["dispatch", "status"])
    assert result.exit_code == 0
    assert slug in result.output
    assert "running" in result.output
    assert "claude" in result.output


def test_cli_dispatch_start_emits_pid_log_status(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, slug = repo_with_task
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )

    fake = _FakePopen(pid=55512)
    # Patch the module-local factory alias instead of `subprocess.Popen`
    # — the latter would also patch click/typer's internal subprocess
    # usage and break CliRunner.
    monkeypatch.setattr(dispatch, "_DEFAULT_POPEN_FACTORY", fake)

    result = runner.invoke(
        app,
        ["dispatch", "start", slug, "--host", "claude", "--role", "implementer"],
    )
    assert result.exit_code == 0, result.output
    assert "pid=55512" in result.output
    assert "log=" in result.output
    assert "status=running" in result.output


def test_cli_dispatch_tail_reads_log(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, slug = repo_with_task
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    fake = _FakePopen(pid=66)
    state = start_background(
        slug, role="implementer", host="claude", prompt="x", root=repo, popen_factory=fake
    )
    # Pretend the spawned process wrote some output.
    with state.log_path.open("a", encoding="utf-8") as fh:
        fh.write("line one\nline two\nline three\n")

    result = runner.invoke(app, ["dispatch", "tail", slug, "--lines", "2"])
    assert result.exit_code == 0
    assert "line two" in result.output
    assert "line three" in result.output
    assert "line one" not in result.output


def test_cli_dispatch_wait_returns_completed_when_pid_dead(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, slug = repo_with_task
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    fake = _FakePopen(pid=44)
    start_background(
        slug, role="implementer", host="claude", prompt="x", root=repo, popen_factory=fake
    )

    # Simulate the process already done.
    def dead(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(dispatch.os, "kill", dead)
    result = runner.invoke(app, ["dispatch", "wait", slug, "--timeout", "0.5"])
    assert result.exit_code == 0
    assert "status=completed" in result.output


def test_cli_dispatch_wait_times_out_for_running(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, slug = repo_with_task
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    fake = _FakePopen(pid=33)
    start_background(
        slug, role="implementer", host="claude", prompt="x", root=repo, popen_factory=fake
    )
    monkeypatch.setattr(dispatch.os, "kill", lambda _pid, _sig: None)  # alive

    result = runner.invoke(
        app, ["dispatch", "wait", slug, "--timeout", "0.1", "--poll-interval", "0.05"]
    )
    assert result.exit_code == 1  # timed out while running
    assert "status=running" in result.output


def test_cli_dispatch_wait_returns_2_for_unknown_slug(
    repo_with_task: tuple[Path, str],
) -> None:
    result = runner.invoke(app, ["dispatch", "wait", "ghost"])
    assert result.exit_code == 2
    assert "no dispatch recorded" in result.output


def test_dispatch_state_file_lives_under_task_dir(
    repo_with_task: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock the on-disk layout: state lives next to the plan, not in a
    side index."""
    repo, slug = repo_with_task
    monkeypatch.setattr(
        dispatch.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    fake = _FakePopen(pid=11)
    start_background(
        slug, role="implementer", host="claude", prompt="x", root=repo, popen_factory=fake
    )

    runs = list((repo / ".nightly" / "runs").iterdir())
    run_dir = next(p for p in runs if p.is_dir())
    task_dir = next((run_dir / "tasks").iterdir())
    assert (task_dir / "dispatch.json").is_file()
    assert (task_dir / "dispatch.log").is_file()
    payload = json.loads((task_dir / "dispatch.json").read_text(encoding="utf-8"))
    assert payload["slug"] == slug
    assert payload["status"] == "running"
