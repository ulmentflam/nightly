"""Tests for `nightly init`'s model-control discovery.

The probe's whole justification is that Nightly should not carry a table
of vendor flags and model ids that goes stale. So these tests exercise it
against captured help text rather than pinning a hardcoded expectation:
given what a CLI says about itself, does the right flag, vocabulary, and
tier hierarchy come out?
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from nightly_core.contract import MODEL_TIERS, HostId, ModelTier
from nightly_core.model_probe import (
    HARNESS_ENV_MARKERS,
    HELP_INVOCATIONS,
    assign_tiers,
    detect_harness,
    discover_tier_bindings,
    is_pinned,
    merge_discovered_tiers,
    probe_model_control,
    probe_models,
)

# Verbatim from `claude --help` (2026-07-28).
CLAUDE_HELP = """
  -c, --continue                        Continue the most recent conversation
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name (e.g.
                                        'claude-fable-5').
  --session-id <uuid>                   Use a specific session ID
  --output-format <format>              Output format (e.g. 'json', 'text')
"""

NO_MODEL_HELP = """
  -h, --help      Show help
  -v, --version   Show version
"""

SHORT_FLAG_HELP = """
  -m, --model MODEL   Model to use (e.g. 'gpt-5.6', 'gpt-5-mini')
"""


def _runner(mapping: dict[tuple[str, ...], str]):
    """Fake help-runner keyed by the argv suffix after the binary."""

    def run(argv: Sequence[str]) -> str:
        return mapping.get(tuple(argv[1:]), "")

    return run


def _which(present: set[str]):
    def which(name: str) -> str | None:
        return f"/usr/local/bin/{name}" if name in present else None

    return which


# ── flag discovery ────────────────────────────────────────────────────────


def test_discovers_long_flag_from_real_claude_help() -> None:
    control = probe_model_control(
        "claude",
        runner=_runner({("--help",): CLAUDE_HELP}),
        which=_which({"claude"}),
    )
    assert control is not None
    assert control.flag == "--model"
    assert control.probed_via == ("--help",)


def test_records_short_alias_when_help_lists_one() -> None:
    control = probe_model_control(
        "codex",
        runner=_runner({("exec", "--help"): SHORT_FLAG_HELP}),
        which=_which({"codex"}),
    )
    assert control is not None
    assert control.flag == "--model"
    assert control.short_flag == "-m"


def test_falls_back_to_root_help_when_subcommand_has_no_flag() -> None:
    control = probe_model_control(
        "codex",
        runner=_runner({("exec", "--help"): NO_MODEL_HELP, ("--help",): CLAUDE_HELP}),
        which=_which({"codex"}),
    )
    assert control is not None
    assert control.probed_via == ("--help",)


def test_installed_but_no_model_flag_is_not_the_same_as_absent() -> None:
    """`flag=None` means "runs on its default model", not "not installed"."""
    control = probe_model_control(
        "gemini",
        runner=_runner({("--help",): NO_MODEL_HELP}),
        which=_which({"gemini"}),
    )
    assert control is not None
    assert control.flag is None
    assert control.supported is False


def test_absent_binary_yields_none() -> None:
    assert probe_model_control("claude", runner=_runner({}), which=_which(set())) is None


def test_unreadable_help_degrades_to_no_flag() -> None:
    """A CLI that errors, hangs, or prints nothing must not break init.

    `_run_help` returns '' for every failure mode (non-zero exit, timeout,
    OSError), so empty output is the single shape the parser has to
    survive — and it resolves to "no known flag", not an exception.
    """
    control = probe_model_control("claude", runner=lambda _: "", which=_which({"claude"}))
    assert control is not None
    assert control.flag is None
    assert control.supported is False


# ── model vocabulary ──────────────────────────────────────────────────────


def test_reads_model_vocabulary_out_of_help_text() -> None:
    models = probe_models(
        "claude",
        runner=_runner({("--help",): CLAUDE_HELP}),
        which=_which({"claude"}),
    )
    assert models == ["fable", "opus", "sonnet", "claude-fable-5"]


def test_ignores_quoted_tokens_that_are_not_models() -> None:
    """'json' and 'text' are quoted in the same help page, under another flag."""
    models = probe_models(
        "claude",
        runner=_runner({("--help",): CLAUDE_HELP}),
        which=_which({"claude"}),
    )
    assert "json" not in models
    assert "text" not in models


def test_no_vocabulary_when_host_is_absent() -> None:
    assert probe_models("claude", runner=_runner({}), which=_which(set())) == []


# ── tier assignment ───────────────────────────────────────────────────────


def test_assigns_families_to_the_right_tiers() -> None:
    tiers = assign_tiers(["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"])
    assert tiers == {
        "lite": "claude-haiku-4-5",
        "coding": "claude-sonnet-5",
        "reasoning": "claude-opus-5",
    }


def test_opus_beats_fable_for_the_reasoning_slot() -> None:
    """Reasoning is the judgment tier, not the most-expensive-available tier."""
    tiers = assign_tiers(["claude-fable-5", "claude-opus-5"])
    assert tiers["reasoning"] == "claude-opus-5"


def test_pinned_id_beats_bare_alias_within_a_family() -> None:
    tiers = assign_tiers(["opus", "claude-opus-5"])
    assert tiers["reasoning"] == "claude-opus-5"


def test_unrecognized_families_are_skipped() -> None:
    assert assign_tiers(["some-unknown-model"]) == {}


def test_cross_vendor_families_land_correctly() -> None:
    tiers = assign_tiers(["gpt-5-mini", "qwen-coder-3", "gpt-5-reasoning"])
    assert tiers["lite"] == "gpt-5-mini"
    assert tiers["coding"] == "qwen-coder-3"
    assert tiers["reasoning"] == "gpt-5-reasoning"


# ── pinned-vs-alias precedence ────────────────────────────────────────────


@pytest.mark.parametrize("model", ["claude-opus-5", "gpt-5.6", "qwen-coder-3"])
def test_pinned_ids_are_recognized(model: str) -> None:
    assert is_pinned(model)


@pytest.mark.parametrize("model", ["opus", "sonnet", "haiku"])
def test_bare_aliases_are_not_pinned(model: str) -> None:
    assert not is_pinned(model)


def test_seeded_pinned_default_survives_a_discovered_alias() -> None:
    """An alias floats to the latest release — wrong for a reproducible run."""
    merged = merge_discovered_tiers(
        {"claude": {"reasoning": "claude-opus-5"}},
        {"claude": {"reasoning": "opus"}},
    )
    assert merged["claude"]["reasoning"] == "claude-opus-5"


def test_discovered_pinned_id_overrides_the_seeded_default() -> None:
    merged = merge_discovered_tiers(
        {"claude": {"reasoning": "claude-opus-5"}},
        {"claude": {"reasoning": "claude-opus-6"}},
    )
    assert merged["claude"]["reasoning"] == "claude-opus-6"


def test_alias_is_used_when_there_is_no_seeded_default() -> None:
    """A floating alias still beats no binding at all."""
    merged = merge_discovered_tiers({}, {"codex": {"coding": "gpt"}})
    assert merged["codex"]["coding"] == "gpt"


def test_merge_does_not_mutate_the_seeded_table() -> None:
    seeded: dict[HostId, dict[ModelTier, str]] = {"claude": {"reasoning": "claude-opus-5"}}
    merge_discovered_tiers(seeded, {"claude": {"lite": "claude-haiku-9"}})
    assert seeded == {"claude": {"reasoning": "claude-opus-5"}}


# ── harness detection ─────────────────────────────────────────────────────


def test_detects_claude_code_from_its_env_marker() -> None:
    assert detect_harness({"CLAUDECODE": "1"}) == "claude"


def test_detects_via_secondary_marker() -> None:
    assert detect_harness({"CLAUDE_CODE_ENTRYPOINT": "cli"}) == "claude"


def test_plain_shell_detects_no_harness() -> None:
    assert detect_harness({"PATH": "/usr/bin"}) is None


def test_empty_marker_value_does_not_count_as_present() -> None:
    assert detect_harness({"CLAUDECODE": ""}) is None


@pytest.mark.parametrize(
    "host", ["claude", "codex", "cursor", "gemini", "opencode", "pi", "hermes"]
)
def test_every_major_harness_is_probeable(host: HostId) -> None:
    """Gemini, OpenCode, Codex, Claude, Cursor, Pi, Hermes all covered."""
    assert host in HARNESS_ENV_MARKERS
    assert host in HELP_INVOCATIONS


# ── end-to-end discovery ──────────────────────────────────────────────────


def test_discovery_returns_tiers_and_flags_together() -> None:
    tiers, flags = discover_tier_bindings(
        ["claude"],
        runner=_runner({("--help",): CLAUDE_HELP}),
        which=_which({"claude"}),
    )
    assert flags == {"claude": "--model"}
    assert tiers["claude"]["reasoning"] == "opus"


def test_discovery_omits_hosts_that_are_not_installed() -> None:
    tiers, flags = discover_tier_bindings(
        ["claude", "codex"],
        runner=_runner({("--help",): CLAUDE_HELP}),
        which=_which({"claude"}),
    )
    assert "codex" not in tiers
    assert "codex" not in flags


def test_discovered_tiers_are_a_subset_of_the_known_tiers() -> None:
    tiers, _ = discover_tier_bindings(
        ["claude"],
        runner=_runner({("--help",): CLAUDE_HELP}),
        which=_which({"claude"}),
    )
    assert set(tiers["claude"]) <= set(MODEL_TIERS)
