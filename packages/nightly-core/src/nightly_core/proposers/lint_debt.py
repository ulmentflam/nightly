"""Proposer that surfaces autofixable lint findings as draft issues.

Phase 5 only knows about `ruff` (the project's own linter). Future
phases can add eslint/biome/clippy/golangci-lint per file detection.

The output is one proposal per ruff rule code with > 0 autofixable
findings — keeping each issue focused so the autonomy bar can decide
per-rule. Score scales with the finding count up to a cap.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from nightly_core.proposers.base import Proposal, Proposer

__all__ = ["LintDebtProposer"]

# Ruff's check command flags used by the proposer.
_RUFF_ARGS: tuple[str, ...] = (
    "check",
    "--output-format",
    "json",
    "--no-fix",
)

_SCORE_BASE = 1.0
_SCORE_PER_FINDING = 0.15
_SCORE_CAP = 5.0


# Caller-injectable to keep tests hermetic.
RuffRunner = Callable[[Path], list[dict]]


def _ruff_runner_default(root: Path) -> list[dict]:
    """Default runner — shells out to `ruff check --output-format json`.

    Returns an empty list when `ruff` is missing, the command fails, or
    the output is unparseable. The proposer is best-effort.
    """
    if shutil.which("ruff") is None:
        return []
    try:
        result = subprocess.run(
            ["ruff", *_RUFF_ARGS],
            cwd=root,
            check=False,  # ruff exits non-zero when findings exist
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if not result.stdout:
        return []
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return parsed


def _is_autofixable(finding: dict) -> bool:
    fix = finding.get("fix")
    if not isinstance(fix, dict):
        return False
    # ruff signals "fix applied automatically" vs "fix requires confirmation".
    return fix.get("applicability") in {"safe", "sometimes", "always", None}


class LintDebtProposer(Proposer):
    """Surface autofixable ruff findings, grouped by rule code."""

    id = "lint_debt"

    def __init__(self, *, runner: RuffRunner | None = None) -> None:
        self._runner = runner or _ruff_runner_default

    def propose(self, root: Path) -> Iterable[Proposal]:
        findings = self._runner(root)
        if not findings:
            return ()

        autofixable = [f for f in findings if _is_autofixable(f)]
        if not autofixable:
            return ()

        by_code: dict[str, list[dict]] = {}
        for finding in autofixable:
            code = str(finding.get("code") or "?")
            by_code.setdefault(code, []).append(finding)

        proposals: list[Proposal] = []
        for code, items in sorted(by_code.items()):
            files = sorted({str(f.get("filename") or "") for f in items if f.get("filename")})
            description = str(items[0].get("message") or "").strip()
            body = "\n".join(
                [
                    f"## ruff `{code}` — autofixable",
                    "",
                    f"**{len(items)}** finding(s) across **{len(files)}** file(s).",
                    "",
                    f"_Rule message:_ {description}" if description else "",
                    "",
                    "Run `ruff check --fix --select " + code + "` to apply the fix.",
                    "",
                    "### Affected files",
                    "",
                    *(f"- `{f}`" for f in files),
                ]
            )
            score = min(_SCORE_CAP, _SCORE_BASE + _SCORE_PER_FINDING * len(items))
            proposals.append(
                Proposal(
                    proposer=self.id,
                    category="lint_debt",
                    title=f"Apply autofixable ruff `{code}` ({len(items)} finding(s))",
                    body=body,
                    score=score,
                    file_scope=tuple(files),
                    estimated_loc=len(items),
                )
            )
        return proposals
