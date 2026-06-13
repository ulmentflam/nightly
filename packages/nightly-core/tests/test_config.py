"""Tests for nightly_core.config.load_git_config."""

from __future__ import annotations

from pathlib import Path

from nightly_core.config import (
    ContextConfig,
    GitConfig,
    load_context_config,
    load_git_config,
)


def _write_config(root: Path, body: str) -> None:
    nightly = root / ".nightly"
    nightly.mkdir(parents=True, exist_ok=True)
    (nightly / "config.yml").write_text(body, encoding="utf-8")


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    assert load_git_config(tmp_path) == GitConfig()


def test_reads_full_git_block(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "git:\n"
        "  base_branch: develop\n"
        "  branch_prefix: agent/\n"
        "  worktree_root: ~/.cache/nightly/worktrees\n",
    )
    cfg = load_git_config(tmp_path)
    assert cfg.base_branch == "develop"
    assert cfg.branch_prefix == "agent/"
    assert cfg.worktree_root == "~/.cache/nightly/worktrees"


def test_missing_keys_fall_back_to_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, "git:\n  branch_prefix: nightly/\n")
    cfg = load_git_config(tmp_path)
    assert cfg.base_branch == "main"  # default
    assert cfg.branch_prefix == "nightly/"
    assert cfg.worktree_root is None  # unset → None


def test_no_git_block_yields_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, "hosts:\n  - claude\n")
    assert load_git_config(tmp_path) == GitConfig()


def test_blank_worktree_root_is_none(tmp_path: Path) -> None:
    _write_config(tmp_path, "git:\n  worktree_root: '   '\n")
    assert load_git_config(tmp_path).worktree_root is None


def test_malformed_yaml_yields_defaults(tmp_path: Path) -> None:
    # Unbalanced brackets → yaml.YAMLError, swallowed into defaults.
    _write_config(tmp_path, "git: [unclosed\n")
    assert load_git_config(tmp_path) == GitConfig()


def test_non_mapping_document_yields_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, "- just\n- a\n- list\n")
    assert load_git_config(tmp_path) == GitConfig()


# ── context: block (v0.0.12) ──────────────────────────────────────────────


def test_context_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg = load_context_config(tmp_path)
    assert cfg == ContextConfig()
    assert cfg.budget_tokens == 256_000
    assert cfg.digest_every_turns == 1


def test_context_reads_full_block(tmp_path: Path) -> None:
    _write_config(tmp_path, "context:\n  budget_tokens: 128000\n  digest_every_turns: 3\n")
    cfg = load_context_config(tmp_path)
    assert cfg.budget_tokens == 128_000
    assert cfg.digest_every_turns == 3


def test_context_missing_keys_fall_back(tmp_path: Path) -> None:
    _write_config(tmp_path, "context:\n  budget_tokens: 0\n")
    cfg = load_context_config(tmp_path)
    assert cfg.budget_tokens == 0  # explicitly disabled
    assert cfg.digest_every_turns == 1  # default


def test_context_no_block_yields_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, "hosts:\n  - claude\n")
    assert load_context_config(tmp_path) == ContextConfig()


def test_context_malformed_value_degrades_to_default(tmp_path: Path) -> None:
    # A non-integer budget should degrade to the default, not raise.
    _write_config(tmp_path, "context:\n  budget_tokens: not-a-number\n")
    cfg = load_context_config(tmp_path)
    assert cfg.budget_tokens == ContextConfig().budget_tokens


def test_context_malformed_yaml_yields_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, "context: [unclosed\n")
    assert load_context_config(tmp_path) == ContextConfig()
