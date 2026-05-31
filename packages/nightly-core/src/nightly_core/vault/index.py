"""SQLite indexer over the markdown vault — derived read-cache.

`rebuild(vault_root)` walks `vault/**/*.md`, parses each node's
frontmatter via `yaml.safe_load`, and writes the nodes + edges to
`<vault>/_index.db`. The DB is dropped and recreated on every rebuild —
cheap at the scales Nightly operates at (a vault with 10k nodes still
builds in well under a second) and keeps the indexer logic trivial.

Per RFC 003 Fork 05, the DB is canonical for *nothing*: markdown is the
source of truth; SQLite is a query accelerator. Drop the file any time;
the next rebuild re-derives it.

The indexer is deliberately best-effort: malformed frontmatter on a
single file skips that file rather than aborting the whole rebuild.
Dangling edge targets (a `derived_from` that points at a non-existent
node) are materialized as placeholder rows with `kind = "unknown"` so
graph queries stay connected.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .model import EDGE_TYPES, NODE_KINDS, EdgeType, node_kind_dir

__all__ = [
    "INDEX_DB_NAME",
    "INDEX_SCHEMA_VERSION",
    "IndexStats",
    "rebuild",
]


INDEX_DB_NAME = "_index.db"
"""Filename of the index DB, under `vault_root`."""

INDEX_SCHEMA_VERSION = 1
"""Schema version stamped on the DB via `PRAGMA user_version`. Bump when
the schema changes; the indexer drops and recreates on every rebuild so
no migration logic is required."""


@dataclass(frozen=True)
class IndexStats:
    """Summary of one `rebuild()` pass.

    `placeholder_count` is the number of dangling-target nodes that were
    materialized as `kind = "unknown"`. A non-zero value points at vault
    files that reference nodes that haven't been written yet.
    """

    db_path: Path
    node_count: int
    edge_count: int
    placeholder_count: int


def rebuild(vault_root: Path) -> IndexStats:
    """Drop and recreate `_index.db` under `vault_root` from the markdown vault.

    The vault root must exist; if it doesn't contain any vault files yet,
    the DB still gets created (empty), which keeps the read path simple
    for renderers — they can always `SELECT FROM nodes` without checking.
    """
    vault_root.mkdir(parents=True, exist_ok=True)
    db_path = vault_root / INDEX_DB_NAME
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        _create_schema(conn)

        nodes_seen: dict[str, _NodeRow] = {}
        edges: list[_EdgeRow] = []
        for md_path in sorted(vault_root.rglob("*.md")):
            if not _is_vault_node_file(vault_root, md_path):
                continue
            meta = _parse_vault_frontmatter(md_path)
            if meta is None:
                continue
            row = _node_row_from_meta(meta, vault_root, md_path)
            if row is None:
                continue
            nodes_seen[row.id] = row
            edges.extend(_extract_edges(row.id, meta))

        placeholder_count = _insert_placeholders(nodes_seen, edges)

        conn.executemany(
            "INSERT INTO nodes (id, kind, title, status, created, updated, tags, data, body_path)"
            " VALUES (:id, :kind, :title, :status, :created, :updated, :tags, :data, :body_path)",
            [_row_as_dict(r) for r in nodes_seen.values()],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO edges (src_id, dst_id, edge_type)"
            " VALUES (:src_id, :dst_id, :edge_type)",
            [{"src_id": e.src_id, "dst_id": e.dst_id, "edge_type": e.edge_type} for e in edges],
        )
        conn.commit()

        return IndexStats(
            db_path=db_path,
            node_count=len(nodes_seen),
            edge_count=len(edges),
            placeholder_count=placeholder_count,
        )
    finally:
        conn.close()


# ── internals ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _NodeRow:
    id: str
    kind: str
    title: str | None
    status: str | None
    created: str | None
    updated: str | None
    tags: str  # json-encoded array
    data: str  # json-encoded object
    body_path: str | None


@dataclass(frozen=True)
class _EdgeRow:
    src_id: str
    dst_id: str
    edge_type: EdgeType


_KIND_DIRS: frozenset[str] = frozenset(node_kind_dir(k) for k in NODE_KINDS)
"""The vault subdirectories the indexer walks. Anything else (including
`_site/`, `_dashboard/`, `_index.db`, `vault-manifest.json`) is ignored."""


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        PRAGMA user_version = {INDEX_SCHEMA_VERSION};

        CREATE TABLE nodes (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            title       TEXT,
            status      TEXT,
            created     TEXT,
            updated     TEXT,
            tags        TEXT,
            data        TEXT,
            body_path   TEXT
        );

        CREATE TABLE edges (
            src_id      TEXT NOT NULL,
            dst_id      TEXT NOT NULL,
            edge_type   TEXT NOT NULL,
            PRIMARY KEY (src_id, dst_id, edge_type)
        );

        CREATE INDEX idx_nodes_kind_status ON nodes(kind, status);
        CREATE INDEX idx_nodes_created     ON nodes(created);
        CREATE INDEX idx_edges_dst         ON edges(dst_id, edge_type);
        """
    )


