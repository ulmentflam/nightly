"""Tests for `project_pr()` and `backfill_prs()` in `nightly_core.vault.project`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from nightly_core.vault import vault_root_for
from nightly_core.vault.project import backfill_prs, project_pr

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vault_sample_run"
SAMPLE_RUN_ID = "2026-05-27T16-30-35Z"


def test_project_pr_writes_node_with_derived_from(tmp_path: Path):
    repo_root = tmp_path
    target = project_pr(
        pr_number=57,
        title="feat: foo",
        branch="nightly/task-foo",
        url="https://github.com/example/repo/pull/57",
        ci_state="passing",
        merge_state="open",
        source_task_id="task/2026-05-27T16-30-35Z--0001-foo",
        repo_root=repo_root,
    )
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "id: pr/57" in text
    assert "kind: pr" in text
    assert "derived_from: [task/2026-05-27T16-30-35Z--0001-foo]" in text
    # Numbers and URLs survive the renderer
    assert "number: 57" in text
    assert "ci: passing" in text


def test_project_pr_without_source_task_is_graph_isolated(tmp_path: Path):
    target = project_pr(
        pr_number=99,
        title="orphan",
        branch="nightly/something",
        url="https://example/99",
        repo_root=tmp_path,
    )
    text = target.read_text(encoding="utf-8")
    assert "derived_from: []" in text


def test_backfill_prs_returns_empty_when_gh_missing(tmp_path: Path, monkeypatch):
    """If `gh` isn't on PATH, backfill is a no-op."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    paths = backfill_prs(tmp_path)
    assert paths == []


def test_backfill_prs_writes_nodes_and_links_to_source_task(tmp_path: Path, monkeypatch):
    # Stage a fake run with a known task so the slug → task_id lookup resolves.
    runs_dir = tmp_path / ".nightly" / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(FIXTURE_DIR, runs_dir / SAMPLE_RUN_ID)

    # Stub `gh pr list` via subprocess.run patching at the module level.
    import nightly_core.vault.project as project_mod

    fake_payload = json.dumps(
        [
            {
                "number": 11,
                "title": "feat: sample-task-one",
                "headRefName": "nightly/0001-sample-task-one",
                "url": "https://example/11",
                "state": "OPEN",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [{"state": "SUCCESS"}],
            },
            {
                "number": 12,
                "title": "chore: nothing",
                "headRefName": "not-nightly/x",  # filtered out by prefix
                "url": "https://example/12",
                "state": "OPEN",
                "statusCheckRollup": [],
            },
        ]
    )

    class FakeCompleted:
        def __init__(self, stdout: str):
            self.stdout = stdout

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(project_mod.subprocess, "run", lambda *a, **kw: FakeCompleted(fake_payload))

    paths = backfill_prs(tmp_path)
    assert len(paths) == 1
    pr_md = vault_root_for(tmp_path) / "pulls" / "11.md"
    assert pr_md.is_file()
    text = pr_md.read_text(encoding="utf-8")
    # Derived_from points at the matching task
    assert f"derived_from: [task/{SAMPLE_RUN_ID}--0001-sample-task-one]" in text
    assert "ci: passing" in text


def test_backfill_prs_handles_gh_failure_gracefully(tmp_path: Path, monkeypatch):
    import nightly_core.vault.project as project_mod

    def boom(*_args, **_kwargs):
        raise project_mod.subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(project_mod.subprocess, "run", boom)

    paths = backfill_prs(tmp_path)
    assert paths == []
