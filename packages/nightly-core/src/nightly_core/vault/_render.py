"""Markdown rendering for vault nodes — frontmatter + body.

Hand-rolled rather than using `yaml.safe_dump` so the on-disk shape is
predictable (consistent key order, no trailing whitespace, scalar vs.
list shape chosen per edge type). The indexer (`index.py`) uses
`yaml.safe_load` to parse vault frontmatter, so the round-trip works
even if a human hand-edits a node.
"""

from __future__ import annotations

from typing import Any

from .model import EDGE_TYPES, Node, is_singular_edge

__all__ = ["render_node"]


_SCALAR_NEEDS_QUOTING = set(":#[]{},&*!|>'\"%@`")
_RESERVED_BOOL_LIKE = frozenset({"null", "true", "false", "yes", "no", "on", "off"})


def render_node(node: Node) -> str:
    """Render a `Node` to its on-disk markdown form (frontmatter + body).

    Trailing newline is always present. Body is stripped and re-trailed so
    the rendering is idempotent across re-projections.
    """
    lines: list[str] = ["---"]
    lines.append(f"id: {node.id}")
    lines.append(f"kind: {node.kind}")
    lines.append(f"title: {_render_scalar(node.title)}")
    lines.append(f"status: {_render_scalar(node.status)}")
    lines.append(f"created: {_render_scalar(node.created)}")
    lines.append(f"updated: {_render_scalar(node.updated)}")
    lines.append(f"tags: {_render_list(node.tags)}")

    for edge_type in EDGE_TYPES:
        dst_ids = node.edges.get(edge_type, ())
        if is_singular_edge(edge_type):
            value = dst_ids[0] if dst_ids else None
            lines.append(f"{edge_type}: {_render_scalar(value)}")
        else:
            lines.append(f"{edge_type}: {_render_list(dst_ids)}")

    if node.data:
        lines.append("data:")
        for key in sorted(node.data):
            lines.append(f"  {key}: {_render_scalar(node.data[key])}")

    lines.append("---")
    body = node.body.strip()
    if body:
        return "\n".join(lines) + "\n\n" + body + "\n"
    return "\n".join(lines) + "\n"


def _render_scalar(value: Any) -> str:
    """Render a single YAML scalar. None → `null`; bool → `true`/`false`;
    int → integer literal; everything else stringified and quoted only
    when ambiguous chars would otherwise change the parse."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    if _scalar_needs_quoting(s):
        return f'"{_escape_double_quotes(s)}"'
    return s


def _scalar_needs_quoting(s: str) -> bool:
    """True if the string would parse ambiguously when emitted unquoted."""
    if not s:
        return True
    if s.lower() in _RESERVED_BOOL_LIKE:
        return True
    if s[0].isdigit() and not _is_simple_token(s):
        return True
    if any(c in _SCALAR_NEEDS_QUOTING for c in s):
        return True
    return s != s.strip()


def _render_list(items: tuple[str, ...]) -> str:
    if not items:
        return "[]"
    rendered = [_render_scalar(item) for item in items]
    return "[" + ", ".join(rendered) + "]"


def _escape_double_quotes(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _is_simple_token(s: str) -> bool:
    return all(c.isalnum() or c in "-._" for c in s)
