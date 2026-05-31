"""Vault & knowledge graph — projection + indexer + renderers.

Public API:
- `build(repo_root)` — full pipeline; Phase A wires projection only.
- `project_run(run_id, repo_root=...)` — project one run into vault md.
- `vault_root_for(repo_root)` — canonical vault path.

The indexer (`index.py`) and renderers (`render_encyclopedia.py`,
`render_dashboard.py`) land in Phases B-D. `build()` calls them once
they exist; for now it projects every run it finds and writes the
manifest.

See RFC 003 (`.planning/rfcs/003-vault-knowledge-graph.md`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .index import IndexStats
from .index import rebuild as rebuild_index
from .manifest import write_manifest
from .model import Node
from .project import ProjectionResult, backfill_feedback, project_run, vault_root_for
from .render_dashboard import DashboardResult
from .render_dashboard import render as render_dashboard
from .render_encyclopedia import RenderResult
from .render_encyclopedia import render as render_encyclopedia

__all__ = [
    "BuildResult",
    "DashboardResult",
    "IndexStats",
    "Node",
    "ProjectionResult",
    "RenderResult",
    "backfill_feedback",
    "build",
    "project_run",
    "rebuild_index",
    "render_dashboard",
    "render_encyclopedia",
    "vault_root_for",
]


@dataclass(frozen=True)
class BuildResult:
    """The outcome of one `build()` call."""

    repo_root: Path
    vault_root: Path
    projections: tuple[ProjectionResult, ...]
    index_stats: IndexStats
    encyclopedia: RenderResult
    dashboard: DashboardResult
    manifest_path: Path

    @property
    def total_nodes(self) -> int:
        return sum(
            1 + len(p.task_nodes) + len(p.dispatch_nodes) + len(p.lesson_nodes)
            for p in self.projections
        )


def build(repo_root: Path) -> BuildResult:
    """Project every run in `<repo_root>/.nightly/runs/` into the vault,
    rebuild the SQLite index, render both targets, and write the manifest.
    Idempotent."""
    vault_root = vault_root_for(repo_root)
    runs_dir = repo_root / ".nightly" / "runs"

    projections: list[ProjectionResult] = []
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                projection = project_run(entry.name, repo_root=repo_root, vault_root=vault_root)
            except FileNotFoundError:
                continue
            projections.append(projection)

    all_nodes: list[Node] = []
    for projection in projections:
        all_nodes.append(projection.run_node)
        all_nodes.extend(projection.task_nodes)
        all_nodes.extend(projection.dispatch_nodes)
        all_nodes.extend(projection.lesson_nodes)

    index_stats = rebuild_index(vault_root)
    encyclopedia = render_encyclopedia(vault_root)
    dashboard = render_dashboard(vault_root)
    manifest_path = write_manifest(vault_root, nodes=all_nodes, run_count=len(projections))

    return BuildResult(
        repo_root=repo_root,
        vault_root=vault_root,
        projections=tuple(projections),
        index_stats=index_stats,
        encyclopedia=encyclopedia,
        dashboard=dashboard,
        manifest_path=manifest_path,
    )
