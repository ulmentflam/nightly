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

# Recognise TODO/FIXME/XXX/HACK in a comment context. The marker may be
# followed by `(author)`, `:`, or whitespace before the actual message.
_MARKER_RE = re.compile(
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
