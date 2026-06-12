"""Tests for nightly_core.hook_install matcher support (v0.0.12).

The Stop hook is matcher-less (matcher=""); the SessionStart(compact) hook
carries matcher="compact". Both must merge/remove idempotently and
independently, and matcher-less behavior must stay byte-for-byte compatible.
"""

from __future__ import annotations

import json
from pathlib import Path

from nightly_core.hook_install import (
    HookFile,
    find_nested_hook_index,
    merge_nested_hook,
    read_settings,
    remove_nested_hook,
)


def _stop(path: Path) -> HookFile:
    return HookFile(path=path, event_name="Stop", command="nightly hook stop")


def _session_start(path: Path) -> HookFile:
    return HookFile(
        path=path,
        event_name="SessionStart",
        command="nightly hook session-start",
        matcher="compact",
    )


def test_matcherless_merge_writes_empty_matcher(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert merge_nested_hook(_stop(path)) is True
    settings = read_settings(path)
    block = settings["hooks"]["Stop"][0]
    assert block["matcher"] == ""
    assert block["hooks"][0]["command"] == "nightly hook stop"


def test_matcher_merge_writes_matcher(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert merge_nested_hook(_session_start(path)) is True
    settings = read_settings(path)
    block = settings["hooks"]["SessionStart"][0]
    assert block["matcher"] == "compact"
    assert block["hooks"][0]["command"] == "nightly hook session-start"


def test_both_hooks_coexist(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    merge_nested_hook(_stop(path))
    merge_nested_hook(_session_start(path))
    settings = read_settings(path)
    assert "Stop" in settings["hooks"]
    assert "SessionStart" in settings["hooks"]


def test_matcher_merge_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert merge_nested_hook(_session_start(path)) is True
    assert merge_nested_hook(_session_start(path)) is False
    settings = read_settings(path)
    assert len(settings["hooks"]["SessionStart"]) == 1


def test_matcher_remove_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    merge_nested_hook(_session_start(path))
    assert remove_nested_hook(_session_start(path)) is True
    assert remove_nested_hook(_session_start(path)) is False


def test_remove_one_leaves_the_other(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    merge_nested_hook(_stop(path))
    merge_nested_hook(_session_start(path))
    remove_nested_hook(_session_start(path))
    settings = read_settings(path)
    assert "Stop" in settings["hooks"]
    assert "SessionStart" not in settings["hooks"]


def test_find_with_matcher_does_not_match_wrong_matcher(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    # Hand-write a SessionStart entry under a DIFFERENT matcher.
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {"type": "command", "command": "nightly hook session-start"}
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    # Looking for matcher="compact" must NOT find the startup entry.
    assert (
        find_nested_hook_index(
            read_settings(path),
            event_name="SessionStart",
            command="nightly hook session-start",
            matcher="compact",
        )
        is None
    )
    # And merging the compact one adds a second, independent block.
    assert merge_nested_hook(_session_start(path)) is True
    assert len(read_settings(path)["hooks"]["SessionStart"]) == 2


def test_matcherless_find_ignores_block_matcher(tmp_path: Path) -> None:
    """Back-compat: a matcher-less lookup keys purely on event+command."""
    path = tmp_path / "settings.json"
    merge_nested_hook(_stop(path))
    found = find_nested_hook_index(
        read_settings(path), event_name="Stop", command="nightly hook stop"
    )
    assert found == (0, 0)