def _is_vault_node_file(vault_root: Path, md_path: Path) -> bool:
    try:
        rel = md_path.relative_to(vault_root)
    except ValueError:
        return False
    if not rel.parts:
        return False
    head = rel.parts[0]
    if head.startswith("_") or head.startswith("."):
        return False
    return head in _KIND_DIRS


def _parse_vault_frontmatter(path: Path) -> dict[str, Any] | None:
    """Return the YAML frontmatter mapping at the top of `path`, or `None`
    if the file has no fence or the fence doesn't parse to a mapping.

    Malformed YAML is logged-by-skip: we want one bad file to not stop a
    rebuild over thousands of good ones.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        end = text.find("\n---", 4)
        if end == -1:
            return None
    header = text[4:end]
    try:
        parsed = yaml.safe_load(header)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _node_row_from_meta(meta: dict[str, Any], vault_root: Path, md_path: Path) -> _NodeRow | None:
    node_id = meta.get("id")
    kind = meta.get("kind")
    if not isinstance(node_id, str) or not isinstance(kind, str):
        return None

    tags_raw = meta.get("tags") or []
    tags = tags_raw if isinstance(tags_raw, list) else []
    data_raw = meta.get("data") or {}
    data = data_raw if isinstance(data_raw, dict) else {}

    try:
        body_path = str(md_path.relative_to(vault_root))
    except ValueError:
        body_path = str(md_path)

    return _NodeRow(
        id=node_id,
        kind=kind,
        title=_str_or_none(meta.get("title")),
        status=_str_or_none(meta.get("status")),
        created=_str_or_none(meta.get("created")),
        updated=_str_or_none(meta.get("updated")),
        tags=json.dumps(tags),
        data=json.dumps(data, default=str),
        body_path=body_path,
    )


def _extract_edges(src_id: str, meta: dict[str, Any]) -> list[_EdgeRow]:
    edges: list[_EdgeRow] = []
    for edge_type in EDGE_TYPES:
        value = meta.get(edge_type)
        if value is None:
            continue
        if isinstance(value, str):
            edges.append(_EdgeRow(src_id, value, edge_type))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    edges.append(_EdgeRow(src_id, item, edge_type))
    return edges


def _insert_placeholders(
    nodes: dict[str, _NodeRow],
    edges: list[_EdgeRow],
) -> int:
    """Materialize a placeholder row for every dangling edge target.

    A "dangling" target is an edge `dst_id` that no walked vault file
    claimed. We insert it as `kind = "unknown"` so renderers can still
    traverse the graph; the operator sees these as missing nodes in the
    UI and can decide whether to author them.
    """
    count = 0
    referenced = {e.dst_id for e in edges}
    for dst in referenced:
        if dst in nodes:
            continue
        nodes[dst] = _NodeRow(
            id=dst,
            kind="unknown",
            title=None,
            status=None,
            created=None,
            updated=None,
            tags="[]",
            data="{}",
            body_path=None,
        )
        count += 1
    return count


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)


def _row_as_dict(row: _NodeRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "status": row.status,
        "created": row.created,
        "updated": row.updated,
        "tags": row.tags,
        "data": row.data,
        "body_path": row.body_path,
    }
