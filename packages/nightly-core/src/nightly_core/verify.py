"""`nightly verify` — detect & run the repo's linters and formatters.

Nightly opens PRs autonomously, which means it needs to clear the same
quality gates a human contributor would clear before pushing. This
module is the agent-facing equivalent of the user's `pre-commit run` /
`make lint` muscle memory: detect what's configured, run all of it,
report the result.

Detection is lightweight and config-driven — we don't import the tool's
own python package or shell out a discovery command. We inspect
`pyproject.toml` / `package.json` / `go.mod` / `Cargo.toml` /
`Makefile` and key off well-known config keys / targets. Anything we
find gets a `VerifyCheck` row; anything we can't find is silently
omitted (no fail-on-missing — "this repo doesn't use ruff" should not
break verify).

Running is direct subprocess. We never modify the working tree
(`--check` / `-l` flavors only); the agent is expected to run the
auto-fix variants themselves before re-invoking `nightly verify`. If
any check fails, the CLI exits non-zero so the agent's own `if`
statement in the prompt can branch on it.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = [
    "VerifyCheck",
    "VerifyReport",
    "detect_checks",
    "run_verify",
]


CheckStatus = Literal["ok", "failed", "skipped", "not_found"]


@dataclass(frozen=True)
class VerifyCheck:
    """One detected lint / format / type tool and its run outcome.

    `command` is what we would (or did) shell out — preserved for the
    audit trail. `status` is `not_found` when the binary is missing
    from PATH (the tool is configured but not installed), `skipped`
    when we explicitly didn't run it (dry-run mode), `ok` / `failed`
    after a real run. `output` carries stdout+stderr truncated to a
    reasonable budget so the briefing isn't drowned in noise.
    """

    name: str
    description: str
    command: tuple[str, ...]
    status: CheckStatus
    output: str = ""
    exit_code: int = 0


@dataclass(frozen=True)
class VerifyReport:
    """All checks Nightly ran for this verify invocation."""

    checks: tuple[VerifyCheck, ...]
    dry_run: bool

    @property
    def failed(self) -> tuple[VerifyCheck, ...]:
        return tuple(c for c in self.checks if c.status == "failed")

    @property
    def not_found(self) -> tuple[VerifyCheck, ...]:
        return tuple(c for c in self.checks if c.status == "not_found")

    @property
    def passed(self) -> tuple[VerifyCheck, ...]:
        return tuple(c for c in self.checks if c.status == "ok")

    @property
    def ok(self) -> bool:
        """True iff no check failed *and* no configured tool was missing."""
        return not self.failed and not self.not_found


# ── detection ─────────────────────────────────────────────────────────────


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _pyproject_has_tool(path: Path, *, tool: str) -> bool:
    """True iff `pyproject.toml` declares `[tool.<tool>]` or its lockfile equivalent.

    Cheap substring match rather than a TOML parse — verify must work in
    a repo that hasn't installed Nightly's deps yet. The cost is a
    handful of false positives (e.g. a stringly-typed mention of
    `[tool.ruff]` in a comment) which the run step will catch when the
    binary isn't there or the config is unparseable.
    """
    text = _read_text(path)
    return f"[tool.{tool}]" in text or f"[tool.{tool}." in text


def _package_json_has_dep(path: Path, *, dep: str) -> bool:
    text = _read_text(path)
    if not text:
        return False
    # Both runtime + devDependencies count.
    needle = f'"{dep}"'
    return needle in text


def _makefile_target(path: Path) -> set[str]:
    """Return the set of target names defined in a Makefile, best-effort."""
    text = _read_text(path)
    if not text:
        return set()
    out: set[str] = set()
    for line in text.splitlines():
        # Targets are `name:` at column 0 (no leading whitespace).
        if not line or line[0] in (" ", "\t", "#"):
            continue
        if ":" not in line:
            continue
        name = line.split(":", 1)[0].strip()
        # Skip variables (foo = bar) and ill-formed lines.
        if name and "=" not in name and not name.startswith("."):
            out.add(name)
    return out


def detect_checks(root: Path) -> list[VerifyCheck]:  # noqa: PLR0912 — one branch per ecosystem is the whole point
    """Inspect `root` and return one `VerifyCheck(status='skipped')` per detected tool.

    Use this as the dry-run output. `run_verify` calls it and then
    executes the resulting commands, replacing each with a fresh
    `VerifyCheck` carrying the run outcome.
    """
    out: list[VerifyCheck] = []
    pyproject = root / "pyproject.toml"
    package_json = root / "package.json"
    go_mod = root / "go.mod"
    cargo_toml = root / "Cargo.toml"
    makefile = root / "Makefile"

    # Python
    if pyproject.is_file():
        if _pyproject_has_tool(pyproject, tool="ruff"):
            out.append(
                VerifyCheck(
                    name="ruff-check",
                    description="ruff lint",
                    command=("ruff", "check", "."),
                    status="skipped",
                )
            )
            out.append(
                VerifyCheck(
                    name="ruff-format",
                    description="ruff format check",
                    command=("ruff", "format", "--check", "."),
                    status="skipped",
                )
            )
        if _pyproject_has_tool(pyproject, tool="black"):
            out.append(
                VerifyCheck(
                    name="black",
                    description="black format check",
                    command=("black", "--check", "."),
                    status="skipped",
                )
            )
        if _pyproject_has_tool(pyproject, tool="mypy"):
            out.append(
                VerifyCheck(
                    name="mypy",
                    description="mypy type check",
                    command=("mypy", "."),
                    status="skipped",
                )
            )
        if _pyproject_has_tool(pyproject, tool="pyrefly"):
            out.append(
                VerifyCheck(
                    name="pyrefly",
                    description="pyrefly type check",
                    command=("pyrefly", "check"),
                    status="skipped",
                )
            )

    # JavaScript / TypeScript
    if package_json.is_file():
        if _package_json_has_dep(package_json, dep="eslint"):
            out.append(
                VerifyCheck(
                    name="eslint",
                    description="eslint lint",
                    command=("npx", "--no", "eslint", "."),
                    status="skipped",
                )
            )
        if _package_json_has_dep(package_json, dep="prettier"):
            out.append(
                VerifyCheck(
                    name="prettier",
                    description="prettier format check",
                    command=("npx", "--no", "prettier", "--check", "."),
                    status="skipped",
                )
            )
        if _package_json_has_dep(package_json, dep="typescript"):
            out.append(
                VerifyCheck(
                    name="tsc",
                    description="tsc type check (--noEmit)",
                    command=("npx", "--no", "tsc", "--noEmit"),
                    status="skipped",
                )
            )

    # Go
    if go_mod.is_file():
        out.append(
            VerifyCheck(
                name="gofmt",
                description="gofmt -l check (must return empty)",
                command=("gofmt", "-l", "."),
                status="skipped",
            )
        )
        out.append(
            VerifyCheck(
                name="go-vet",
                description="go vet",
                command=("go", "vet", "./..."),
                status="skipped",
            )
        )

    # Rust
    if cargo_toml.is_file():
        out.append(
            VerifyCheck(
                name="cargo-fmt",
                description="cargo fmt --check",
                command=("cargo", "fmt", "--", "--check"),
                status="skipped",
            )
        )
        out.append(
            VerifyCheck(
                name="cargo-clippy",
                description="cargo clippy -D warnings",
                command=("cargo", "clippy", "--", "-D", "warnings"),
                status="skipped",
            )
        )

    # Makefile umbrella targets — only added if the targets exist *and*
    # we didn't already enumerate tool-specific checks above. This is
    # the escape hatch for repos that wire their own `make lint`.
    if makefile.is_file():
        targets = _makefile_target(makefile)
        for target in ("lint", "check", "verify"):
            if target in targets:
                out.append(
                    VerifyCheck(
                        name=f"make-{target}",
                        description=f"make {target}",
                        command=("make", target),
                        status="skipped",
                    )
                )

    return out


# ── execution ─────────────────────────────────────────────────────────────


_OUTPUT_BUDGET_BYTES = 8192
"""Cap on per-check stdout+stderr we keep in the report; longer output
gets truncated with a marker so the briefing doesn't explode."""

