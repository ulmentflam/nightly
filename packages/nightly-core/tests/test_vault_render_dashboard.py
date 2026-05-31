"""Tests for `nightly_core.vault.render_dashboard`.

We don't run a full headless browser here. Instead, we check the
structural contract: every asset the dashboard expects is on disk, the
HTML references them, and `vault.db` is a valid copy of the index.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from nightly_core.vault import project_run, vault_root_for
from nightly_core.vault.index import rebuild as rebuild_index
from nightly_core.vault.render_dashboard import render

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


def test_render_writes_all_expected_artifacts(indexed_vault: Path):
    result = render(vault_root_for(indexed_vault))

    root = result.dashboard_root
    expected = [
        "index.html",
        "app.js",
        "style.css",
        "cytoscape.min.js",
        "sql-wasm.js",
        "sql-wasm-inline.js",
        "vault.db",
    ]
    for name in expected:
        assert (root / name).is_file(), f"missing {name}"


def test_index_html_references_all_assets(indexed_vault: Path):
    result = render(vault_root_for(indexed_vault))
    html = result.index_path.read_text(encoding="utf-8")
    assert 'src="cytoscape.min.js"' in html
    assert 'src="sql-wasm-inline.js"' in html
    assert 'src="sql-wasm.js"' in html
    assert 'src="app.js"' in html
    assert 'href="style.css"' in html


def test_inline_wasm_is_base64_data_uri(indexed_vault: Path):
    result = render(vault_root_for(indexed_vault))
    inline = (result.dashboard_root / "sql-wasm-inline.js").read_text(encoding="utf-8")
    assert "data:application/wasm;base64," in inline
    assert "__VAULT_SQL_WASM_DATA__" in inline


def test_db_copy_is_valid_sqlite(indexed_vault: Path):
    result = render(vault_root_for(indexed_vault))
    conn = sqlite3.connect(result.db_copy)
    try:
        # 1 run + 2 tasks + 3 lessons projected.
        (count,) = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
        assert count == 6
    finally:
        conn.close()


def test_render_raises_without_index(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(FileNotFoundError):
        render(vault)
