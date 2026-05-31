"""Proposer that audits TODO/FIXME/XXX/HACK markers across the repo.

Emits a single umbrella proposal listing every marker by file, sorted.
The agent (or a human reviewer) can later split that into per-area
issues if the audit reveals concentrated clusters.

Scoring: monotonically increasing in the number of files touched, capped
to keep the proposer from dominating the ranking when a repo has many
files with stale markers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from nightly_core.proposers.base import Proposal, Proposer

__all__ = ["TodoFixmeProposer"]

# Recognise TODO/FIXME/XXX/HACK only when preceded by a comment leader
# (`#`, `//`, `/*`, `*`, `--`, `<!--`). Without this the proposer would
# self-detect on its own regex literal here (`TODO|FIXME|XXX|HACK` is
# a substring of this very file) plus every test fixture that embeds
# a comment string. Dogfooding Issue #8 — the proposer ran against the
# Nightly source repo and surfaced 13 hits, zero of them actionable.
#
# The comment-leader alternation covers the languages in
# `_DEFAULT_EXTENSIONS` below: `#` for python/bash/ruby/yaml,
# `//` and `/*` and `*` for C-family, `--` for SQL, `<!--` for HTML.
# A leader must appear *somewhere in the same line* before the marker
# — anchoring `^` is too restrictive (real comments can follow code:
# `x = 1  # TODO: explain`).
_MARKER_RE = re.compile(
    r"(?:#|//|/\*|\*|--|<!--)[^\n]*?"
    r"\b(?P<marker>TODO|FIXME|XXX|HACK)(?:\([^)]*\))?\s*[:\-]?\s+(?P<text>.+?)$",
    re.MULTILINE,
)

# Source file extensions Nightly scans. Configurable per-proposer instance.
_DEFAULT_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".sh",
        ".sql",
    }
)

# Directories to skip during the scan.
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
        "target",  # rust / java build dirs
        ".next",
        ".turbo",
        # Host-internal worktree / cache dirs (dogfooding Issue #13).
        # Claude Code's agent-isolation worktrees live under
        # `.claude/worktrees/<agent-id>/`, each one a full duplicate
        # of the repo. Scanning them double-counts every finding and
        # leaks host plumbing into the proposer's output. The same
        # applies to other hosts that mirror state into their dotdirs.
        ".claude",
        ".codex",
        ".cursor",
        ".gemini",
        ".opencode",
        ".nightly",
    }
)

# Files to skip by basename. The proposer's own source and its tests
# contain marker strings *as fixtures*, not actionable items — without
# this carveout the proposer self-detects with a 100% false-positive
# rate on the Nightly source repo (dogfooding Issue #8). The regex
# itself can't tell a comment from a string literal containing a
# `#`-prefixed marker; AST-aware filtering would close the gap fully
# but adds complexity. This narrow basename skip is sufficient until
# the AST pass lands.
_IGNORED_FILENAMES = frozenset(
    {
        "todo_fixme.py",  # this proposer's own source
        "test_proposers.py",  # this proposer's own tests
    }
)

# Score cap to keep the proposer from dominating the ranking when a repo
# has thousands of TODOs.
_SCORE_CAP = 5.0


@dataclass(frozen=True)
class _Hit:
    rel_path: str
    line: int
    marker: str
    text: str


def _walk_source_files(root: Path, *, extensions: frozenset[str]) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in extensions:
            continue
        if any(part in _IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.name in _IGNORED_FILENAMES:
            continue
        yield path


def _scan_file(path: Path, root: Path) -> Iterable[_Hit]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    rel = str(path.relative_to(root))
    for match in _MARKER_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        yield _Hit(
            rel_path=rel,
            line=line,
            marker=match.group("marker").upper(),
            text=match.group("text").strip(),
        )


class TodoFixmeProposer(Proposer):
    """Surface lingering TODO/FIXME/XXX/HACK markers as a single audit."""

    id = "todo_fixme"

    def __init__(self, *, extensions: frozenset[str] | None = None) -> None:
        self.extensions = extensions or _DEFAULT_EXTENSIONS

    def propose(self, root: Path) -> Iterable[Proposal]:
        hits: list[_Hit] = []
        for path in _walk_source_files(root, extensions=self.extensions):
            hits.extend(_scan_file(path, root))
        if not hits:
            return ()

        by_file: dict[str, list[_Hit]] = {}
        for hit in hits:
            by_file.setdefault(hit.rel_path, []).append(hit)

        body_lines: list[str] = [
            "## TODO / FIXME audit",
            "",
            f"Found **{len(hits)}** marker(s) across **{len(by_file)}** file(s).",
            "",
            "_Surfaced by Nightly's `todo_fixme` proposer. Review and decide_",
            "_which markers represent real work, which are stale, and which_",
            "_should become real issues with acceptance criteria._",
            "",
        ]
        for rel_path, file_hits in sorted(by_file.items()):
            body_lines.append(f"### `{rel_path}` ({len(file_hits)})")
            body_lines.append("")
            for hit in sorted(file_hits, key=lambda h: h.line):
                body_lines.append(f"- L{hit.line}  **{hit.marker}** — {hit.text}")
            body_lines.append("")

        score = min(_SCORE_CAP, 1.0 + 0.25 * len(by_file))
        return [
            Proposal(
                proposer=self.id,
                category="todo_audit",
                title=(f"Audit {len(hits)} TODO/FIXME marker(s) across {len(by_file)} file(s)"),
                body="\n".join(body_lines),
                score=score,
                file_scope=tuple(sorted(by_file.keys())),
                estimated_loc=len(hits),
            )
        ]
