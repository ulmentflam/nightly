"""RFC 013 — the pi host integration.

These tests assert the extension's structure and Python integration.
`test_extension_runtime.py` executes the hook subprocess boundary in Node.
The release smoke checks also exercise real pi with a local provider.

The second cluster is about install locations. pi is the only host that
writes outside the scope the operator asked for, and getting that wrong
produces a keep-alive that works interactively and silently fails
headlessly, which is the failure mode the whole RFC exists to avoid.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nightly_core._version import __version__
from nightly_core.dispatch import HEADLESS_HOSTS, build_argv
from nightly_core.keepalive_hook import HOOK_FORMATS, StopHookDecision, format_decision
from nightly_host_pi import (
    PiHostIntegration,
    extension_version,
    parse_json_stream,
    render_keepalive_ts,
)
from nightly_host_pi import respawn as pi_respawn
from nightly_host_pi.respawn import respawn
from nightly_host_pi.skill import KEEPALIVE_TS, SKILL_MD, VERSION_MARKER_PREFIX


@pytest.fixture
def integration(tmp_path: Path) -> PiHostIntegration:
    """A pi integration whose global writes land in tmp, not ~/.pi.

    `pi_home` redirects *every* global path — user-scope skills, the
    extension, and trust.json. An earlier version of this fixture
    redirected only the extension, and `install("user")` promptly wrote
    real files into the developer's home."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return PiHostIntegration(root=repo, pi_home=tmp_path / "pihome")


def test_pi_home_redirects_every_global_path(tmp_path: Path) -> None:
    """Guards the accident above: no path may escape to the real home."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "pihome"
    integration = PiHostIntegration(root=repo, pi_home=home)
    for path in (
        integration.skill_path("user"),
        integration.conclude_skill_path("user"),
        integration.update_skill_path("user"),
        integration.init_skill_path("user"),
        integration.extension_path(),
        integration.trust_path(),
    ):
        assert home in path.parents


# ── the extension ─────────────────────────────────────────────────────────


def test_extension_carries_a_version_marker() -> None:
    assert KEEPALIVE_TS.startswith(VERSION_MARKER_PREFIX)
    assert extension_version(KEEPALIVE_TS) == __version__


def test_version_marker_is_stamped_not_hardcoded() -> None:
    rendered = render_keepalive_ts("9.9.9")
    assert extension_version(rendered) == "9.9.9"
    assert "__NIGHTLY_VERSION__" not in rendered


def test_extension_version_is_none_for_a_foreign_file() -> None:
    """An absent marker means reinstall, same as a mismatch."""
    assert extension_version("// someone else's extension\n") is None
    assert extension_version("") is None


def test_extension_subscribes_to_agent_settled_not_agent_end() -> None:
    """`agent_end` is too early — pi may still auto-retry or auto-compact,
    and continuing there injects a prompt mid-recovery."""
    assert 'pi.on("agent_settled"' in KEEPALIVE_TS
    assert 'pi.on("agent_end"' not in KEEPALIVE_TS


def test_extension_continues_via_followup_with_trigger_turn() -> None:
    assert "pi.sendMessage(" in KEEPALIVE_TS
    assert "triggerTurn: true" in KEEPALIVE_TS
    assert "followUp" in KEEPALIVE_TS


def _extension_code() -> str:
    """The extension with comment lines stripped.

    Its prose legitimately names cascade concepts while explaining that
    they deliberately live in Python — so a leak check has to look at
    code, not at the file."""
    return "\n".join(
        line for line in KEEPALIVE_TS.splitlines() if not line.lstrip().startswith("//")
    )


def test_extension_shells_out_rather_than_deciding() -> None:
    """The Stop decision must have exactly one implementation. A second one
    in TypeScript would drift, and the drift would surface at 03:00."""
    assert '"hook", "stop", "--format", "pi"' in KEEPALIVE_TS
    # No cascade logic in the executable part.
    code = _extension_code()
    for leaked in ("accepted_rfc", "pr_rescue", "resume_in_flight", "livelock", "ideate"):
        assert leaked not in code


def test_extension_guards_on_session_active() -> None:
    """This is what makes a *global* install acceptable — without it the
    extension would fire in every pi session on the machine."""
    assert "SESSION_ACTIVE" in KEEPALIVE_TS
    assert "armedRunPath" in KEEPALIVE_TS


def test_extension_guard_order_is_documented_and_intact() -> None:
    ts = KEEPALIVE_TS
    root_at = ts.index("findNightlyRoot(cwd)")
    run_at = ts.index("currentRunPath(root)")
    marker_at = ts.index("SESSION_ACTIVE))")
    assert root_at < run_at < marker_at


def test_extension_releases_on_every_failure_path() -> None:
    """Trapping a user who never asked for Nightly is worse than failing
    to continue."""
    assert KEEPALIVE_TS.count("return null;") >= 5
    assert "never trap the" in KEEPALIVE_TS


def test_extension_rechecks_idle_before_injecting() -> None:
    """Another extension may have started work while we shelled out."""
    assert "ctx.isIdle" in KEEPALIVE_TS


# ── wire format ───────────────────────────────────────────────────────────


def test_pi_is_a_known_hook_format() -> None:
    assert "pi" in HOOK_FORMATS


def test_pi_format_names_what_actually_happens() -> None:
    """`{"decision":"block"}` would be a lie — nothing is blocked, a
    follow-up is queued."""
    decision = StopHookDecision(
        payload={"decision": "block", "reason": "continue on X"},
        reason_code="force_continue",
        message="m",
    )
    payload = format_decision(decision, fmt="pi")
    assert payload == {"deliver_as": "followUp", "message": "continue on X"}
    assert "decision" not in payload


def test_pi_format_allow_stop_is_the_universal_empty_object() -> None:
    decision = StopHookDecision(payload={}, reason_code="conclude", message="m")
    assert format_decision(decision, fmt="pi") == {}


# ── install layout ────────────────────────────────────────────────────────


def test_install_writes_skill_and_extension(integration: PiHostIntegration) -> None:
    asyncio.run(integration.install("project"))
    assert integration.skill_path("project").is_file()
    assert integration.extension_path().is_file()
    assert integration.is_installed("project")


def test_skill_lands_under_dot_pi_skills(integration: PiHostIntegration) -> None:
    path = integration.skill_path("project")
    assert path.parts[-3:] == ("skills", "nightly", "SKILL.md")
    assert ".pi" in path.parts


def test_extension_is_global_even_at_project_scope(integration: PiHostIntegration) -> None:
    """The single most important behavior in RFC 013. A project-local
    extension loads interactively and silently fails headlessly, because
    pi's non-interactive modes never prompt for project trust."""
    asyncio.run(integration.install("project"))
    extension = integration.extension_path()
    assert integration.root not in extension.parents


