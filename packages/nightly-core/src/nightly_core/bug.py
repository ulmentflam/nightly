"""`nightly bug` — bundle Nightly run state into a debug report.

The user runs `nightly bug` (or `/nightly-bug`) when Nightly's own
behavior looks broken — the agent self-concluded, the cascade ignored
a real plan, the Stop hook stopped force-continuing while work
remained, a worktree got wedged, etc. The command:

1. Gathers the relevant on-disk state — `keepalive.log`, current run
   markers (CONCLUDE / STOP / SESSION_ACTIVE / keepalive.turns), plan
   statuses, `briefing.md` / `briefing.html`, the AGENTS.md / CLAUDE.md
   rules block, recent git log, and `nightly status` / `nightly next`
   output.
2. Writes a markdown report under `.nightly/bugs/<ts>/report.md`
   (separate from `.nightly/runs/` so a busted run can still produce
   a clean report).
3. If `gh` is available and a target repo is reachable, opens an
   issue on the Nightly source repo (`ulmentflam/nightly` by default,
   overridable via `--repo`). When `gh` is missing the report is
   written to disk and the gh command line that *would* have run is
   printed for the operator to copy.

The agent must never invoke this command itself — `bug` is a tool the
human reaches for when they've observed a problem and want it
captured for triage. Self-filing would mask whatever the agent was
about to do wrong; see rules.py rule 10.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from nightly_core._version import __version__
from nightly_core.paths import nightly_dir, repo_root
from nightly_core.runs import current_run

__all__ = [
    "DEFAULT_BUG_REPO",
    "BugReport",
    "build_report",
    "gh_command",
    "submit_report",
    "write_report",
]


DEFAULT_BUG_REPO = "ulmentflam/nightly"
"""GitHub `owner/name` of the Nightly source repo. `nightly bug`
opens issues here by default; `--repo` overrides for forks."""


_MAX_LOG_LINES = 200
"""Truncate `keepalive.log` to the last N lines for the report body —
the full file is attached as a fenced block under "Logs."""

_MAX_GIT_LOG_ENTRIES = 20
"""How many recent commits to include from the host repo."""


@dataclass(frozen=True)
class BugReport:
    """A rendered bug report ready to be written or shipped to GitHub."""

    title: str
    body: str
    path: Path
    """Where the report markdown lives on disk."""

    extra_attachments: tuple[Path, ...] = field(default_factory=tuple)
    """Additional files (briefing.md, keepalive.log) the operator may
    want to attach manually when filing the issue. `gh issue create`
    doesn't take file attachments, so these are advisory."""


# ── public entry points ────────────────────────────────────────────────────


def build_report(
    *,
    root: Path | None = None,
    title: str | None = None,
    summary: str | None = None,
    now: datetime | None = None,
) -> BugReport:
    """Collect on-disk state and render a markdown bug report.

    Does **not** write anything — the caller composes this and then
    passes it to `write_report` (and optionally `submit_report`).
    Pure builders make the CLI side easy to test.

    `title` and `summary` are operator-supplied. When `title` is
    missing, the function auto-generates one from current run id and
    timestamp. `summary` is the free-text "what went wrong" the
    operator can type at the prompt; it becomes the first section of
    the body so reviewers see context before the disk dump.
    """
    root = (root or repo_root()).resolve()
    moment = now or datetime.now(UTC)
    stamp = moment.strftime("%Y-%m-%dT%H-%M-%SZ")

    run = current_run(root)
    run_id = run.id if run else None

    auto_title = f"Nightly bug report — run {run_id or '(no active run)'} @ {stamp}"
    final_title = (title or auto_title).strip() or auto_title

    sections: list[str] = []
    sections.append(_render_header(run=run, stamp=stamp, root=root))
    if summary:
        sections.append("## Operator summary\n\n" + summary.strip() + "\n")
    else:
        sections.append(
            "## Operator summary\n\n"
            "_No summary supplied — re-run with `--describe \"…\"` to add one._\n"
        )
    sections.append(_render_markers_section(run))
    sections.append(_render_keepalive_log_section(run))
    sections.append(_render_plans_section(root))
    sections.append(_render_briefing_section(run))
    sections.append(_render_status_section(root))
    sections.append(_render_next_section(root))
    sections.append(_render_git_section(root))
    sections.append(_render_environment_section(root))

    body = "\n".join(s.rstrip() + "\n" for s in sections if s)

    bugs_dir = nightly_dir(root) / "bugs" / stamp
    report_path = bugs_dir / "report.md"

    extras: list[Path] = []
    if run is not None:
        for candidate in ("keepalive.log", "briefing.md", "briefing.html"):
            p = run.path / candidate
            if p.is_file():
                extras.append(p)

    return BugReport(
        title=final_title,
        body=body,
        path=report_path,
        extra_attachments=tuple(extras),
    )


