"""Vault graph types — node/edge schemas and globally-unique ID helpers.

The vault is a navigable knowledge graph over runs, tasks, dispatches, PRs,
feedback, and lessons. Every node has the same frontmatter envelope (id,
kind, title, status, created, updated, tags) with kind-specific extras in
`data` and directed edges to other nodes.

This module is intentionally typed-but-dumb: it defines the shape, not the
projection logic (`project.py`) or the indexer (`index.py`). All ID
generation flows through the helpers below so renames stay localized.

See RFC 003 (`.planning/rfcs/003-vault-knowledge-graph.md`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "EDGE_TYPES",
    "NODE_KINDS",
    "Edge",
    "EdgeType",
    "Node",
    "NodeKind",
    "node_id_for_dispatch",
    "node_id_for_feedback",
    "node_id_for_lesson",
    "node_id_for_pr",
    "node_id_for_run",
    "node_id_for_task",
    "node_kind_dir",
]


NodeKind = Literal["run", "task", "dispatch", "pr", "feedback", "lesson"]
"""The six node kinds in v0. Decision-grain (cascade picks as nodes) and
soft kinds (issue / rfc / uncertainty) deliberately omitted — RFC 003 §
'Resolved design decisions' #3 picks medium grain."""

EdgeType = Literal[
    "parent",
    "spawned",
    "derived_from",
    "produced",
    "references",
    "superseded_by",
]
"""Directed edge types. `parent` and `spawned` express the same relation
from opposite ends; the indexer stores both rows and renderers can use
either. `references` is a soft mention, distinct from `derived_from`
(causal lineage)."""

NODE_KINDS: tuple[NodeKind, ...] = (
    "run",
    "task",
    "dispatch",
    "pr",
    "feedback",
    "lesson",
)

EDGE_TYPES: tuple[EdgeType, ...] = (
    "parent",
    "spawned",
    "derived_from",
    "produced",
    "references",
    "superseded_by",
)

_SINGULAR_EDGES: frozenset[EdgeType] = frozenset({"parent", "superseded_by"})
"""Edges that point to at most one destination. The renderer emits them as
scalars (`parent: run/...` rather than `parent: [run/...]`) so the on-disk
shape stays human-readable."""


@dataclass(frozen=True)
class Edge:
    """A directed edge between two nodes, typed by `edge_type`.

    Used by the indexer (`index.py`) and renderer queries. The vault
    markdown itself stores edges as frontmatter keys on the source node;
    `Edge` instances are derived at index time.
    """

    src_id: str
    dst_id: str
    edge_type: EdgeType


@dataclass(frozen=True)
class Node:
    """A vault node — one markdown file under `.nightly/vault/<kind>s/`.

    `id` doubles as the URL slug for the rendered HTML and as the primary
    key in `_index.db`. It must be globally unique across the vault — use
    the `node_id_for_*` helpers below to construct one.

    `edges` maps edge type to a tuple of destination IDs. Singular edges
    (`parent`, `superseded_by`) carry zero or one destination; the rest
    can carry many.

    `data` holds kind-specific extras (e.g. a task's `proposer_fingerprint`,
    a PR's `number` and `ci`). The indexer JSON-encodes this into the
    `nodes.data` column.

    Frozen for identity safety; the mutable `data` / `edges` containers
    are not deep-copied. Callers should treat a Node as immutable after
    construction.
    """

    id: str
    kind: NodeKind
    title: str | None = None
    status: str | None = None
    created: str | None = None
    updated: str | None = None
    tags: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    edges: dict[EdgeType, tuple[str, ...]] = field(default_factory=dict)
    body: str = ""


# ── ID helpers ────────────────────────────────────────────────────────────
#
# Format: `<kind>/<stable-identifier>`. The kind prefix doubles as the
# directory under `vault/`. The stable identifier embeds enough context to
# stay unique across runs (task slugs aren't globally unique on their own,
# so they're prefixed with the run id).


def node_id_for_run(run_id: str) -> str:
    """ID for a run node. `run_id` is the ISO-with-dashes form used on
    disk, e.g. `2026-05-27T16-30-35Z`."""
    return f"run/{run_id}"


def node_id_for_task(run_id: str, slug: str) -> str:
    """ID for a task. Slug typically includes its index prefix
    (`0002-audit-todos`) and is unique within a run but not globally;
    we prefix with the run id to disambiguate across runs."""
    return f"task/{run_id}--{slug}"


def node_id_for_dispatch(run_id: str, slug: str, n: int) -> str:
    """ID for a specialist sub-agent dispatch made during a task.
    `n` is 1-based within the task."""
    return f"dispatch/{run_id}--{slug}--{n}"


def node_id_for_pr(pr_number: int) -> str:
    """ID for a PR. PRs are long-lived across runs and live in the
    operator's GitHub repo; the integer number is global."""
    return f"pr/{pr_number}"


def node_id_for_feedback(pr_number: int, sha: str) -> str:
    """ID for a single feedback event on a PR. `sha` is a short hash
    of the source (review id, CI run id, or comment id) — stable
    enough that re-ingesting the same event is idempotent."""
    return f"feedback/{pr_number}--{sha}"


def node_id_for_lesson(run_id: str, n: int) -> str:
    """ID for a lesson extracted from a run's `lessons.md`. `n` is 1-based
    by bullet order — stable as long as lessons.md isn't reordered."""
    return f"lesson/{run_id}--{n}"


def node_kind_dir(kind: NodeKind) -> str:
    """Directory under `vault/` where nodes of this kind live. Plural,
    short. Centralizing keeps the on-disk layout consistent."""
    return {
        "run": "runs",
        "task": "tasks",
        "dispatch": "dispatches",
        "pr": "pulls",
        "feedback": "feedback",
        "lesson": "lessons",
    }[kind]


def is_singular_edge(edge_type: EdgeType) -> bool:
    """Whether this edge type can point to at most one destination.
    Used by the renderer to choose scalar-vs-list YAML shape."""
    return edge_type in _SINGULAR_EDGES