def test_extension_is_installed_at_user_scope_too(integration: PiHostIntegration) -> None:
    """Hook hosts skip hook install at user scope; pi must not — the
    extension is global, so a user-scope install needs it just as much."""
    asyncio.run(integration.install("user"))
    assert integration.extension_path().is_file()


def test_keepalive_reports_installed_at_both_scopes(integration: PiHostIntegration) -> None:
    asyncio.run(integration.install("project"))
    assert integration.is_keepalive_hook_installed("project")
    assert integration.is_keepalive_hook_installed("user")


def test_uninstall_removes_both(integration: PiHostIntegration) -> None:
    asyncio.run(integration.install("project"))
    asyncio.run(integration.uninstall("project"))
    assert not integration.skill_path("project").is_file()
    assert not integration.extension_path().is_file()


def test_uninstall_is_idempotent(integration: PiHostIntegration) -> None:
    asyncio.run(integration.uninstall("project"))
    asyncio.run(integration.uninstall("project"))


def test_uninstall_leaves_a_shared_extension_dir_alone(
    integration: PiHostIntegration,
) -> None:
    """An operator may keep files of their own beside ours."""
    asyncio.run(integration.install("project"))
    sibling = integration.extension_path().parent / "operator-notes.md"
    sibling.write_text("mine", encoding="utf-8")
    asyncio.run(integration.uninstall("project"))
    assert sibling.is_file()


def test_installed_extension_version_round_trips(integration: PiHostIntegration) -> None:
    asyncio.run(integration.install("project"))
    assert integration.installed_extension_version() == __version__


def test_installed_extension_version_is_none_when_absent(
    integration: PiHostIntegration,
) -> None:
    assert integration.installed_extension_version() is None


def test_stale_extension_is_detectable(integration: PiHostIntegration) -> None:
    asyncio.run(integration.install("project"))
    integration.extension_path().write_text(render_keepalive_ts("0.0.1"), encoding="utf-8")
    assert integration.installed_extension_version() == "0.0.1"
    assert integration.installed_extension_version() != __version__


# ── project trust ─────────────────────────────────────────────────────────


def test_untrusted_by_default(integration: PiHostIntegration) -> None:
    assert not integration.is_project_trusted()


