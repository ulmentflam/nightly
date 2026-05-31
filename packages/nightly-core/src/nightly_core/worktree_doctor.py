"""Worktree readiness — probe and (optionally) remediate broken pre-commit hooks.

A fresh `git worktree` venv is reliably under-equipped: project-wide
type-checkers (`pyrefly`, `mypy`, `tsc`) and other heavyweight pre-commit
hooks need the full dependency graph installed before they can run. The
corpus-forge incident (issue #2) burned 21 verified tasks across 2
sessions because every `git commit` failed on these hook errors.

The probe answers: "is this worktree's pre-commit infrastructure
runnable?" — and classifies failures into a small known set of
remediation patterns. The driver wires the probe into a new cascade
step (`worktree_blocked`) and into pre-dispatch readiness gating.

Refusal-policy carveout: installing missing dependencies via the repo's
declared installer (`uv sync`, `pip install -r`, `pre-commit install
--install-hooks`) is **making the test runnable**, not bypassing it.
`--no-verify` remains forbidden. See `.planning/brainstorm.html` §06.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = [
    "ReadinessKind",
    "ReadinessState",
    "WorktreeReadiness",
    "probe_worktree_readiness",
    "remediate_missing_pre_commit_hook",
    "remediate_missing_python_dep",
]


ReadinessState = Literal["ok", "remediable", "blocked"]
"""Whether the worktree is ready, fixable, or broken."""

ReadinessKind = Literal[
    "missing_python_dep",
    "missing_pre_commit_hook",
    "missing_binary",
    "hook_config_error",
    "unknown",
]
"""Failure pattern; `None` when state == "ok"."""


@dataclass(frozen=True)
class WorktreeReadiness:
    """The result of `probe_worktree_readiness()`.

    `kind` is None iff `state == "ok"`. `detail` carries the captured
    error excerpt for unremediable failures, so the operator-facing
    proposal can quote it directly.
    """

    state: ReadinessState
    kind: ReadinessKind | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    @property
    def remediable(self) -> bool:
        return self.state == "remediable"

    @property
    def blocked(self) -> bool:
        return self.state == "blocked"


# ── signature classifier ──────────────────────────────────────────────────


_MISSING_DEP_RE = re.compile(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]")
_MISSING_HOOK_RE = re.compile(
    r"(hook .* (is not |isn't )installed|run.*pre-commit install)", re.IGNORECASE
)
_MISSING_BINARY_RE = re.compile(
    r"(command not found|No such file or directory:\s*'(?P<tool>[^']+)')"
)
_HOOK_CONFIG_RE = re.compile(
    r"(yaml.parser.ParserError|yaml.scanner.ScannerError|"
    r"toml(?:lib)?\.\w*Error|invalid pre-commit config)",
    re.IGNORECASE,
)


def _classify(output: str) -> tuple[ReadinessKind, str]:
    """Reduce a captured pre-commit output to a (kind, detail) pair.

    Order matters: missing-dep is the most specific so it wins. Unknown
    is the catch-all; the operator sees the raw output as detail.
    """
    if (m := _MISSING_DEP_RE.search(output)) is not None:
        return "missing_python_dep", m.group(1)
    if _MISSING_HOOK_RE.search(output):
        return "missing_pre_commit_hook", "pre-commit hooks not installed"
    if (m := _MISSING_BINARY_RE.search(output)) is not None:
        tool = m.group("tool") if m.group("tool") else ""
        return "missing_binary", tool
    if _HOOK_CONFIG_RE.search(output):
        return "hook_config_error", output[:400].strip()
    return "unknown", output[:400].strip()


# Subset of `kind`s the doctor can auto-fix. `missing_binary` and below
# require operator intervention (install the tool, fix YAML, etc.).
_REMEDIABLE_KINDS: frozenset[ReadinessKind] = frozenset(
    {"missing_python_dep", "missing_pre_commit_hook"}
)


# ── probe ─────────────────────────────────────────────────────────────────


def probe_worktree_readiness(
    root: Path,
    *,
    runner: type[subprocess.CompletedProcess] | None = None,
) -> WorktreeReadiness:
    """Run pre-commit against an empty file set and classify the result.

    Returns:
    - `WorktreeReadiness(state="ok")` if `.pre-commit-config.yaml` doesn't
      exist (no hooks to probe) or pre-commit exits 0.
    - `WorktreeReadiness(state="remediable", kind=...)` if the failure
      matches a known remediation pattern.
    - `WorktreeReadiness(state="blocked", kind=...)` for anything else.

    `runner` is unused in production; the parameter exists to keep the
    test surface narrow (tests inject a subprocess factory via
    `monkeypatch`).
    """
    config = root / ".pre-commit-config.yaml"
    if not config.is_file():
        return WorktreeReadiness(state="ok")

    if shutil.which("pre-commit") is None:
        return WorktreeReadiness(
            state="blocked",
            kind="missing_binary",
            detail="pre-commit",
        )

    try:
        result = subprocess.run(
            ["pre-commit", "run", "--all-files", "--show-diff-on-failure"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return WorktreeReadiness(state="blocked", kind="unknown", detail=str(exc))

    if result.returncode == 0:
        return WorktreeReadiness(state="ok")

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    kind, detail = _classify(output)
    state: ReadinessState = "remediable" if kind in _REMEDIABLE_KINDS else "blocked"
    return WorktreeReadiness(state=state, kind=kind, detail=detail)


# ── remediation ───────────────────────────────────────────────────────────


def remediate_missing_python_dep(root: Path) -> bool:
    """Install Python deps via the repo's declared installer.

    Tries `uv sync --all-packages --all-extras` first if `uv.lock` is
    present, falls back to `pip install -r requirements*.txt`. Returns
    True on success, False otherwise.
    """
    if (root / "uv.lock").is_file() and shutil.which("uv") is not None:
        try:
            r = subprocess.run(
                ["uv", "sync", "--all-packages", "--all-extras"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    requirements = sorted(root.glob("requirements*.txt"))
    if requirements and shutil.which("pip") is not None:
        try:
            argv = ["pip", "install"]
            for req in requirements:
                argv += ["-r", str(req)]
            r = subprocess.run(
                argv, cwd=root, capture_output=True, text=True, timeout=600, check=False
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    return False


def remediate_missing_pre_commit_hook(root: Path) -> bool:
    """Run `pre-commit install --install-hooks` so the framework
    materializes its declared hook environments."""
    if shutil.which("pre-commit") is None:
        return False
    try:
        r = subprocess.run(
            ["pre-commit", "install", "--install-hooks"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def remediate(readiness: WorktreeReadiness, root: Path) -> bool:
    """Invoke the remediator matching `readiness.kind`. Returns the
    remediator's bool result, or False for unremediable kinds."""
    if readiness.kind == "missing_python_dep":
        return remediate_missing_python_dep(root)
    if readiness.kind == "missing_pre_commit_hook":
        return remediate_missing_pre_commit_hook(root)
    return False