def write_report(report: BugReport) -> Path:
    """Write `report.body` to `report.path`. Returns the path written."""
    report.path.parent.mkdir(parents=True, exist_ok=True)
    report.path.write_text(report.body, encoding="utf-8")
    return report.path


def gh_command(report: BugReport, *, repo: str = DEFAULT_BUG_REPO) -> list[str]:
    """Build the `gh issue create` argv that would file this report.

    Returned as a list so callers can `subprocess.run` it or print it
    verbatim when `gh` is unavailable. The body is passed via
    `--body-file` so newlines and code fences survive the shell.
    """
    return [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        report.title,
        "--body-file",
        str(report.path),
        "--label",
        "nightly:self-report",
    ]


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of `submit_report` — never raises; failure is in the dataclass."""

    ok: bool
    issue_url: str | None
    error: str | None
    command: tuple[str, ...]


def submit_report(
    report: BugReport,
    *,
    repo: str = DEFAULT_BUG_REPO,
    runner: subprocess.CompletedProcess | None = None,  # type: ignore[type-arg]
) -> SubmitResult:
    """Run `gh issue create` for `report`. Best-effort; never raises.

    `runner` is an injection point for tests — pass a
    `subprocess.CompletedProcess` to skip the real `subprocess.run`.
    Production callers leave it `None` and rely on `shutil.which("gh")`.
    """
    cmd = tuple(gh_command(report, repo=repo))

    if runner is None:
        if shutil.which("gh") is None:
            return SubmitResult(
                ok=False,
                issue_url=None,
                error="gh CLI not on PATH — report written to disk only",
                command=cmd,
            )
        try:
            completed = subprocess.run(
                list(cmd),
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return SubmitResult(
                ok=False,
                issue_url=None,
                error=f"gh invocation failed: {exc!r}",
                command=cmd,
            )
    else:
        completed = runner

    if completed.returncode != 0:
        return SubmitResult(
            ok=False,
            issue_url=None,
            error=(completed.stderr or completed.stdout or "").strip() or "gh exited non-zero",
            command=cmd,
        )
    url = _extract_issue_url(completed.stdout or "")
    return SubmitResult(ok=True, issue_url=url, error=None, command=cmd)


# ── section builders ──────────────────────────────────────────────────────


def _render_header(*, run, stamp: str, root: Path) -> str:
    lines = [
        "# Nightly bug report",
        "",
        f"- **Generated:** `{stamp}`",
        f"- **Nightly version:** `{__version__}`",
        f"- **Repo:** `{root}`",
        f"- **Run id:** `{run.id if run else '(none)'}`",
        f"- **Run concluded:** `{run.is_concluded if run else 'n/a'}`",
    ]
    return "\n".join(lines)


def _render_markers_section(run) -> str:
    if run is None:
        return "## Run markers\n\n_No active run — nothing under `.nightly/runs/CURRENT`._\n"
    markers = ("SESSION_ACTIVE", "CONCLUDE", "STOP", "keepalive.turns")
    lines = ["## Run markers", ""]
    for name in markers:
        path = run.path / name
        if path.is_file():
            stat = path.stat()
            content = ""
            try:
                content = path.read_text(encoding="utf-8").strip()
            except OSError:
                content = "(unreadable)"
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            value = f"`{content}`" if content else "_(empty)_"
            lines.append(f"- ✓ `{name}` — mtime `{mtime}`, content {value}")
        else:
            lines.append(f"- ✗ `{name}` — absent")
    return "\n".join(lines)


def _render_keepalive_log_section(run) -> str:
    if run is None:
        return ""
    log = run.path / "keepalive.log"
    if not log.is_file():
        return "## Keepalive log\n\n_No `keepalive.log` for this run._\n"
    try:
        text = log.read_text(encoding="utf-8")
    except OSError:
        return "## Keepalive log\n\n_(unreadable)_\n"
    lines = text.splitlines()
    tail = lines[-_MAX_LOG_LINES:]
    truncated_note = ""
    if len(lines) > _MAX_LOG_LINES:
        truncated_note = (
            f"\n_(showing last {_MAX_LOG_LINES} of {len(lines)} lines — "
            f"full log at `{log}`)_\n"
        )
    return (
        "## Keepalive log\n\n"
        f"{truncated_note}"
        "```\n"
        + "\n".join(tail)
        + "\n```\n"
    )


def _render_plans_section(root: Path) -> str:
    # Local import — plans module pulls in run dataclass plumbing we don't
    # want to import at module top to keep `nightly bug` fast.
    from nightly_core.plans import list_plans  # noqa: PLC0415

    try:
        plans = list_plans(root)
    except Exception as exc:  # bug command must never crash
        return f"## Plans\n\n_Could not list plans: {exc!r}_\n"
    if not plans:
        return "## Plans\n\n_No plans found across runs._\n"
    lines = ["## Plans", "", "| status | run | slug |", "|---|---|---|"]
    for p in plans:
        # Escape pipe chars in slugs/status to keep table well-formed.
        status = p.status.replace("|", "\\|")
        run_id = p.run_id.replace("|", "\\|")
        slug = p.slug.replace("|", "\\|")
        lines.append(f"| `{status}` | `{run_id}` | `{slug}` |")
    return "\n".join(lines)


def _render_briefing_section(run) -> str:
    if run is None:
        return ""
    briefing = run.path / "briefing.md"
    if not briefing.is_file():
        return "## Last briefing\n\n_No `briefing.md` for this run._\n"
    try:
        text = briefing.read_text(encoding="utf-8")
    except OSError:
        return "## Last briefing\n\n_(unreadable)_\n"
    # Cap the embedded briefing so we don't drown the report.
    cap = 4000
    body = text if len(text) <= cap else text[:cap] + "\n…(truncated)…\n"
    return "## Last briefing\n\n```\n" + body.rstrip() + "\n```\n"


def _render_status_section(root: Path) -> str:
    out = _run_subprocess(["nightly", "status"], cwd=root)
    return "## `nightly status`\n\n```\n" + out + "\n```\n"


def _render_next_section(root: Path) -> str:
    out = _run_subprocess(["nightly", "next"], cwd=root)
    return "## `nightly next`\n\n```\n" + out + "\n```\n"


def _render_git_section(root: Path) -> str:
    log = _run_subprocess(
        [
            "git",
            "log",
            "--oneline",
            "--decorate",
            f"-n{_MAX_GIT_LOG_ENTRIES}",
        ],
        cwd=root,
    )
    status = _run_subprocess(["git", "status", "--short", "--branch"], cwd=root)
    return (
        "## Git\n\n"
        "### Recent commits\n\n```\n"
        + log
        + "\n```\n\n"
        "### Working tree\n\n```\n"
        + status
        + "\n```\n"
    )


def _render_environment_section(root: Path) -> str:
    rules_block = _extract_rules_block(root)
    if rules_block is None:
        return "## Rules block (AGENTS.md / CLAUDE.md)\n\n_No marker-delimited block found._\n"
    return (
        "## Rules block (AGENTS.md / CLAUDE.md)\n\n"
        "```markdown\n"
        + rules_block.rstrip()
        + "\n```\n"
    )


# ── helpers ───────────────────────────────────────────────────────────────


def _run_subprocess(argv: list[str], *, cwd: Path) -> str:
    """Run `argv` and return stdout (best-effort). Stderr is folded in on failure."""
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as exc:
        return f"(command failed: {exc!r})"
    out = (result.stdout or "").rstrip()
    err = (result.stderr or "").rstrip()
    if result.returncode != 0 and err:
        return f"{out}\n[exit {result.returncode}]\n{err}".strip()
    return out or "(no output)"


def _extract_rules_block(root: Path) -> str | None:
    """Pull the marker-delimited Nightly rules block out of AGENTS.md/CLAUDE.md."""
    from nightly_core.rules import MARKER_END, MARKER_START  # noqa: PLC0415

    for filename in ("AGENTS.md", "CLAUDE.md"):
        path = root / filename
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if MARKER_START in text and MARKER_END in text:
            start = text.index(MARKER_START)
            end = text.index(MARKER_END) + len(MARKER_END)
            return f"({filename})\n\n" + text[start:end]
    return None


# `gh issue create` prints the issue URL as the last line of stdout on success.
_GH_URL_RE = re.compile(r"https?://[^\s]+/issues/\d+")


def _extract_issue_url(stdout: str) -> str | None:
    matches = _GH_URL_RE.findall(stdout)
    return matches[-1] if matches else None
