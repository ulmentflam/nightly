"""RFC 004 §D — characterization tests against the 2026-05-24 stacked-paperwork
bundle.

The 2026-05-24 incident produced this PR chain:

    main
    └── nightly/unblock-20260523            (PR #54)
        └── nightly/phase-e-reconcile-…     (PR #55, base = #54)
            └── nightly/phase-j-reconcile-… (PR #56, base = #55)
                └── nightly/phase-k-…       (PR #57, base = #56)
                    └── nightly/plan-recon… (PR #58, base = #57)

The tests below pin two scenarios:

- **D1.** None of the child plans declare `depends_on_pr` → the driver's
  `_resolve_base_branch` returns `main` for every one of them. The stack
  is prevented at branch-creation time. Even if HEAD lives on a chain
  branch, the cascade's geometry detection marks it as accidental
  (rose panel).

- **D2.** Every child plan declares `depends_on_pr: <parent>` → the
  driver bases each worktree on the parent PR's head ref. The chain is
  preserved (declared, intentional), the briefing's geometry detection
  marks it as declared (teal panel), and the prompt builder injects the
  literal `Depends on #<N>` line into the agent's task prompt.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
from pathlib import Path

import nightly_core.cascade as cascade_mod
import nightly_core.worktree as worktree_mod
from nightly_core.driver import build_task_prompt
from nightly_core.plans import PlanRecord, parse_frontmatter, render_frontmatter
from nightly_core.runs import new_task, start_run
from nightly_core.worktree import _resolve_base_branch

# The historical chain. Each tuple is `(branch, pr_number)`.
_CHAIN = [
    ("nightly/unblock-20260523", 54),
    ("nightly/phase-e-reconcile", 55),
    ("nightly/phase-j-reconcile", 56),
    ("nightly/phase-k-reconcile", 57),
    ("nightly/plan-reconcile", 58),
]


def _stub_gh_for_chain(monkeypatch) -> None:
    """Stub `gh pr view <N>` so each PR in the chain reports OPEN + its branch."""

    def fake_run(args, **_kwargs):
        # args looks like: ['gh', 'pr', 'view', '54', '--json', 'headRefName,state']
        pr_num = int(args[3])
        branch = next((b for b, n in _CHAIN if n == pr_num), "")
        payload = {"state": "OPEN" if branch else "CLOSED", "headRefName": branch}
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(worktree_mod.subprocess, "run", fake_run)


def _stamp_depends_on_pr(plan_path: Path, pr_number: int) -> None:
    text = plan_path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    metadata["depends_on_pr"] = str(pr_number)
    plan_path.write_text(render_frontmatter(metadata, body), encoding="utf-8")


# ── D1 — undeclared chain: prevention kicks in ────────────────────────────


def test_d1_undeclared_chain_all_cut_from_main(monkeypatch) -> None:
    """No plan declares `depends_on_pr` → every worktree's base = `main`.

    `_resolve_base_branch` never reaches the `gh` subprocess in this path,
    so a stub that would fail the test if called is the strongest
    assertion: it proves the short-circuit on `depends_on_pr is None` is
    load-bearing."""

    def _unexpected(*_args, **_kwargs) -> None:
        msg = "gh should not be consulted when no plan declares depends_on_pr"
        raise AssertionError(msg)

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(worktree_mod.subprocess, "run", _unexpected)

    for _, _ in _CHAIN[1:]:  # children would-have-been; #54 was the root
        base = _resolve_base_branch(depends_on_pr=None, default_base="main")
        assert base == "main"


def test_d1_geometry_panel_rose_when_undeclared(monkeypatch) -> None:
    """Even if HEAD ends up on a chain branch (e.g. legacy worktrees pre-RFC),
    `detect_stacked_geometry` marks the chain as accidental → briefing
    renders rose."""

    def stub_branch(*_a, **_kw):
        return subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="nightly/phase-k-reconcile\n"
        )

    monkeypatch.setattr(cascade_mod.subprocess, "run", stub_branch)
    monkeypatch.setattr(
        cascade_mod,
        "_nightly_open_pr_branches",
        lambda *_a, **_kw: [(b, n, f"https://example/{n}") for b, n in _CHAIN],
    )
    monkeypatch.setattr(cascade_mod, "_match_plan_to_branch", lambda *_a, **_kw: None)
    geo = cascade_mod.detect_stacked_geometry()
    assert geo.is_stacked
    assert not geo.all_declared
    # The single chain entry should be PR #57 (HEAD's own PR), declared=False.
    assert geo.chain == ((57, "nightly/phase-k-reconcile", "https://example/57", False),)


# ── D2 — declared chain: stacking is preserved + green ────────────────────


def test_d2_declared_chain_preserves_stacking(monkeypatch) -> None:
    """Each child plan declares its parent → `_resolve_base_branch` returns
    the parent's head ref (preserves the dependency)."""
    _stub_gh_for_chain(monkeypatch)
    # Walk the chain pairwise: each child opt-in-stacks on its parent.
    for (parent_branch, parent_pr), (_child_branch, _child_pr) in itertools.pairwise(_CHAIN):
        base = _resolve_base_branch(depends_on_pr=parent_pr, default_base="main")
        assert base == parent_branch, f"PR #{parent_pr} should resolve to {parent_branch}"


def test_d2_geometry_panel_teal_when_declared(monkeypatch) -> None:
    """Plan declares depends_on_pr → geometry panel renders teal/declared."""

    def stub_branch(*_a, **_kw):
        return subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="nightly/phase-k-reconcile\n"
        )

    fake_plan = PlanRecord(
        path=Path("/nonexistent/plan.md"),
        metadata={"depends_on_pr": "56"},  # PR #56 is the parent of HEAD's PR #57
        body="",
    )
    monkeypatch.setattr(cascade_mod.subprocess, "run", stub_branch)
    monkeypatch.setattr(
        cascade_mod,
        "_nightly_open_pr_branches",
        lambda *_a, **_kw: [(b, n, f"https://example/{n}") for b, n in _CHAIN],
    )
    monkeypatch.setattr(cascade_mod, "_match_plan_to_branch", lambda *_a, **_kw: fake_plan)
    geo = cascade_mod.detect_stacked_geometry()
    assert geo.is_stacked
    # Single-entry chain (HEAD's PR), declared because plan carries
    # `depends_on_pr` — v1's per-entry declared flag is "did the current
    # plan declare ANY dependency", not "did it declare this specific PR".
    assert geo.all_declared


def test_d2_prompt_carries_pr_body_directive(tmp_path: Path) -> None:
    """When a plan declares `depends_on_pr`, the task prompt instructs the
    agent to begin the PR body with the literal `Depends on #N` line. Walks
    one slice of the chain (PR #58 → depends_on_pr=#57) for brevity; the
    prompt-injection unit tests in test_driver.py cover the broader matrix."""
    from nightly_core.plans import read_plan

    run = start_run(tmp_path)
    task = new_task(run, slug="plan-reconcile")
    _stamp_depends_on_pr(task.path / "plan.md", 57)
    plan = read_plan(task.path / "plan.md")

    prompt = build_task_prompt(plan, task.path)
    assert "Declared dependency" in prompt
    assert "Depends on #57" in prompt
    assert "base = PR #57" in prompt
