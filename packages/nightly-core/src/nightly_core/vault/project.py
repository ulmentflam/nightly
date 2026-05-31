"""Project run artifacts into vault markdown.

`project_run(run_id, repo_root)` walks `.nightly/runs/<run-id>/` and writes:
- one run node (`vault/runs/<run-id>.md`) — body sourced from briefing.md
- one task node per `tasks/<n>-<slug>/plan.md` — body aggregates the
  plan + notes + proposal + uncertainty sections
- one lesson node per bullet in `lessons.md` — title parsed from the
  bold-prefixed first phrase

Projection is idempotent — re-running overwrites existing vault files,
which is the desired behavior (the run dir is the source of truth, the
vault is its rendered projection).

PR nodes have a separate writer: `project_pr()` mints one node for a
single PR, and `backfill_prs()` walks `gh pr list` to write any nodes
that are missing. PR creation in this codebase happens in the agent's
shell (not in Python) so there's no in-process `gh pr create` site to
hook — backfill on every `nightly vault build` is the v0 strategy
(RFC 003 Fork 01 adjusted: dispatch-inline is replaced with
build-time-backfill since no Python writer site exists).

Feedback nodes are written by `backfill_feedback()`, which walks every
PR node already in `vault/pulls/` and calls `pr_feedback.fetch_feedback`
for each branch. Each feedback item becomes a stable, idempotent node
under `vault/feedback/<pr_number>--<sha>.md`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nightly_core.plans import parse_frontmatter

from ._render import render_node
from .model import (
    Node,
    node_id_for_dispatch,
    node_id_for_feedback,
    node_id_for_lesson,
    node_id_for_pr,
    node_id_for_run,
    node_id_for_task,
    node_kind_dir,
)

__all__ = [
    "ProjectionResult",
    "backfill_feedback",
    "backfill_prs",
    "project_pr",
    "project_run",
    "vault_root_for",
]


@dataclass(frozen=True)
class ProjectionResult:
    """The outcome of one `project_run()` call.

    `paths_written` records every file the projection touched, in
    deterministic order (run → tasks → dispatches → lessons). Used by
    the CLI to print a summary and by tests to assert idempotence.
    """

    run_id: str
    run_node: Node
    task_nodes: tuple[Node, ...]
    dispatch_nodes: tuple[Node, ...]
    lesson_nodes: tuple[Node, ...]
    paths_written: tuple[Path, ...]


_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
"""First H1 heading on a markdown document — used to derive node titles."""

_LESSON_TITLE_RE = re.compile(r"\*\*([^*]+?)\*\*")
"""First **bold** phrase in a lesson bullet — used as the lesson title."""

_STALE_SESSION_AFTER = 4 * 60 * 60  # seconds


def vault_root_for(repo_root: Path) -> Path:
    """Where the vault lives under the repo root. Centralized so the path
    convention has one place to change."""
    return repo_root / ".nightly" / "vault"


def project_run(
    run_id: str,
    *,
    repo_root: Path,
    vault_root: Path | None = None,
) -> ProjectionResult:
    """Project one run's artifacts into the vault.

    Caller passes an explicit `repo_root` so tests can use a `tmp_path`
    without relying on git's view of the cwd. `vault_root` defaults to
    `<repo_root>/.nightly/vault/`.

    Raises `FileNotFoundError` if the run dir doesn't exist. Missing
    artifacts within an existing run (no briefing, no lessons) are
    handled gracefully — the projection writes what it can.
    """
    run_path = repo_root / ".nightly" / "runs" / run_id
    if not run_path.is_dir():
        msg = f"run not found: {run_path}"
        raise FileNotFoundError(msg)

    target_root = vault_root if vault_root is not None else vault_root_for(repo_root)

    run_node = _project_run_node(run_id, run_path)
    task_nodes = tuple(_project_task_nodes(run_id, run_path))
    dispatch_nodes = tuple(_project_dispatch_nodes(run_id, run_path))
    lesson_nodes = tuple(_project_lesson_nodes(run_id, run_path / "lessons.md"))

    # The run node's `spawned` edges are derived from the projected task
    # nodes — we couldn't populate them until we'd walked the tasks dir.
    if task_nodes:
        spawned_ids = tuple(t.id for t in task_nodes)
        run_node.edges["spawned"] = spawned_ids

    paths: list[Path] = []
    paths.append(_write_node(run_node, target_root))
    for task in task_nodes:
        paths.append(_write_node(task, target_root))
    for dispatch in dispatch_nodes:
        paths.append(_write_node(dispatch, target_root))
    for lesson in lesson_nodes:
        paths.append(_write_node(lesson, target_root))

    return ProjectionResult(
        run_id=run_id,
        run_node=run_node,
        task_nodes=task_nodes,
        dispatch_nodes=dispatch_nodes,
        lesson_nodes=lesson_nodes,
        paths_written=tuple(paths),
    )


# ── per-kind projectors ───────────────────────────────────────────────────


def _project_run_node(run_id: str, run_path: Path) -> Node:
    briefing = _read_text_or_empty(run_path / "briefing.md")
    title = _first_h1(briefing) or f"Run {run_id}"
    body = _strip_first_h1(briefing).strip()
    status = _run_status(run_path)
    created = _run_id_to_iso(run_id)
    updated = _latest_mtime_iso(run_path)
    return Node(
        id=node_id_for_run(run_id),
        kind="run",
        title=title,
        status=status,
        created=created,
        updated=updated,
        body=body,
    )


def _project_task_nodes(run_id: str, run_path: Path) -> list[Node]:
    tasks_dir = run_path / "tasks"
    if not tasks_dir.is_dir():
        return []

    nodes: list[Node] = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        plan_path = task_dir / "plan.md"
        if not plan_path.is_file():
            continue
        node = _project_task_node(run_id, task_dir, plan_path)
        if node is not None:
            nodes.append(node)
    return nodes


def _project_task_node(run_id: str, task_dir: Path, plan_path: Path) -> Node | None:
    text = plan_path.read_text(encoding="utf-8")
    metadata, plan_body = parse_frontmatter(text)
    slug = metadata.get("slug") or task_dir.name
    title = _first_h1(plan_body) or slug

    body = _strip_first_h1(plan_body).strip()
    for extra_name in ("notes.md", "proposal.md", "uncertainty.md"):
        extra_path = task_dir / extra_name
        if not extra_path.is_file():
            continue
        extra_text = extra_path.read_text(encoding="utf-8")
        _, extra_body = parse_frontmatter(extra_text)
        extra_body = extra_body.strip()
        if not extra_body:
            continue
        heading = extra_name.removesuffix(".md").capitalize()
        body = (
            f"{body}\n\n## {heading}\n\n{extra_body}" if body else f"## {heading}\n\n{extra_body}"
        )

    data: dict[str, Any] = {}
    if "proposer_fingerprint" in metadata:
        data["proposer_fingerprint"] = metadata["proposer_fingerprint"]
    if "task_number" in metadata:
        raw = metadata["task_number"]
        data["task_number"] = int(raw) if raw.isdigit() else raw

    return Node(
        id=node_id_for_task(run_id, slug),
        kind="task",
        title=title.strip(),
        status=metadata.get("status") or "ready",
        created=metadata.get("created"),
        updated=metadata.get("updated"),
        data=data,
        edges={"parent": (node_id_for_run(run_id),)},
        body=body,
    )


def _project_dispatch_nodes(run_id: str, run_path: Path) -> list[Node]:  # noqa: PLR0912, PLR0915
    """Project per-task `dispatch.json` records into dispatch nodes.

    Each task may have at most one `dispatch.json` (the last-recorded
    background-specialist invocation for that task). We emit one
    dispatch node per file, linked to its task via `parent`. v1 only
    captures the latest dispatch; multi-dispatch history is a follow-up.
    """
    tasks_dir = run_path / "tasks"
    if not tasks_dir.is_dir():
        return []
    nodes: list[Node] = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        dispatch_json = task_dir / "dispatch.json"
        if not dispatch_json.is_file():
            continue
        try:
            payload = json.loads(dispatch_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        # Resolve the task slug — prefer the plan.md frontmatter so the
        # ID matches the task node's ID exactly. Fall back to dir name.
        slug = task_dir.name
        plan = task_dir / "plan.md"
        if plan.is_file():
            try:
                metadata, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
                slug = metadata.get("slug") or slug
            except OSError:
                pass

        role = str(payload.get("role") or "specialist")
        host = str(payload.get("host") or "unknown")
        status = str(payload.get("status") or "unknown")
        started = payload.get("started_at")
        finished = payload.get("finished_at")
        pid = payload.get("pid")
        exit_code = payload.get("exit_code")
        log_path = payload.get("log_path")

        data: dict[str, Any] = {
            "specialist": role,
            "host": host,
        }
        if isinstance(pid, int):
            data["pid"] = pid
        if isinstance(exit_code, int):
            data["exit_code"] = exit_code

        # Best-effort duration in seconds.
        if isinstance(started, str) and isinstance(finished, str):
            try:
                t_start = datetime.fromisoformat(started.replace("Z", "+00:00"))
                t_end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
                data["duration_s"] = max(0, int((t_end - t_start).total_seconds()))
            except (ValueError, TypeError):
                pass

        body_parts = [
            f"Background dispatch for [[{node_id_for_task(run_id, slug)}]].",
            f"- **Specialist role:** `{role}`",
            f"- **Host:** `{host}`",
            f"- **Status:** `{status}`",
        ]
        if isinstance(pid, int):
            body_parts.append(f"- **PID:** `{pid}`")
        if isinstance(exit_code, int):
            body_parts.append(f"- **Exit code:** `{exit_code}`")
        if log_path:
            body_parts.append(f"- **Log:** `{log_path}`")

        nodes.append(
            Node(
                id=node_id_for_dispatch(run_id, slug, 1),
                kind="dispatch",
                title=f"{role} · {slug}",
                status=status,
                created=_str_or_none(started),
                updated=_str_or_none(finished) or _str_or_none(started),
                data=data,
                edges={"parent": (node_id_for_task(run_id, slug),)},
                body="\n".join(body_parts),
            )
        )
    return nodes


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v or None
    return str(v)


def _project_lesson_nodes(run_id: str, lessons_path: Path) -> list[Node]:
    if not lessons_path.is_file():
        return []
    text = lessons_path.read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    body = _strip_first_h1(body)

    chunks = _split_lesson_bullets(body)
    nodes: list[Node] = []
    for i, chunk in enumerate(chunks, start=1):
        chunk_text = chunk.strip()
        if not chunk_text:
            continue
        title = _extract_lesson_title(chunk_text) or f"Lesson {i}"
        nodes.append(
            Node(
                id=node_id_for_lesson(run_id, i),
                kind="lesson",
                title=title,
                status=None,
                created=_run_id_to_iso(run_id),
                updated=None,
                edges={"parent": (node_id_for_run(run_id),)},
                body=chunk_text,
            )
        )
    return nodes


# ── helpers ───────────────────────────────────────────────────────────────


def _split_lesson_bullets(body: str) -> list[str]:
    """Split a lessons.md body into per-bullet chunks.

    A bullet starts at a line beginning with `- ` and continues until the
    next such line. Indented continuation lines stay attached to the
    bullet they continue.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    for raw_line in body.splitlines(keepends=True):
        if raw_line.startswith("- "):
            if current:
                chunks.append(current)
            current = [raw_line]
        elif current:
            current.append(raw_line)
        # Lines before the first bullet (e.g. a stray paragraph) are ignored.
    if current:
        chunks.append(current)
    return ["".join(chunk) for chunk in chunks]