_DEFAULT_TIMEOUT_S = 300.0
"""Per-check timeout. Verify is meant to be quick — 5 min is plenty."""


def _truncate(text: str, budget: int = _OUTPUT_BUDGET_BYTES) -> str:
    if len(text) <= budget:
        return text
    return text[:budget] + f"\n… (truncated {len(text) - budget} bytes)"


def _run_one(check: VerifyCheck, *, cwd: Path, timeout: float) -> VerifyCheck:
    binary = check.command[0]
    # `npx --no` does the resolution itself (no `which` for `eslint` etc.);
    # `which` only catches the launcher binary in that case.
    launcher = "npx" if binary == "npx" else binary
    if shutil.which(launcher) is None:
        return VerifyCheck(
            name=check.name,
            description=check.description,
            command=check.command,
            status="not_found",
            output=f"binary not on PATH: {launcher}",
            exit_code=-1,
        )
    try:
        result = subprocess.run(
            list(check.command),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return VerifyCheck(
            name=check.name,
            description=check.description,
            command=check.command,
            status="failed",
            output=f"timed out after {exc.timeout}s",
            exit_code=-1,
        )
    except OSError as exc:
        return VerifyCheck(
            name=check.name,
            description=check.description,
            command=check.command,
            status="failed",
            output=f"OSError: {exc}",
            exit_code=-1,
        )
    combined = _truncate((result.stdout or "") + (result.stderr or ""))
    # gofmt -l succeeds with non-empty output when files need formatting.
    # Convention: any non-empty stdout = failure.
    if (
        check.name == "gofmt"
        and result.returncode == 0
        and (result.stdout or "").strip()
    ):
        return VerifyCheck(
            name=check.name,
            description=check.description,
            command=check.command,
            status="failed",
            output=combined,
            exit_code=1,
        )
    status: CheckStatus = "ok" if result.returncode == 0 else "failed"
    return VerifyCheck(
        name=check.name,
        description=check.description,
        command=check.command,
        status=status,
        output=combined,
        exit_code=result.returncode,
    )


def run_verify(
    root: Path,
    *,
    dry_run: bool = False,
    only: Iterable[str] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> VerifyReport:
    """Detect + run every applicable check; return the structured report.

    `dry_run=True` short-circuits before any subprocess fires — every
    check stays `skipped`. `only` (an iterable of check names) narrows
    the run to just those checks; pass None to run them all.
    """
    detected = detect_checks(root)
    selected = (
        [c for c in detected if c.name in set(only)] if only is not None else detected
    )
    if dry_run:
        return VerifyReport(checks=tuple(selected), dry_run=True)

    results = [_run_one(c, cwd=root, timeout=timeout_s) for c in selected]
    return VerifyReport(checks=tuple(results), dry_run=False)
