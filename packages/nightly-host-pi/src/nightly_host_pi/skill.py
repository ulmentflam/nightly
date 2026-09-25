"""Loaders for the pi assets shipped in this package.

Two artifacts, not one. Every other host package ships only markdown;
pi additionally ships `keepalive.ts`, the extension that replaces the
Stop-hook script the other hosts install into settings JSON.

The extension is rendered rather than copied verbatim: the version marker
on its first line is what `nightly doctor` compares against the installed
package to detect drift (RFC 013 §12), so it has to be stamped at load
time from `nightly_core._version`.
"""

from __future__ import annotations

from importlib.resources import files

from nightly_core._version import __version__

__all__ = [
    "KEEPALIVE_TS",
    "SKILL_MD",
    "VERSION_MARKER_PREFIX",
    "extension_version",
    "load_keepalive_ts",
    "load_skill_md",
    "parse_json_stream",
    "render_keepalive_ts",
]

VERSION_MARKER_PREFIX = "// nightly-keepalive v"
"""First line of the rendered extension.

`nightly doctor` cannot meaningfully parse TypeScript, so drift detection
is a single version token rather than a content check — the same trade the
`_REQUIRED_SKILL_TOKENS` table makes for markdown skills."""

_VERSION_PLACEHOLDER = "__NIGHTLY_VERSION__"


def load_skill_md() -> str:
    """Return the packaged SKILL.md as a string."""
    return files("nightly_host_pi").joinpath("skill.md").read_text(encoding="utf-8")


def load_keepalive_ts() -> str:
    """Return the raw extension source, version placeholder unsubstituted."""
    return files("nightly_host_pi").joinpath("keepalive.ts").read_text(encoding="utf-8")


def render_keepalive_ts(version: str | None = None) -> str:
    """Return the extension source with its version marker stamped."""
    return load_keepalive_ts().replace(_VERSION_PLACEHOLDER, version or __version__)


def extension_version(source: str) -> str | None:
    """Read the version marker out of an installed extension.

    Returns None when the marker is absent — an operator hand-edit, a
    truncated write, or a file that was never ours. All three mean the same
    thing to doctor: reinstall.
    """
    first_line = source.lstrip().split("\n", 1)[0]
    if not first_line.startswith(VERSION_MARKER_PREFIX):
        return None
    remainder = first_line[len(VERSION_MARKER_PREFIX) :].strip()
    return remainder.split()[0] if remainder else None


def parse_json_stream(raw: str) -> str:
    """Extract the assistant's text from a `pi --mode json` JSONL stream.

    Deliberately tolerant. The stream is versioned
    (`{"type":"session","version":3,...}` on line one) and pi is moving
    quickly, so unknown event types are skipped rather than treated as
    errors: a schema addition upstream should cost us nothing, and a
    breaking change should degrade to "returned no parsed output" rather
    than raising inside a background dispatch nobody is watching.

    Reads `agent_end`, whose `messages` carry the run's result. Falls back
    to accumulating `message_end` assistant text when `agent_end` is absent
    — which is what a stream truncated by a timeout looks like, and losing
    a partial answer there would discard the only evidence of what the
    specialist managed to do.
    """
    import json  # noqa: PLC0415

    final: list[str] = []
    streamed: list[str] = []

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        kind = event.get("type")
        if kind == "agent_end":
            for message in event.get("messages") or []:
                final.extend(_message_text(message))
        elif kind == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                streamed.extend(_message_text(message))

    chosen = final or streamed
    return "\n".join(part for part in chosen if part).strip()


def _message_text(message: object) -> list[str]:
    """Pull plain-text fragments out of one pi message.

    pi content blocks mirror the common `{"type":"text","text":...}` shape,
    but a bare string is also valid. Both are handled; anything else
    (images, tool calls) is skipped — this function exists to recover
    prose, not to reconstruct a transcript.
    """
    if not isinstance(message, dict):
        return []
    if message.get("role") not in (None, "assistant"):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                out.append(text)
    return out


SKILL_MD: str = load_skill_md()
"""The pi skill markdown — installed by PiHostIntegration.install."""

KEEPALIVE_TS: str = render_keepalive_ts()
"""The pi keep-alive extension, version-stamped for the running Nightly."""
