"""Tests for RFC 009 Phase C — synthesis.json cache + --force + doctor drift."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core.cli import app
from nightly_core.proposers.synthesis import (
    SYNTHESIS_CACHE_FILENAME,
    SynthesisProposer,
    _current_run_dir,
    _read_synthesis_cache,
    _write_synthesis_cache,
)
from nightly_core.runs import start_run


def _stage_repo_with_run(tmp_path: Path) -> Path:
    """Minimum repo skeleton + an active run so synthesis can cache."""
    (tmp_path / "README.md").write_text("# t\n\nObj: ship.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Always advance.\n", encoding="utf-8")
    src = tmp_path / "packages" / "demo" / "src" / "demo" / "__init__.py"
    src.parent.mkdir(parents=True)
    src.write_text("# demo\n", encoding="utf-8")
    start_run(tmp_path)
    return tmp_path


def _stub_runner(payload: str):
    calls = {"count": 0, "prompts": []}

    def _runner(prompt: str, _root: Path) -> str:
        calls["count"] += 1
        calls["prompts"].append(prompt)
        return payload

    return _runner, calls


_SAMPLE_PAYLOAD = """[
    {"strategic_category": "cleaning", "title": "X", "description": "dead", "file_scope": ["a.py"]}
]"""


# ── current_run_dir + cache I/O ──────────────────────────────────────────


def test_current_run_dir_returns_active_run(tmp_path: Path) -> None:
    _stage_repo_with_run(tmp_path)
    run_dir = _current_run_dir(tmp_path)
    assert run_dir is not None
    assert run_dir.is_dir()
    assert (run_dir.parent / "CURRENT").is_file()


def test_current_run_dir_returns_none_when_no_run(tmp_path: Path) -> None:
    assert _current_run_dir(tmp_path) is None


def test_write_synthesis_cache_persists_envelope(tmp_path: Path) -> None:
    _stage_repo_with_run(tmp_path)
    _write_synthesis_cache(tmp_path, head_sha="abc123", raw_output=_SAMPLE_PAYLOAD)
    run_dir = _current_run_dir(tmp_path)
    assert run_dir is not None
    cache_path = run_dir / SYNTHESIS_CACHE_FILENAME
    assert cache_path.is_file()
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    assert envelope["head_sha"] == "abc123"
    assert "ran_at" in envelope
    assert isinstance(envelope["proposals"], list)
    assert len(envelope["proposals"]) == 1


def test_write_synthesis_cache_silently_skips_malformed_output(tmp_path: Path) -> None:
    """Don't pollute the cache with non-JSON noise."""
    _stage_repo_with_run(tmp_path)
    _write_synthesis_cache(tmp_path, head_sha="x", raw_output="not json at all")
    run_dir = _current_run_dir(tmp_path)
    assert run_dir is not None
    assert not (run_dir / SYNTHESIS_CACHE_FILENAME).is_file()


def test_write_synthesis_cache_silently_skips_when_no_run(tmp_path: Path) -> None:
    """No active run → cache write is a no-op (no .nightly dir to write to)."""
    _write_synthesis_cache(tmp_path, head_sha="x", raw_output=_SAMPLE_PAYLOAD)
    # No exception; nothing to assert other than "didn't crash."


def test_read_synthesis_cache_returns_none_when_missing(tmp_path: Path) -> None:
    _stage_repo_with_run(tmp_path)
    assert _read_synthesis_cache(tmp_path, head_sha="any") is None


def test_read_synthesis_cache_invalidates_on_sha_mismatch(tmp_path: Path) -> None:
    """A new commit on `main` between cascade walks → SHA changes → cache
    invalidates → proposer re-spawns. The cache survives within a run on
    a stable SHA but not across."""
    _stage_repo_with_run(tmp_path)
    _write_synthesis_cache(tmp_path, head_sha="abc123", raw_output=_SAMPLE_PAYLOAD)
    assert _read_synthesis_cache(tmp_path, head_sha="def456") is None
    # Same SHA returns the cached proposals.
    cached = _read_synthesis_cache(tmp_path, head_sha="abc123")
    assert cached is not None
    assert len(cached) == 1
    assert cached[0].strategic_category == "cleaning"


def test_read_synthesis_cache_handles_corrupt_envelope(tmp_path: Path) -> None:
    """A truncated or malformed JSON envelope returns None — refuse rather
    than feed garbage into the parser."""
    _stage_repo_with_run(tmp_path)
    run_dir = _current_run_dir(tmp_path)
    assert run_dir is not None
    (run_dir / SYNTHESIS_CACHE_FILENAME).write_text("{not json", encoding="utf-8")
    assert _read_synthesis_cache(tmp_path, head_sha="x") is None


# ── SynthesisProposer end-to-end with cache + force ──────────────────────


def test_synthesis_proposer_caches_first_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First propose() call spawns the LLM; second call reads the cache."""
    _stage_repo_with_run(tmp_path)
    monkeypatch.setattr(
        "nightly_core.proposers.synthesis._git_head_short_sha",
        lambda _root: "abc123",
    )
    runner, calls = _stub_runner(_SAMPLE_PAYLOAD)
    proposer = SynthesisProposer(runner=runner)

    first = list(proposer.propose(tmp_path))
    assert len(first) == 1
    assert calls["count"] == 1

    second = list(proposer.propose(tmp_path))
    assert len(second) == 1
    assert calls["count"] == 1, "second call should hit cache, not re-spawn"


def test_synthesis_proposer_force_bypasses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`force=True` skips the cache lookup even when a cache exists."""
    _stage_repo_with_run(tmp_path)
    monkeypatch.setattr(
        "nightly_core.proposers.synthesis._git_head_short_sha",
        lambda _root: "abc123",
    )
    # Pre-seed a cache.
    _write_synthesis_cache(tmp_path, head_sha="abc123", raw_output=_SAMPLE_PAYLOAD)

    runner, calls = _stub_runner(_SAMPLE_PAYLOAD)
    proposer = SynthesisProposer(runner=runner, force=True)
    list(proposer.propose(tmp_path))
    assert calls["count"] == 1, "force=True must re-spawn even with cache present"


