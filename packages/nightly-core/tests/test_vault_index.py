"""Tests for `nightly_core.vault.index` — SQLite indexer rebuild."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from nightly_core.vault import project_run, vault_root_for
from nightly_core.vault.index import (
    INDEX_DB_NAME,
    INDEX_SCHEMA_VERSION,
    rebuild,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vault_sample_run"
SAMPLE_RUN_ID = "2026-05-27T16-30-35Z"


@pytest.fixture
def projected_vault(tmp_path: Path) -> Path:
    """A repo where the sample fixture has been projected into the vault."""
    runs_dir = tmp_path / ".nightly" / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(FIXTURE_DIR, runs_dir / SAMPLE_RUN_ID)
    project_run(SAMPLE_RUN_ID, repo_root=tmp_path)
    return tmp_path


def test_rebuild_creates_db_with_expected_schema(projected_vault: Path):
    vault = vault_root_for(projected_vault)
    stats = rebuild(vault)

    assert stats.db_path == vault / INDEX_DB_NAME
    assert stats.db_path.is_file()

    conn = sqlite3.connect(stats.db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == INDEX_SCHEMA_VERSION

        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"nodes", "edges"} <= tables
    finally:
        conn.close()


def test_rebuild_inserts_run_task_and_lesson_nodes(projected_vault: Path):
    vault = vault_root_for(projected_vault)
    stats = rebuild(vault)

    # 1 run + 2 tasks + 3 lessons = 6 nodes; no dangling references.
    assert stats.node_count == 6
    assert stats.placeholder_count == 0

    conn = sqlite3.connect(stats.db_path)
    try:
        counts = dict(conn.execute("SELECT kind, COUNT(*) FROM nodes GROUP BY kind").fetchall())
        assert counts == {"run": 1, "task": 2, "lesson": 3}
    finally:
        conn.close()


def test_rebuild_emits_parent_and_spawned_edges(projected_vault: Path):
    vault = vault_root_for(projected_vault)
    stats = rebuild(vault)

    # parent edges: 2 task→run + 3 lesson→run = 5
    # spawned edges: 1 run→task per task = 2
    # Total: 7
    assert stats.edge_count == 7

    conn = sqlite3.connect(stats.db_path)
    try:
        edge_counts = dict(
            conn.execute("SELECT edge_type, COUNT(*) FROM edges GROUP BY edge_type").fetchall()
        )
        assert edge_counts == {"parent": 5, "spawned": 2}
    finally:
        conn.close()


def test_rebuild_handles_empty_vault(tmp_path: Path):
    vault = tmp_path / "empty_vault"
    stats = rebuild(vault)
    assert stats.node_count == 0
    assert stats.edge_count == 0
    assert stats.placeholder_count == 0
    assert (vault / INDEX_DB_NAME).is_file()


def test_rebuild_inserts_placeholder_for_dangling_target(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "tasks").mkdir(parents=True)
    (vault / "tasks" / "x.md").write_text(
        "---\nid: task/x\nkind: task\ntitle: dangler\nderived_from: [issue/ghost]\n---\nbody\n",
        encoding="utf-8",
    )

    stats = rebuild(vault)

    assert stats.node_count == 2  # task/x + placeholder for issue/ghost
    assert stats.edge_count == 1
    assert stats.placeholder_count == 1

    conn = sqlite3.connect(stats.db_path)
    try:
        row = conn.execute("SELECT kind FROM nodes WHERE id = ?", ("issue/ghost",)).fetchone()
        assert row[0] == "unknown"
    finally:
        conn.close()


def test_rebuild_skips_malformed_frontmatter(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "tasks").mkdir(parents=True)
    (vault / "tasks" / "good.md").write_text(
        "---\nid: task/good\nkind: task\n---\nok\n", encoding="utf-8"
    )
    (vault / "tasks" / "bad.md").write_text(
        "---\nnot: [a, valid: yaml\n---\nbroken\n", encoding="utf-8"
    )
    (vault / "tasks" / "no_fence.md").write_text("no frontmatter at all\n", encoding="utf-8")

    stats = rebuild(vault)
    assert stats.node_count == 1  # only the good one


def test_rebuild_skips_reserved_directories(tmp_path: Path):
    """Files under `_site/` and `_dashboard/` are renderer artifacts, not nodes."""
    vault = tmp_path / "vault"
    for sub in ("tasks", "_site/runs", "_dashboard"):
        (vault / sub).mkdir(parents=True)

    (vault / "tasks" / "real.md").write_text(
        "---\nid: task/real\nkind: task\n---\n", encoding="utf-8"
    )
    (vault / "_site" / "runs" / "fake.md").write_text(
        "---\nid: task/fake-site\nkind: task\n---\n", encoding="utf-8"
    )
    (vault / "_dashboard" / "fake.md").write_text(
        "---\nid: task/fake-dash\nkind: task\n---\n", encoding="utf-8"
    )

    stats = rebuild(vault)
    assert stats.node_count == 1


def test_rebuild_perf_budget(tmp_path: Path):
    """Build a 2000-node synthetic vault. Should rebuild well under a second
    on a modern laptop; we use a generous 5s ceiling here so iCloud-backed
    test runs don't flake."""
    vault = tmp_path / "vault"
    (vault / "tasks").mkdir(parents=True)
    for i in range(2000):
        (vault / "tasks" / f"t{i}.md").write_text(
            f"---\nid: task/t{i}\nkind: task\ntitle: Task {i}\n---\n",
            encoding="utf-8",
        )

    start = time.perf_counter()
    stats = rebuild(vault)
    elapsed = time.perf_counter() - start

    assert stats.node_count == 2000
    assert elapsed < 5.0, f"indexer too slow: {elapsed:.2f}s for 2000 nodes"
