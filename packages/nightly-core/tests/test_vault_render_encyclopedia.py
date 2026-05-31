"""Tests for `nightly_core.vault.render_encyclopedia`."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nightly_core.vault import project_run, vault_root_for
from nightly_core.vault.index import rebuild as rebuild_index
from nightly_core.vault.render_encyclopedia import render

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vault_sample_run"
SAMPLE_RUN_ID = "2026-05-27T16-30-35Z"


@pytest.fixture
def indexed_vault(tmp_path: Path) -> Path:
    runs_dir = tmp_path / ".nightly" / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(FIXTURE_DIR, runs_dir / SAMPLE_RUN_ID)
    project_run(SAMPLE_RUN_ID, repo_root=tmp_path)
    rebuild_index(vault_root_for(tmp_path))
    return tmp_path


def test_render_emits_one_page_per_node(indexed_vault: Path):
    result = render(vault_root_for(indexed_vault))
    # 1 run + 2 tasks + 3 lessons = 6 pages
    assert result.pages_written == 6
    assert result.index_path.is_file()

    site = result.site_root
    assert (site / "runs" / f"{SAMPLE_RUN_ID}.html").is_file()
    assert (site / "tasks" / f"{SAMPLE_RUN_ID}--0001-sample-task-one.html").is_file()
    assert (site / "lessons" / f"{SAMPLE_RUN_ID}--1.html").is_file()


def test_render_copies_assets(indexed_vault: Path):
    result = render(vault_root_for(indexed_vault))
    assert (result.site_root / "assets" / "style.css").is_file()


def test_node_page_contains_title_kind_status(indexed_vault: Path):
    render(vault_root_for(indexed_vault))
    task_html = (
        vault_root_for(indexed_vault)
        / "_site"
        / "tasks"
        / f"{SAMPLE_RUN_ID}--0001-sample-task-one.html"
    ).read_text(encoding="utf-8")
    assert "Task 0001" in task_html
    assert "kind-task" in task_html
    assert ">done<" in task_html


def test_node_page_contains_backlinks_section(indexed_vault: Path):
    render(vault_root_for(indexed_vault))
    run_html = (
        vault_root_for(indexed_vault) / "_site" / "runs" / f"{SAMPLE_RUN_ID}.html"
    ).read_text(encoding="utf-8")
    # Run is referenced by tasks and lessons via `parent` edges.
    assert "Referenced by" in run_html
    assert "parent" in run_html


def test_index_page_lists_all_kinds(indexed_vault: Path):
    render(vault_root_for(indexed_vault))
    index_html = (vault_root_for(indexed_vault) / "_site" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Vault" in index_html
    # All kinds present in our fixture should appear
    for kind_dir in ("runs", "tasks", "lessons"):
        assert kind_dir in index_html


def test_wiki_link_resolves_to_existing_node(tmp_path: Path):
    """A `[[task/x]]` in a body becomes an <a> to the right page."""
    vault = tmp_path / ".nightly" / "vault"
    (vault / "tasks").mkdir(parents=True)
    (vault / "tasks" / "x.md").write_text(
        "---\nid: task/x\nkind: task\ntitle: X\n---\nXyz\n", encoding="utf-8"
    )
    (vault / "tasks" / "y.md").write_text(
        "---\nid: task/y\nkind: task\ntitle: Y\n---\nLinks to [[task/x]] here.\n",
        encoding="utf-8",
    )
    rebuild_index(vault)
    render(vault)

    y_html = (vault / "_site" / "tasks" / "y.html").read_text(encoding="utf-8")
    assert '<a href="../tasks/x.html">X</a>' in y_html


def test_wiki_link_dangling_renders_with_class(tmp_path: Path):
    vault = tmp_path / ".nightly" / "vault"
    (vault / "tasks").mkdir(parents=True)
    (vault / "tasks" / "y.md").write_text(
        "---\nid: task/y\nkind: task\n---\nLinks to [[task/nonexistent]].\n",
        encoding="utf-8",
    )
    rebuild_index(vault)
    render(vault)

    y_html = (vault / "_site" / "tasks" / "y.html").read_text(encoding="utf-8")
    assert 'class="dangling"' in y_html
    assert "task/nonexistent" in y_html


def test_render_raises_without_index(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(FileNotFoundError):
        render(vault)
