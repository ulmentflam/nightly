"""The cascade must not re-pick RFC items finished on an unmerged branch.

Regression test for a livelock observed for seven consecutive turn
boundaries: the `accepted_rfc` ranker only knew about work in *open PRs*,
so an item completed and committed on a local branch — because the push
failed, credentials expired, or nobody had pushed yet — stayed unchecked
on `main` and got handed to the agent again every single turn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightly_core.cascade import _items_done_on_local_branches

RFC_REL = ".planning/rfcs/007-example.md"

RFC_OPEN = """---
status: accepted
---

# RFC 007

- [ ] A1. First item
- [ ] A2. Second item
"""

RFC_A1_DONE = """---
status: accepted
---

# RFC 007

- [x] A1. First item
- [ ] A2. Second item
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    rfc = tmp_path / RFC_REL
    rfc.parent.mkdir(parents=True, exist_ok=True)
    rfc.write_text(RFC_OPEN, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def _branch_with(repo: Path, name: str, content: str) -> None:
    _git(repo, "checkout", "-q", "-b", name)
    (repo / RFC_REL).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"work on {name}")
    _git(repo, "checkout", "-q", "main")


def test_nothing_done_when_no_branches_exist(repo: Path) -> None:
    assert _items_done_on_local_branches(repo / RFC_REL, repo) == set()


def test_item_ticked_on_a_nightly_branch_is_detected(repo: Path) -> None:
    """The core regression: committed-but-unpushed work must count."""
    _branch_with(repo, "nightly/rfc007-a1", RFC_A1_DONE)
    done = _items_done_on_local_branches(repo / RFC_REL, repo)
    assert "A1. First item" in done
    assert "A2. Second item" not in done


def test_non_nightly_branches_are_ignored(repo: Path) -> None:
    """Only Nightly's own branches signal Nightly's own work."""
    _branch_with(repo, "feature/someone-elses-work", RFC_A1_DONE)
    assert _items_done_on_local_branches(repo / RFC_REL, repo) == set()


def test_items_are_unioned_across_branches(repo: Path) -> None:
    rfc_a2_done = RFC_OPEN.replace("- [ ] A2.", "- [x] A2.")
    _branch_with(repo, "nightly/one", RFC_A1_DONE)
    _branch_with(repo, "nightly/two", rfc_a2_done)
    done = _items_done_on_local_branches(repo / RFC_REL, repo)
    assert done == {"A1. First item", "A2. Second item"}


def test_branch_without_the_rfc_file_is_skipped(repo: Path) -> None:
    """A branch cut before the RFC existed must not raise or poison."""
    _git(repo, "checkout", "-q", "-b", "nightly/unrelated")
    (repo / RFC_REL).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "remove rfc")
    _git(repo, "checkout", "-q", "main")
    assert _items_done_on_local_branches(repo / RFC_REL, repo) == set()


def test_path_outside_the_repo_yields_nothing(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.md"
    assert _items_done_on_local_branches(outside, repo) == set()


def test_non_git_directory_degrades_to_empty(tmp_path: Path) -> None:
    """No git, no signal — fall back to today's behavior, don't skip all."""
    rfc = tmp_path / RFC_REL
    rfc.parent.mkdir(parents=True, exist_ok=True)
    rfc.write_text(RFC_OPEN, encoding="utf-8")
    assert _items_done_on_local_branches(rfc, tmp_path) == set()


def test_uppercase_checkbox_counts_as_done(repo: Path) -> None:
    _branch_with(repo, "nightly/upper", RFC_OPEN.replace("- [ ] A1.", "- [X] A1."))
    assert "A1. First item" in _items_done_on_local_branches(repo / RFC_REL, repo)