def test_trust_project_writes_a_decision(integration: PiHostIntegration, tmp_path: Path) -> None:
    trust = integration.trust_path()
    integration.trust_project()
    assert integration.is_project_trusted()
    payload = json.loads(trust.read_text(encoding="utf-8"))
    assert str(integration.root) in payload["trusted"]


def test_trust_project_merges_rather_than_clobbers(integration: PiHostIntegration) -> None:
    """Wiping an operator's other trust decisions to add ours would be
    indefensible."""
    trust = integration.trust_path()
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_text(json.dumps({"trusted": {"/somewhere/else": "yes"}}), encoding="utf-8")
    integration.trust_project()
    payload = json.loads(trust.read_text(encoding="utf-8"))
    assert payload["trusted"]["/somewhere/else"] == "yes"


def test_ancestor_trust_decision_applies(integration: PiHostIntegration, tmp_path: Path) -> None:
    """pi resolves the closest decision on the current path or a parent."""
    trust = integration.trust_path()
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_text(
        json.dumps({"trusted": {str(integration.root.parent): "yes"}}), encoding="utf-8"
    )
    assert integration.is_project_trusted()


def test_malformed_trust_file_reads_as_untrusted(integration: PiHostIntegration) -> None:
    trust = integration.trust_path()
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_text("{ not json", encoding="utf-8")
    assert not integration.is_project_trusted()


# ── dispatch argv ─────────────────────────────────────────────────────────


def test_pi_is_dispatchable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "pi" in HEADLESS_HOSTS


def test_build_argv_pi(monkeypatch: pytest.MonkeyPatch) -> None:
    from nightly_core import dispatch as d

    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/pi" if name == "pi" else None)
    argv = build_argv("pi", "do the thing")
    assert argv is not None
    assert argv[0] == "/usr/bin/pi"
    assert argv[1:5] == ["-p", "--mode", "json", "-a"]
    assert argv[-1] == "do the thing"


def test_build_argv_pi_always_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `-a`, headless pi ignores project resources and the
    specialist cannot find the skill it was told to run."""
    from nightly_core import dispatch as d

    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/pi" if name == "pi" else None)
    assert "-a" in (build_argv("pi", "x") or [])


def test_build_argv_pi_passes_thinking_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """pi is the first host with a real effort flag; everywhere else the
    tier reaches the agent only as a prompt directive."""
    from nightly_core import dispatch as d

    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/pi" if name == "pi" else None)
    argv = build_argv("pi", "x", effort="xhigh") or []
    assert "--thinking" in argv
    assert argv[argv.index("--thinking") + 1] == "xhigh"


def test_build_argv_pi_model_needs_a_discovered_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nightly emits a discovered flag, never a guessed one."""
    from nightly_core import dispatch as d

    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/pi" if name == "pi" else None)
    assert "gpt-5" not in (build_argv("pi", "x", model="gpt-5") or [])
    with_flag = build_argv("pi", "x", model="gpt-5", model_flag="--model") or []
    assert with_flag[with_flag.index("--model") + 1] == "gpt-5"


def test_build_argv_pi_none_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from nightly_core import dispatch as d

    monkeypatch.setattr(d.shutil, "which", lambda _name: None)
    assert build_argv("pi", "x") is None


# ── headless JSONL parsing ────────────────────────────────────────────────


def _stream(*events: dict) -> str:
    header = {"type": "session", "version": 3, "id": "u", "cwd": "/tmp"}
    return "\n".join(json.dumps(e) for e in (header, *events))


def test_parse_json_stream_reads_agent_end() -> None:
    raw = _stream(
        {"type": "turn_start"},
        {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            ],
        },
    )
    assert parse_json_stream(raw) == "done"


def test_parse_json_stream_falls_back_to_streamed_messages() -> None:
    """A stream truncated by a timeout has no agent_end; discarding the
    partial answer would lose the only evidence of what happened."""
    raw = _stream(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "partial"}]},
        },
    )
    assert parse_json_stream(raw) == "partial"


def test_parse_json_stream_prefers_agent_end_over_streamed() -> None:
    raw = _stream(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "streamed"}]},
        },
        {
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": [{"type": "text", "text": "final"}]}],
        },
    )
    assert parse_json_stream(raw) == "final"


def test_parse_json_stream_skips_unknown_events() -> None:
    """A schema addition upstream should cost us nothing."""
    raw = _stream(
        {"type": "some_future_event", "payload": {"x": 1}},
        {"type": "agent_end", "messages": [{"role": "assistant", "content": "ok"}]},
    )
    assert parse_json_stream(raw) == "ok"


