"""Proposer that flags `Any` / `unknown` at Python module boundaries.

The brainstorm calls these "type holes" — uses of the top type at
locations where a more specific signature would have value. Phase 5 ships
a Python-only heuristic; TypeScript / Go variants can land later.

Heuristic patterns (regex-based, intentionally conservative):
- `def fn(...) -> Any`
- `def fn(arg: Any, ...)`
- `: Any =` (annotated variable initialized to `Any`)

The proposer emits one proposal per file with hits, ranked by hit count.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from nightly_core.proposers.base import Proposal, Proposer

__all__ = ["TypeHoleProposer"]

# `def fn(...) -> Any:` (return annotation = Any)
_RE_RETURN_ANY = re.compile(r"^\s*(async\s+)?def\s+\w+\([^)]*\)\s*->\s*Any\b", re.MULTILINE)
# `arg: Any` inside a function signature — keyed off the `def` line via a
# multi-line scan; we approximate by looking for `: Any` in a `def` context
# on the same line. This is intentionally cheap; tests pin the cases.
_RE_PARAM_ANY = re.compile(r"^\s*(async\s+)?def\s+\w+\([^)]*:\s*Any\b", re.MULTILINE)
# Annotated variable initialized to Any: `x: Any = ...`
_RE_VAR_ANY = re.compile(r"^\s*\w+\s*:\s*Any\b", re.MULTILINE)

_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".pyrefly_cache",
        # Host-internal worktree / cache dirs (dogfooding Issue #13).
        # Claude Code's agent-isolation worktrees live under
        # `.claude/worktrees/<id>/`, full repo duplicates; scanning
        # them double-counts every finding. Same for other hosts.
        ".claude",
        ".codex",
        ".cursor",
        ".gemini",
        ".opencode",
        ".nightly",
    }
)

_SCORE_BASE = 1.0
_SCORE_PER_HIT = 0.2
_SCORE_CAP = 5.0


@dataclass(frozen=True)
class _FileHits:
    rel_path: str
    return_any: int
    param_any: int
    var_any: int

    @property
    def total(self) -> int:
        return self.return_any + self.param_any + self.var_any


def _walk_python(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def _scan_file(path: Path, root: Path) -> _FileHits | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Cheap pre-check: skip files that don't import Any at all.
    if "Any" not in text:
        return None
    return_any = len(_RE_RETURN_ANY.findall(text))
    param_any = len(_RE_PARAM_ANY.findall(text))
    var_any = len(_RE_VAR_ANY.findall(text))
    if not (return_any or param_any or var_any):
        return None
    return _FileHits(
        rel_path=str(path.relative_to(root)),
        return_any=return_any,
        param_any=param_any,
        var_any=var_any,
    )


class TypeHoleProposer(Proposer):
    """Flag Python files with `Any` at function signatures or boundaries."""

    id = "type_holes"

    def propose(self, root: Path) -> Iterable[Proposal]:
        results: list[_FileHits] = []
        for path in _walk_python(root):
            hits = _scan_file(path, root)
            if hits:
                results.append(hits)
        if not results:
            return ()

        proposals: list[Proposal] = []
        for hits in sorted(results, key=lambda h: -h.total):
            score = min(_SCORE_CAP, _SCORE_BASE + _SCORE_PER_HIT * hits.total)
            body = "\n".join(
                [
                    f"## Type holes in `{hits.rel_path}`",
                    "",
                    f"- {hits.return_any} `-> Any` return annotation(s)",
                    f"- {hits.param_any} `: Any` parameter annotation(s)",
                    f"- {hits.var_any} `: Any` variable annotation(s)",
                    "",
                    "Replacing `Any` with a concrete type at module boundaries",
                    "improves IDE help, catches misuse earlier, and surfaces",
                    "assumptions to reviewers. Internal `Any` (inside private",
                    "helpers) is usually fine — focus on the public surface.",
                ]
            )
            proposals.append(
                Proposal(
                    proposer=self.id,
                    category="type_holes",
                    title=f"Tighten {hits.total} `Any` usage(s) in {hits.rel_path}",
                    body=body,
                    score=score,
                    file_scope=(hits.rel_path,),
                    estimated_loc=hits.total,
                )
            )
        return proposals