def _extract_lesson_title(text: str) -> str | None:
    m = _LESSON_TITLE_RE.search(text)
    if not m:
        return None
    title = m.group(1).strip().rstrip(".").strip()
    return title or None


def _first_h1(text: str) -> str | None:
    m = _H1_RE.search(text)
    if not m:
        return None
    return m.group(1).strip()


def _strip_first_h1(text: str) -> str:
    return _H1_RE.sub("", text, count=1).lstrip()


def _read_text_or_empty(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _run_id_to_iso(run_id: str) -> str:
    """Convert filesystem-safe run id (`2026-05-27T16-30-35Z`) to canonical
    ISO 8601 (`2026-05-27T16:30:35Z`). Leaves unrecognized inputs alone."""
    if "T" not in run_id:
        return run_id
    date_part, sep, time_part = run_id.partition("T")
    return f"{date_part}{sep}{time_part.replace('-', ':')}"


def _run_status(run_path: Path) -> str:
    """Best-effort run status from on-disk markers.

    Returns:
    - `aborted` if `STOP` marker exists
    - `concluded` if `CONCLUDE` marker exists, or `briefing.md` exists with no fresh `SESSION_ACTIVE`
    - `active` if `SESSION_ACTIVE` is fresh (<4h old)
    - `concluded` otherwise (safe default for old runs)
    """
    if (run_path / "STOP").is_file():
        return "aborted"
    if (run_path / "CONCLUDE").is_file():
        return "concluded"
    session = run_path / "SESSION_ACTIVE"
    if session.is_file():
        age = datetime.now(UTC).timestamp() - session.stat().st_mtime
        if age < _STALE_SESSION_AFTER:
            return "active"
    return "concluded"


def _latest_mtime_iso(path: Path) -> str | None:
    """Latest mtime among the run's regular files, as ISO 8601 UTC."""
    latest: float | None = None
    for entry in path.rglob("*"):
        if not entry.is_file():
            continue
        mtime = entry.stat().st_mtime
        if latest is None or mtime > latest:
            latest = mtime
    if latest is None:
        return None
    return datetime.fromtimestamp(latest, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_node(node: Node, vault_root: Path) -> Path:
    """Write a node to its on-disk location under `vault_root`, creating
    parents as needed. Returns the path written."""
    kind_dir = vault_root / node_kind_dir(node.kind)
    kind_dir.mkdir(parents=True, exist_ok=True)
    filename = node.id.split("/", 1)[1] + ".md"
    target = kind_dir / filename
    target.write_text(render_node(node), encoding="utf-8")
    return target


# ── PR node writers ────────────────────────────────────────────────────────


def project_pr(  # noqa: PLR0913 - keyword-only args are intentional for clarity
    *,
    pr_number: int,
    title: str | None,
    branch: str,
    url: str,
    ci_state: str | None = None,
    merge_state: str | None = None,
    source_task_id: str | None = None,
    repo_root: Path,
    vault_root: Path | None = None,
) -> Path:
    """Write one `vault/pulls/<num>.md` node.

    `source_task_id`, if provided, becomes the PR's `derived_from` edge —
    the task that produced it. Callers that don't know it can pass None;
    the PR still gets a node but is graph-isolated until a later pass
    backfills the link.
    """
    target_root = vault_root if vault_root is not None else vault_root_for(repo_root)
    edges: dict[Any, Any] = {}
    if source_task_id:
        edges["derived_from"] = (source_task_id,)

    data: dict[str, Any] = {
        "number": pr_number,
        "url": url,
        "branch": branch,
    }
    if ci_state:
        data["ci"] = ci_state
    if merge_state:
        data["merge_state"] = merge_state

    node = Node(
        id=node_id_for_pr(pr_number),
        kind="pr",
        title=title or f"PR #{pr_number}",
        status=merge_state or "open",
        created=None,
        updated=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        data=data,
        edges=edges,
    )
    return _write_node(node, target_root)


def backfill_prs(
    repo_root: Path,
    *,
    vault_root: Path | None = None,
    branch_prefix: str = "nightly/",
) -> list[Path]:
    """Walk `gh pr list` for Nightly-authored PRs and project a node for each.

    Returns the list of paths written. Best-effort — if `gh` is missing
    or the call fails, returns `[]` rather than raising.

    Source-task linkage is heuristic: any task slug in the run dirs that
    appears as a substring of the PR branch name becomes the
    `derived_from` edge. False positives are possible but rare given the
    branch-name conventions Nightly uses.
    """
    target_root = vault_root if vault_root is not None else vault_root_for(repo_root)
    if shutil.which("gh") is None:
        return []
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "all",
                "--limit",
                "200",
                "--json",
                "number,title,headRefName,url,state,mergeStateStatus,statusCheckRollup",
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []

    paths: list[Path] = []
    task_id_lookup = _task_id_lookup_for(repo_root)
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        branch = str(entry.get("headRefName") or "")
        if not branch.startswith(branch_prefix):
            continue
        try:
            num = int(entry.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if num <= 0:
            continue
        title = str(entry.get("title") or f"PR #{num}")
        url = str(entry.get("url") or "")
        state = str(entry.get("state") or "OPEN").lower()
        merge_state = str(entry.get("mergeStateStatus") or "").lower() or None
        ci = _summarize_status_checks(entry.get("statusCheckRollup"))
        source_task_id = _guess_source_task(branch, task_id_lookup)
        paths.append(
            project_pr(
                pr_number=num,
                title=title,
                branch=branch,
                url=url,
                ci_state=ci,
                merge_state=state if state != "open" else merge_state,
                source_task_id=source_task_id,
                repo_root=repo_root,
                vault_root=target_root,
            )
        )
    return paths


def _summarize_status_checks(rollup: Any) -> str | None:
    """Reduce `gh`'s statusCheckRollup payload to one of failing/pending/passing."""
    if not isinstance(rollup, list) or not rollup:
        return None
    states: set[str] = set()
    for entry in rollup:
        if not isinstance(entry, dict):
            continue
        # check_run uses `conclusion`/`status`; status_context uses `state`
        state = str(
            entry.get("state") or entry.get("conclusion") or entry.get("status") or ""
        ).upper()
        if state:
            states.add(state)
    if any(s in states for s in ("FAILURE", "FAILED", "ERROR", "TIMED_OUT")):
        return "failing"
    if any(s in states for s in ("PENDING", "IN_PROGRESS", "QUEUED")):
        return "pending"
    if "SUCCESS" in states or "COMPLETED" in states:
        return "passing"
    return None


def _task_id_lookup_for(repo_root: Path) -> dict[str, str]:
    """Build a {task_slug: task_node_id} lookup across every run on disk.

    Used by `_guess_source_task` to resolve a PR's branch back to a task.
    Cheap — each plan.md only needs its frontmatter parsed.
    """
    lookup: dict[str, str] = {}
    runs_dir = repo_root / ".nightly" / "runs"
    if not runs_dir.is_dir():
        return lookup
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        tasks_dir = run_dir / "tasks"
        if not tasks_dir.is_dir():
            continue
        for task_dir in tasks_dir.iterdir():
            plan = task_dir / "plan.md"
            if not plan.is_file():
                continue
            try:
                metadata, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
            except OSError:
                continue
            slug = metadata.get("slug") or task_dir.name
            lookup[slug] = node_id_for_task(run_dir.name, slug)
    return lookup


def _guess_source_task(branch: str, task_lookup: dict[str, str]) -> str | None:
    """Return the task_id whose slug appears in the PR branch, or None."""
    for slug, task_id in task_lookup.items():
        if slug in branch:
            return task_id
    return None


# ── Feedback node writers ──────────────────────────────────────────────────

_BRANCH_LINE_RE = re.compile(r"^\s+branch:\s+(.+)$", re.MULTILINE)
"""Match `  branch: <value>` inside a PR node's `data:` block."""


def _feedback_sha(url: str, body: str) -> str:
    """12-hex-char SHA-256 of url+body — stable, idempotent across re-runs."""
    digest = hashlib.sha256((url + "\x00" + body).encode()).hexdigest()
    return digest[:12]


def _feedback_status(feedback: Any) -> str:
    """Map a PRFeedback to a status pill.

    - `praise` when it's an APPROVED review
    - `blocking` when `feedback.is_blocking` is True
    - `nit` otherwise (normal comments, bot summaries, etc.)
    """
    if feedback.kind == "review" and feedback.state == "APPROVED":
        return "praise"
    if feedback.is_blocking:
        return "blocking"
    return "nit"


def backfill_feedback(
    repo_root: Path,
    *,
    vault_root: Path | None = None,
) -> list[Path]:
    """Walk every PR node in `vault/pulls/` and mint feedback nodes for each.

    For each `vault/pulls/<num>.md` file the function:
    1. Reads the frontmatter to extract the PR number (from the filename)
       and the branch (from the `data.branch` line).
    2. Calls `pr_feedback.fetch_feedback(branch, root=repo_root)`.
    3. Writes one `vault/feedback/<num>--<sha>.md` node per item.

    Returns the list of paths written. Best-effort — if `gh` is missing
    or `fetch_feedback` raises for a given PR, that PR is skipped and the
    loop continues. Never propagates exceptions.
    """
    from nightly_core import pr_feedback  # noqa: PLC0415 — lazy to avoid circular at module scope

    target_root = vault_root if vault_root is not None else vault_root_for(repo_root)
    pulls_dir = target_root / "pulls"
    if not pulls_dir.is_dir():
        return []

    paths: list[Path] = []

    for pr_file in sorted(pulls_dir.glob("*.md")):
        stem = pr_file.stem
        try:
            pr_number = int(stem)
        except ValueError:
            continue

        # Extract the branch from the data block.  parse_frontmatter only
        # handles flat key: value lines so we search the raw text instead.
        try:
            raw = pr_file.read_text(encoding="utf-8")
        except OSError:
            continue

        m = _BRANCH_LINE_RE.search(raw)
        if not m:
            continue
        branch = m.group(1).strip()
        if not branch:
            continue

        # Fetch feedback items — best-effort.
        try:
            items = pr_feedback.fetch_feedback(branch, root=repo_root)
        except Exception:  # best-effort: gh unavailable, network error, etc.
            continue

        for item in items:
            sha = _feedback_sha(item.url, item.body)
            node = Node(
                id=node_id_for_feedback(pr_number, sha),
                kind="feedback",
                title=f"{item.kind} by {item.author_login}",
                status=_feedback_status(item),
                created=item.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                updated=None,
                data={
                    "kind": item.kind,
                    "author_login": item.author_login,
                    "author_is_bot": item.author_is_bot,
                    "state": item.state,
                    "file_ref": item.file_ref,
                    "line_ref": item.line_ref,
                    "url": item.url,
                },
                edges={"derived_from": (node_id_for_pr(pr_number),)},
                body=item.body,
            )
            paths.append(_write_node(node, target_root))

    return paths