def test_parse_json_stream_tolerates_malformed_lines() -> None:
    raw = _stream({"type": "agent_end", "messages": [{"role": "assistant", "content": "ok"}]})
    raw += '\n{"type": "message_en'
    assert parse_json_stream(raw) == "ok"


def test_parse_json_stream_ignores_non_assistant_messages() -> None:
    raw = _stream(
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "the prompt"},
                {"role": "assistant", "content": "the answer"},
            ],
        }
    )
    assert parse_json_stream(raw) == "the answer"


@pytest.mark.parametrize("raw", ["", "   ", "not json at all", "{}"])
def test_parse_json_stream_returns_empty_on_junk(raw: str) -> None:
    assert parse_json_stream(raw) == ""


# ── respawn ───────────────────────────────────────────────────────────────


def _completed(returncode: int = 0):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


def test_respawn_resumes_rather_than_restarting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pi is the first host where respawn is a true resume — `-c` continues
    the actual conversation instead of re-running the slash command."""
    monkeypatch.setattr(pi_respawn.shutil, "which", lambda name: f"/bin/{name}")
    captured: list[list[str]] = []
    outcome = respawn(
        tmp_path,
        runner=lambda argv, cwd=None: (captured.append(argv), _completed())[1],
    )
    assert outcome.ok
    assert "pi -c -a" in " ".join(captured[0])


def test_respawn_is_a_full_resume_via_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi_respawn.shutil, "which", lambda name: f"/bin/{name}")
    assert respawn(tmp_path, runner=lambda argv, cwd=None: _completed()).is_full_resume


def test_respawn_headless_fallback_is_not_a_full_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pi_respawn.shutil, "which", lambda name: "/bin/pi" if name == "pi" else None
    )
    outcome = respawn(tmp_path, runner=lambda argv, cwd=None: _completed())
    assert outcome.strategy == "headless"
    assert not outcome.is_full_resume


def test_respawn_fails_cleanly_with_no_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pi_respawn.shutil, "which", lambda _name: None)
    outcome = respawn(tmp_path, runner=lambda argv, cwd=None: _completed())
    assert not outcome.ok
    assert outcome.strategy == "failed"


def test_respawn_rejects_a_foreign_host(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        respawn(tmp_path, host="claude")


# ── skill content ─────────────────────────────────────────────────────────


def test_skill_carries_the_preflight_token() -> None:
    """`_REQUIRED_SKILL_TOKENS` and RFC 008's doctrine both depend on it."""
    assert "Pre-flight verification" in SKILL_MD or "pre-flight verification" in SKILL_MD.lower()


def test_skill_documents_the_global_extension() -> None:
    assert "globally" in SKILL_MD
    assert "agent_settled" in SKILL_MD


def test_skill_documents_project_trust() -> None:
    """A specialist that cannot find the skill is otherwise baffling."""
    assert "project trust" in SKILL_MD.lower()


def test_skill_forbids_the_operator_only_verbs() -> None:
    assert "nightly supervisor install" in SKILL_MD
    assert "never invoke" in SKILL_MD.lower()


def test_skill_states_there_is_no_sandbox() -> None:
    """pi ships none, and the refusal policy is the only thing left."""
    assert "no sandbox" in SKILL_MD.lower() or "no OS sandbox" in SKILL_MD


# ── home isolation ────────────────────────────────────────────────────────


def test_default_pi_home_honors_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite-wide conftest guard depends on this being read per call."""
    from nightly_host_pi import PI_HOME_ENV, default_pi_home

    monkeypatch.setenv(PI_HOME_ENV, "/tmp/elsewhere")
    assert default_pi_home() == Path("/tmp/elsewhere")


def test_default_pi_home_falls_back_to_the_real_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nightly_host_pi import PI_HOME_ENV, default_pi_home

    monkeypatch.delenv(PI_HOME_ENV, raising=False)
    assert default_pi_home() == Path.home() / ".pi" / "agent"


def test_an_uninjected_integration_still_respects_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure that motivated all this: integrations Nightly builds
    internally (doctor --all, init, update) take no `pi_home` argument, so
    the env var is the only thing standing between the suite and the
    developer's home."""
    from nightly_host_pi import PI_HOME_ENV

    monkeypatch.setenv(PI_HOME_ENV, str(tmp_path / "sandbox"))
    repo = tmp_path / "repo"
    repo.mkdir()
    uninjected = PiHostIntegration(root=repo)
    assert (tmp_path / "sandbox") in uninjected.extension_path().parents
