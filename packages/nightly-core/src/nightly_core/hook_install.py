"""Shared host-config merge helpers for the Stop-hook keep-alive.

Three of the four hosts with forced keep-alive (Claude Code, Codex CLI,
Antigravity/Gemini CLI) all use the same nested JSON shape for their
hook entries:

    {
      "hooks": {
        "<EventName>": [
          {
            "matcher": "",
            "hooks": [
              { "type": "command", "command": "nightly hook stop ..." }
            ]
          }
        ]
      }
    }

Cursor uses a different (flatter) shape and isn't covered here. Each
host's integration calls `merge_nested_hook` / `remove_nested_hook` /
`find_nested_hook_index` with its event name (Claude+Codex: "Stop",
Gemini/Antigravity: "AfterAgent") and the exact command string that
identifies its Nightly entry.

All operations are idempotent and preserve unrelated entries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CURSOR_HOOK_DEFAULT_LOOP_LIMIT",
    "CursorHookFile",
    "HookFile",
    "find_cursor_hook_index",
    "find_nested_hook_index",
    "merge_cursor_hook",
    "merge_nested_hook",
    "merge_settings_env",
    "read_settings",
    "remove_cursor_hook",
    "remove_nested_hook",
    "remove_settings_env",
    "write_settings",
]


CURSOR_HOOK_DEFAULT_LOOP_LIMIT = 500
"""Cursor caps auto-continue iterations at `loop_limit` per hook entry
(default 5). For Nightly we want a much higher cap because the cascade
will naturally run out of work and emit `{}` long before this triggers
— matches the MAX_TURNS=500 safety cap in keepalive_hook.py."""


@dataclass(frozen=True)
class HookFile:
    """A host's hook config — file path + the event name it nests under.

    `event_name` is the host-specific key under `hooks`: Claude Code and
    Codex use `Stop`; Gemini CLI / Antigravity use `AfterAgent`. The
    `command` is the exact string used both to write the new entry and
    to find an existing one on subsequent installs / uninstalls.
    """

    path: Path
    event_name: str
    command: str


def read_settings(path: Path) -> dict[str, Any]:
    """Parse the settings JSON. Empty file → `{}`. Non-dict root → `{}`.

    Raises `json.JSONDecodeError` on malformed JSON so callers can
    decide whether to skip (default) or surface the error.
    """
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def write_settings(path: Path, settings: dict[str, Any]) -> None:
    """Atomically replace the settings file with `settings` as pretty JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def find_nested_hook_index(
    settings: dict[str, Any],
    *,
    event_name: str,
    command: str,
) -> tuple[int, int] | None:
    """Locate an existing `command` entry under `hooks.<event_name>[*].hooks[*]`.

    Returns `(block_idx, entry_idx)` so callers can splice it out. Returns
    `None` when no matching entry exists.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return None
    event_block = hooks.get(event_name)
    if not isinstance(event_block, list):
        return None
    for block_idx, block in enumerate(event_block):
        if not isinstance(block, dict):
            continue
        entries = block.get("hooks")
        if not isinstance(entries, list):
            continue
        for entry_idx, entry in enumerate(entries):
            if (
                isinstance(entry, dict)
                and entry.get("type") == "command"
                and entry.get("command") == command
            ):
                return (block_idx, entry_idx)
    return None


def merge_nested_hook(hook: HookFile) -> bool:
    """Add the hook entry to its settings file. Returns True iff anything changed.

    Idempotent: if the exact entry is already present, this is a no-op
    (returns False). Preserves all unrelated keys. If the file is
    malformed JSON, returns False without modifying the file — the
    caller should report the situation (or not) per host policy.
    """
    try:
        settings = read_settings(hook.path)
    except json.JSONDecodeError:
        return False
    if find_nested_hook_index(settings, event_name=hook.event_name, command=hook.command):
        return False
    hooks_root = settings.setdefault("hooks", {})
    event_block = hooks_root.setdefault(hook.event_name, [])
    event_block.append(
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": hook.command}],
        }
    )
    write_settings(hook.path, settings)
    return True


def remove_nested_hook(hook: HookFile) -> bool:
    """Remove the hook entry. Trim empty parents; delete file when fully empty.

    Returns True iff anything changed. Idempotent — calling on a file
    that doesn't contain the entry is a no-op.
    """
    if not hook.path.is_file():
        return False
    try:
        settings = read_settings(hook.path)
    except json.JSONDecodeError:
        return False
    found = find_nested_hook_index(settings, event_name=hook.event_name, command=hook.command)
    if found is None:
        return False
    block_idx, entry_idx = found
    event_block = settings["hooks"][hook.event_name]
    del event_block[block_idx]["hooks"][entry_idx]
    if not event_block[block_idx]["hooks"]:
        del event_block[block_idx]
    if not settings["hooks"][hook.event_name]:
        del settings["hooks"][hook.event_name]
    if not settings["hooks"]:
        del settings["hooks"]
    if not settings:
        hook.path.unlink()
        return True
    write_settings(hook.path, settings)
    return True


# ── session env vars (Claude Code top-level `env`) ────────────────────────


def merge_settings_env(path: Path, key: str, value: str) -> bool:
    """Idempotently set `env.<key> = value` in a settings file.

    Claude Code applies the top-level `"env"` object to the session
    environment. Used to pin `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` high so an
    overnight forced-continuation chain never trips the host's
    without-progress override.

    Returns True iff anything changed. Preserves all other content;
    creates the file and the `env` object as needed. Malformed JSON is
    left untouched (returns False) — same policy as `merge_nested_hook`.
    """
    try:
        settings = read_settings(path)
    except json.JSONDecodeError:
        return False
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
        settings["env"] = env
    if env.get(key) == value:
        return False
    env[key] = value
    write_settings(path, settings)
    return True


def remove_settings_env(path: Path, key: str, *, only_if_value: str | None = None) -> bool:
    """Remove `env.<key>` from a settings file. Trim an emptied `env` object.

    When `only_if_value` is given, the key is deleted only if its current
    value matches — so uninstalling never clobbers an operator's custom
    override of the same key. Returns True iff anything changed.
    Idempotent; deletes the file if it becomes fully empty.
    """
    if not path.is_file():
        return False
    try:
        settings = read_settings(path)
    except json.JSONDecodeError:
        return False
    env = settings.get("env")
    if not isinstance(env, dict) or key not in env:
        return False
    if only_if_value is not None and env.get(key) != only_if_value:
        return False
    del env[key]
    if not env:
        del settings["env"]
    if not settings:
        path.unlink()
        return True
    write_settings(path, settings)
    return True


# ── Cursor flat shape ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class CursorHookFile:
    """Cursor's hooks.json uses a flatter shape than Claude / Codex / Gemini.

    The entry sits directly under `hooks.<event>[*]` as
    `{"command": "...", "loop_limit": N}` — no `matcher` / no nested
    `hooks: []` array. Reference: https://cursor.com/docs/hooks
    """

    path: Path
    event_name: str
    command: str
    loop_limit: int = CURSOR_HOOK_DEFAULT_LOOP_LIMIT


def find_cursor_hook_index(
    settings: dict[str, Any],
    *,
    event_name: str,
    command: str,
) -> int | None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return None
    event_block = hooks.get(event_name)
    if not isinstance(event_block, list):
        return None
    for idx, entry in enumerate(event_block):
        if isinstance(entry, dict) and entry.get("command") == command:
            return idx
    return None


def merge_cursor_hook(hook: CursorHookFile) -> bool:
    try:
        settings = read_settings(hook.path)
    except json.JSONDecodeError:
        return False
    if (
        find_cursor_hook_index(settings, event_name=hook.event_name, command=hook.command)
        is not None
    ):
        return False
    # Cursor's hooks.json declares a top-level `version: 1` per the docs.
    # We preserve any existing version, default to 1 when absent.
    settings.setdefault("version", 1)
    hooks_root = settings.setdefault("hooks", {})
    event_block = hooks_root.setdefault(hook.event_name, [])
    event_block.append({"command": hook.command, "loop_limit": hook.loop_limit})
    write_settings(hook.path, settings)
    return True


def remove_cursor_hook(hook: CursorHookFile) -> bool:
    if not hook.path.is_file():
        return False
    try:
        settings = read_settings(hook.path)
    except json.JSONDecodeError:
        return False
    idx = find_cursor_hook_index(settings, event_name=hook.event_name, command=hook.command)
    if idx is None:
        return False
    event_block = settings["hooks"][hook.event_name]
    del event_block[idx]
    if not settings["hooks"][hook.event_name]:
        del settings["hooks"][hook.event_name]
    if not settings["hooks"]:
        del settings["hooks"]
    # If only `version` remains, that's still empty for our purposes — drop it.
    if set(settings.keys()) <= {"version"}:
        hook.path.unlink()
        return True
    write_settings(hook.path, settings)
    return True