def test_synthesis_proposer_invalidates_cache_on_new_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different head_sha between calls → cache stale → re-spawn."""
    _stage_repo_with_run(tmp_path)
    sha_box = ["abc123"]
    monkeypatch.setattr(
        "nightly_core.proposers.synthesis._git_head_short_sha",
        lambda _root: sha_box[0],
    )
    runner, calls = _stub_runner(_SAMPLE_PAYLOAD)
    proposer = SynthesisProposer(runner=runner)

    list(proposer.propose(tmp_path))  # first spawn, writes cache @ abc123
    sha_box[0] = "def456"  # commit advances HEAD
    list(proposer.propose(tmp_path))  # cache @ abc123 stale → re-spawn

    assert calls["count"] == 2


def test_synthesis_proposer_does_not_cache_empty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty parse result → no cache write → next call re-spawns. We'd
    rather pay the spawn cost again than burn a session's cache slot
    on noise."""
    _stage_repo_with_run(tmp_path)
    monkeypatch.setattr(
        "nightly_core.proposers.synthesis._git_head_short_sha",
        lambda _root: "abc123",
    )
    runner, calls = _stub_runner("not json")  # parse fails → empty list
    proposer = SynthesisProposer(runner=runner)

    list(proposer.propose(tmp_path))
    list(proposer.propose(tmp_path))
    assert calls["count"] == 2  # both calls spawned


def test_synthesis_proposer_respects_config_enabled_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ideate.synthesis.enabled: false` in config → proposer short-circuits
    before even building the prompt."""
    _stage_repo_with_run(tmp_path)
    (tmp_path / ".nightly" / "config.yml").write_text(
        "ideate:\n  synthesis:\n    enabled: false\n", encoding="utf-8"
    )
    runner, calls = _stub_runner(_SAMPLE_PAYLOAD)
    proposer = SynthesisProposer(runner=runner)
    proposals = list(proposer.propose(tmp_path))
    assert proposals == []
    assert calls["count"] == 0


# ── CLI --force flag ─────────────────────────────────────────────────────


def test_cli_propose_force_flag_propagates_to_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nightly propose --force` must construct the SynthesisProposer with
    force=True. Smoke-test via the CLI runner with a stubbed registry."""
    _stage_repo_with_run(tmp_path)

    constructed: dict[str, bool] = {}

    class _SpyingProposer:
        id = "synthesis"

        def __init__(self, *, force: bool = False, **_):
            constructed["force"] = force

        def propose(self, _root: Path):
            return ()

    monkeypatch.setattr(
        "nightly_core.proposers.registry.SynthesisProposer",
        _SpyingProposer,
    )
    monkeypatch.setattr("nightly_core.cli.repo_root", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["propose", "--force"])
    assert result.exit_code == 0, result.output
    assert constructed["force"] is True


def test_cli_propose_without_force_constructs_synthesis_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_repo_with_run(tmp_path)
    constructed: dict[str, bool] = {}

    class _SpyingProposer:
        id = "synthesis"

        def __init__(self, *, force: bool = False, **_):
            constructed["force"] = force

        def propose(self, _root: Path):
            return ()

    monkeypatch.setattr(
        "nightly_core.proposers.registry.SynthesisProposer",
        _SpyingProposer,
    )
    monkeypatch.setattr("nightly_core.cli.repo_root", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["propose"])
    assert result.exit_code == 0
    assert constructed["force"] is False


# ── nightly doctor prompt-drift check (RFC 009 §C3) ──────────────────────


def test_doctor_synthesis_prompt_check_passes_on_canonical_template() -> None:
    """The shipped `synthesis_prompt.md` must contain every required anchor.
    If this test fails, either the prompt template was edited without
    updating `_REQUIRED_SYNTHESIS_PROMPT_ANCHORS` or vice versa."""
    from nightly_core.doctor import _check_synthesis_prompt

    check = _check_synthesis_prompt()
    assert check.status == "ok", check.detail


def test_doctor_synthesis_prompt_check_flags_missing_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a stale/edited prompt by replacing the loader with one
    that returns a stripped-down version. The doctor flags it as
    missing and surfaces the absent anchors."""
    monkeypatch.setattr(
        "nightly_core.proposers.synthesis.load_synthesis_prompt",
        lambda: "this prompt has no anchors at all",
    )
    from nightly_core.doctor import _check_synthesis_prompt

    check = _check_synthesis_prompt()
    assert check.status == "missing"
    assert "objectives" in check.detail
    assert "nightly update" in check.detail.lower()


def test_doctor_synthesis_prompt_check_handles_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the prompt file is gone (extreme edge — the wheel was tampered
    with), the doctor reports `error` rather than crashing."""

    def _raise(*_args, **_kwargs):
        msg = "synthetic load failure"
        raise OSError(msg)

    monkeypatch.setattr(
        "nightly_core.proposers.synthesis.load_synthesis_prompt",
        _raise,
    )
    from nightly_core.doctor import _check_synthesis_prompt

    check = _check_synthesis_prompt()
    assert check.status == "error"
