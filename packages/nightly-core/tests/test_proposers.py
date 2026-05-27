"""Tests for the Phase 5 proposer suite."""

from __future__ import annotations

from pathlib import Path

from nightly_core.proposers import (
    LintDebtProposer,
    Proposal,
    TodoFixmeProposer,
    TypeHoleProposer,
    default_proposers,
)

# ── TodoFixmeProposer ─────────────────────────────────────────────────────


def test_todo_fixme_returns_no_proposals_when_empty(tmp_path: Path) -> None:
    (tmp_path / "src.py").write_text("def f(): pass\n", encoding="utf-8")
    assert list(TodoFixmeProposer().propose(tmp_path)) == []


def test_todo_fixme_finds_markers_in_python(tmp_path: Path) -> None:
    (tmp_path / "src.py").write_text(
        "def f():\n    # TODO: handle the error\n    # FIXME: this is hacky\n    pass\n",
        encoding="utf-8",
    )
    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.proposer == "todo_fixme"
    assert p.category == "todo_audit"
    assert "TODO" in p.body
    assert "handle the error" in p.body
    assert "FIXME" in p.body
    assert "this is hacky" in p.body
    assert p.estimated_loc == 2  # two markers
    assert p.file_scope == ("src.py",)


def test_todo_fixme_groups_across_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("# TODO: thing one\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("// XXX: thing two\n// HACK: thing three\n", encoding="utf-8")
    # b.py is .py so the // comments still match — markers are token-based,
    # not language-aware. That's intentional for the audit's purpose.
    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert len(proposals) == 1
    p = proposals[0]
    assert "a.py" in p.file_scope
    assert "b.py" in p.file_scope
    # Score scales with file count
    assert p.score > 1.0


def test_todo_fixme_skips_ignored_dirs(tmp_path: Path) -> None:
    venv = tmp_path / ".venv" / "lib" / "site.py"
    venv.parent.mkdir(parents=True)
    venv.write_text("# TODO: should be ignored\n", encoding="utf-8")
    (tmp_path / "real.py").write_text("# TODO: real marker\n", encoding="utf-8")

    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert len(proposals) == 1
    assert "real.py" in proposals[0].file_scope
    assert ".venv" not in str(proposals[0].body)


def test_todo_fixme_skips_non_source_extensions(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("TODO: not in scope\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# TODO: not in scope\n", encoding="utf-8")
    assert list(TodoFixmeProposer().propose(tmp_path)) == []


def test_todo_fixme_score_caps(tmp_path: Path) -> None:
    # Generate 50 files with markers — score should cap, not run away.
    for i in range(50):
        (tmp_path / f"f{i:02}.py").write_text(f"# TODO: marker {i}\n", encoding="utf-8")
    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert len(proposals) == 1
    assert proposals[0].score <= 5.0


# ── LintDebtProposer (with injected runner) ──────────────────────────────


def test_lint_debt_no_findings_returns_empty(tmp_path: Path) -> None:
    proposer = LintDebtProposer(runner=lambda _root: [])
    assert list(proposer.propose(tmp_path)) == []


def test_lint_debt_skips_findings_without_fix(tmp_path: Path) -> None:
    findings = [
        {
            "code": "B904",
            "message": "Within an except clause, raise exceptions with `raise ... from`",
            "filename": "src/app.py",
            "fix": None,  # not autofixable
        }
    ]
    proposer = LintDebtProposer(runner=lambda _root: findings)
    assert list(proposer.propose(tmp_path)) == []


def test_lint_debt_groups_autofixable_by_code(tmp_path: Path) -> None:
    findings = [
        {
            "code": "F401",
            "message": "Imported but unused",
            "filename": "src/a.py",
            "fix": {"applicability": "safe"},
        },
        {
            "code": "F401",
            "message": "Imported but unused",
            "filename": "src/b.py",
            "fix": {"applicability": "safe"},
        },
        {
            "code": "I001",
            "message": "Import block is un-sorted",
            "filename": "src/a.py",
            "fix": {"applicability": "safe"},
        },
    ]
    proposer = LintDebtProposer(runner=lambda _root: findings)
    proposals = list(proposer.propose(tmp_path))
    assert len(proposals) == 2

    by_code: dict[str, Proposal] = {p.title.split("`")[1]: p for p in proposals}
    assert "F401" in by_code
    assert "I001" in by_code
    assert "(2 finding(s))" in by_code["F401"].title
    assert "(1 finding(s))" in by_code["I001"].title

    # Both proposals are lint_debt category — autonomy bar applies
    assert all(p.category == "lint_debt" for p in proposals)
    # Both name the autofix command in the body
    for p in proposals:
        assert "ruff check --fix --select" in p.body


def test_lint_debt_runner_default_works_without_ruff(tmp_path: Path) -> None:
    # No ruff config in tmp_path; default runner falls back to empty.
    # (ruff IS installed in the dev venv, but tmp_path has no python files
    # so it'll find zero findings.)
    proposer = LintDebtProposer()  # uses default runner
    proposals = list(proposer.propose(tmp_path))
    assert proposals == []


# ── TypeHoleProposer ──────────────────────────────────────────────────────


def test_type_holes_no_python_files_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("some text\n", encoding="utf-8")
    assert list(TypeHoleProposer().propose(tmp_path)) == []


def test_type_holes_finds_return_any(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text(
        "from typing import Any\n\ndef get_user() -> Any:\n    return 1\n",
        encoding="utf-8",
    )
    proposals = list(TypeHoleProposer().propose(tmp_path))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.category == "type_holes"
    assert p.file_scope == ("api.py",)
    assert "1 `-> Any` return annotation" in p.body


def test_type_holes_finds_param_any(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text(
        "from typing import Any\n\ndef set_user(user: Any) -> None:\n    pass\n",
        encoding="utf-8",
    )
    proposals = list(TypeHoleProposer().propose(tmp_path))
    assert len(proposals) == 1
    assert "1 `: Any` parameter" in proposals[0].body


def test_type_holes_skips_files_without_any(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    assert list(TypeHoleProposer().propose(tmp_path)) == []


def test_type_holes_ranks_by_hit_count(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "from typing import Any\n\ndef f() -> Any: return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "from typing import Any\n\ndef g() -> Any: return 1\ndef h(x: Any) -> Any: return x\n",
        encoding="utf-8",
    )
    proposals = list(TypeHoleProposer().propose(tmp_path))
    assert len(proposals) == 2
    # b.py has more hits → higher score → ranked first
    assert proposals[0].file_scope == ("b.py",)
    assert proposals[0].score > proposals[1].score


# ── default_proposers registry ────────────────────────────────────────────


def test_default_proposers_returns_phase_5_set() -> None:
    proposers = default_proposers()
    ids = {p.id for p in proposers}
    assert ids == {"todo_fixme", "lint_debt", "type_holes"}


def test_default_proposers_returns_fresh_list_each_call() -> None:
    a = default_proposers()
    b = default_proposers()
    assert a is not b


# ── dogfood Issue #8: marker regex + self-detection guards ────────────────


def test_todo_marker_requires_comment_prefix(tmp_path: Path) -> None:
    """A line that mentions 'TODO' inside a regex source or string literal
    without a comment leader must NOT match. Used to: any bare TODO in
    code was flagged, including the marker names inside this proposer's
    own regex literal."""
    (tmp_path / "code.py").write_text(
        'pattern = r"\\b(TODO|FIXME)\\b"\nname = "TODO list"\n',
        encoding="utf-8",
    )
    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert proposals == []  # no real TODO comments → no proposal


def test_todo_marker_matches_inline_comment(tmp_path: Path) -> None:
    """Inline comments are real TODOs — `x = 1  # TODO: ...` must still
    match. The fix from Issue #8 requires a comment-leader, not
    start-of-line."""
    (tmp_path / "code.py").write_text(
        "x = 1  # TODO: handle the edge case\n",
        encoding="utf-8",
    )
    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert len(proposals) == 1
    assert "1 TODO/FIXME" in proposals[0].title


def test_todo_proposer_does_not_self_detect(tmp_path: Path) -> None:
    """Regression for Issue #8: a copy of the proposer's source named
    `todo_fixme.py` is skipped by basename. (Before the fix, scanning
    the Nightly source repo produced 13 hits, all in `todo_fixme.py`
    + `test_proposers.py`.)"""
    src = tmp_path / "packages" / "x" / "todo_fixme.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        '# TODO/FIXME/XXX/HACK detection\n_PATTERN = r"TODO|FIXME"\n',
        encoding="utf-8",
    )
    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert proposals == []


def test_todo_proposer_skips_its_own_tests(tmp_path: Path) -> None:
    """Test fixtures embed marker strings — they're not actionable items.
    Issue #8 fix: skip `test_proposers.py` by basename."""
    src = tmp_path / "tests" / "test_proposers.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        '# Test fixture\nx = "# TODO: simulated marker"\n',
        encoding="utf-8",
    )
    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert proposals == []


def test_todo_proposer_skips_host_internal_dirs(tmp_path: Path) -> None:
    """Issue #13: Claude Code's agent-isolation worktrees live under
    `.claude/worktrees/<id>/` and are full repo duplicates. Without
    this skip, the proposer double-counts every finding across the
    main tree and every leftover agent worktree."""
    # Real code with a real TODO in the main tree
    (tmp_path / "src.py").write_text("# TODO: real one\n", encoding="utf-8")
    # Duplicate copy living under .claude/worktrees/
    leftover = tmp_path / ".claude" / "worktrees" / "agent-1234"
    leftover.mkdir(parents=True)
    (leftover / "src.py").write_text(
        "# TODO: would have been double-counted\n",
        encoding="utf-8",
    )
    # Also test other host-internal dirs
    (tmp_path / ".gemini" / "commands").mkdir(parents=True)
    (tmp_path / ".gemini" / "commands" / "x.py").write_text(
        "# TODO: gemini cache\n", encoding="utf-8"
    )

    proposals = list(TodoFixmeProposer().propose(tmp_path))
    assert len(proposals) == 1
    # The hit count should match the main tree only — 1 marker.
    assert "1 TODO/FIXME" in proposals[0].title
