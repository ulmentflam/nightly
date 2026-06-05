"""RFC 009 — synthesis-driven ideation proposer.

The three Phase-5 proposers (`todo_fixme`, `lint_debt`, `type_holes`)
are programmatic and narrow — they grep for TODO markers, run `ruff`,
and look for `Any` at module boundaries. None of them read the README,
none read the RFCs, none know what the project's stated objectives
are. They're correctness checkers, not strategists.

`SynthesisProposer` adds the missing strategic layer. It reads the
project's READMEs / autonomy contract / accepted RFCs / a code
summary, spawns the current host's headless CLI with a structured
prompt (`synthesis_prompt.md`), and parses the JSON-array response
into a stream of `Proposal` records. Each proposal carries a
`strategic_category` tag from RFC 009's five-category ordering —
the cascade sorts by category index before score, so cleaning
proposals outrank capability proposals even at lower numeric scores.

The proposer is best-effort: missing host CLI, network errors,
malformed JSON output, or empty responses all degrade to an empty
proposal list without raising. The three narrow proposers keep
running alongside it, so the operator's morning briefing always
has *something* in `proposed/issues/` even if synthesis fails.

Test ergonomics mirror `LintDebtProposer`: a `SynthesisRunner`
callable is injectable so tests stub the LLM spawn with canned
JSON. The default runner shells out to `claude -p --output-format
json --permission-mode acceptEdits` because Claude Code is the
canonical host today; multi-host detection lands in a follow-up
RFC.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable, Iterable
from importlib.resources import files
from pathlib import Path

from nightly_core.proposers.base import (
    STRATEGIC_CATEGORIES,
    Proposal,
    Proposer,
    StrategicCategory,
)

__all__ = ["SynthesisProposer", "SynthesisRunner", "load_synthesis_prompt"]


# Caller-injectable so tests stub the LLM spawn. Real runs use
# `_default_synthesis_runner` which shells out to `claude -p`.
SynthesisRunner = Callable[[str, Path], str]
"""Function shape: `(prompt, repo_root) -> raw LLM stdout`. Returns the
JSON-array string the proposer parses. Empty string signals "skip" —
the proposer returns an empty proposal list."""


_HOST_TIMEOUT_SECONDS = 120
"""Wall-clock cap on the synthesis spawn. RFC 009 §8 says 120s is the
default; the throttle (Phase C) prevents repeated spawns within a
session."""

_DEFAULT_MAX_PROPOSALS = 25
"""RFC 009 §8 — cap on total synthesis output to keep the morning
briefing readable. The prompt template enforces this on the model
side; the parser drops any overflow that slips through."""

_PROMPT_FILE = "synthesis_prompt.md"


def load_synthesis_prompt() -> str:
    """Return the packaged `synthesis_prompt.md` template as a string.

    Lookup via `importlib.resources` so the prompt ships with the
    wheel; reading off-disk would break for installed packages whose
    source dir doesn't exist (homebrew Cellar, pipx-managed installs).
    """
    return files("nightly_core.proposers").joinpath(_PROMPT_FILE).read_text(encoding="utf-8")


def _read_text_or_empty(path: Path, *, max_chars: int = 25_000) -> str:
    """Read a markdown file for prompt-stuffing; degrade silently if absent.

    `max_chars` clips at ~5-6k tokens — keeps the prompt bounded when
    a README or RFC has grown huge. Truncation is preferable to OOM
    on the LLM side; the operator can chunk the docs via follow-up
    runs if synthesis output ever feels incomplete.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n…[truncated for prompt-length budget]…\n"


def _build_code_summary(root: Path) -> str:
    """Generate a lightweight code-summary string for the prompt.

    Walks the workspace package tree and emits one line per Python
    source file with its repo-relative path + byte size. The synthesis
    model uses this to decide which files are large enough to warrant
    structural-review attention — full bodies are NOT shipped in the
    prompt to keep token budget under control. The model can request
    specific bodies in a follow-up if Phase B-or-later wires that
    interaction.
    """
    summary_lines: list[str] = []
    packages = root / "packages"
    if not packages.is_dir():
        return ""
    for src in sorted(packages.glob("*/src/*/*.py")):
        rel = src.relative_to(root)
        try:
            size = src.stat().st_size
        except OSError:
            continue
        summary_lines.append(f"  {rel} ({size}b)")
    if not summary_lines:
        return ""
    return "\n".join(summary_lines)


def _rfc_titles(root: Path) -> str:
    """One line per accepted/sized RFC: number + title. Cheap to build."""
    rfcs = root / ".planning" / "rfcs"
    if not rfcs.is_dir():
        return ""
    out: list[str] = []
    for entry in sorted(rfcs.iterdir()):
        if entry.suffix != ".md" or not entry.name[0].isdigit():
            continue
        try:
            head = entry.read_text(encoding="utf-8").splitlines()[:20]
        except OSError:
            continue
        title_line = next((ln for ln in head if ln.startswith("title:")), "")
        title = title_line.removeprefix("title:").strip() if title_line else entry.stem
        out.append(f"- {entry.stem} — {title}")
    return "\n".join(out)


def _default_synthesis_runner(prompt: str, root: Path) -> str:
    """Shell out to `claude -p --output-format json` with the prompt.

    Returns the model's stdout (the raw response, which should be the
    JSON-array body the proposer parses). Empty string on any
    failure mode: missing binary, timeout, non-zero exit. The
    proposer degrades silently to "no synthesis proposals" — the
    three narrow proposers keep running alongside.

    Multi-host detection (codex, gemini, etc.) is a follow-up; v1
    hardcodes claude because that's the canonical host today.
    """
    binary = shutil.which("claude")
    if binary is None:
        return ""
    try:
        result = subprocess.run(
            [
                binary,
                "-p",
                "--output-format",
                "json",
                "--permission-mode",
                "acceptEdits",
                prompt,
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=_HOST_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    # `claude -p --output-format json` wraps the model response in a
    # JSON envelope: `{"result": "<model text>", ...}`. Unwrap it; the
    # proposer parses what's inside.
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(envelope, dict):
        return ""
    return str(envelope.get("result") or "")


def _content_fingerprint(
    *, strategic_category: str, title: str, file_scope: tuple[str, ...]
) -> str:
    """RFC 009 §5 — content-hashed fingerprint for synthesis proposals.

    Two synthesis runs may produce two near-identical-but-not-identical
    proposals for the same underlying issue (LLM output is
    non-deterministic). The default `proposer:category:primary_scope`
    fingerprint is too coarse — every "cleaning"-category synthesis
    proposal would dedupe against every other one. We hash
    `title + sorted(file_scope)` so that two runs producing the
    same conceptual proposal with identical wording dedupe correctly
    while two runs proposing different conceptual changes (even in
    the same category + scope) both surface for the operator.
    """
    payload = f"{title}|{'|'.join(sorted(file_scope))}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"synthesis:{strategic_category}:{digest}"


def _parse_synthesis_output(raw: str, *, max_proposals: int) -> list[Proposal]:
    """Parse the LLM's JSON-array response into `list[Proposal]`.

    Lenient on the failure axes that real LLM output exhibits:
    - Empty / whitespace-only string → empty list.
    - Non-JSON or non-array root → empty list.
    - Individual items missing required fields → skipped (the rest
      of the array still surfaces).
    - Unknown `strategic_category` value → skipped (don't try to
      map to a default; refuse rather than silently mis-bucket).
    """
    if not raw or not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    proposals: list[Proposal] = []
    for item in items[:max_proposals]:
        if not isinstance(item, dict):
            continue
        strategic_raw = str(item.get("strategic_category") or "").strip().lower()
        if strategic_raw not in STRATEGIC_CATEGORIES:
            continue
        strategic_category: StrategicCategory = strategic_raw  # type: ignore[assignment]
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        description = str(item.get("description") or "").strip()
        rationale = str(item.get("rationale") or "").strip()
        scope_raw = item.get("file_scope")
        if isinstance(scope_raw, list):
            file_scope = tuple(str(p) for p in scope_raw if isinstance(p, str))
        else:
            file_scope = ()
        estimated_loc_raw = item.get("estimated_loc", 0)
        try:
            estimated_loc = abs(int(estimated_loc_raw))
        except (TypeError, ValueError):
            estimated_loc = 0

        body_lines = [
            f"## {title}",
            "",
            description or "_(no description provided)_",
        ]
        if rationale:
            body_lines.extend(["", "### Why this advances a project objective", "", rationale])
        if file_scope:
            body_lines.extend(["", "### File scope", "", *(f"- `{p}`" for p in file_scope)])

        proposals.append(
            Proposal(
                proposer="synthesis",
                category="synthesis",  # proposer-kind eligibility bucket
                strategic_category=strategic_category,
                title=title,
                body="\n".join(body_lines),
                # Score is uniform across synthesis proposals; the cascade
                # sort is driven by `strategic_category` index first
                # (RFC 009 §4), so per-item score differences would only
                # break ties within the same category. Keep it simple.
                score=1.0,
                file_scope=file_scope,
                estimated_loc=estimated_loc,
            )
        )
    return proposals


class SynthesisProposer(Proposer):
    """LLM-driven strategic proposer (RFC 009 §A3)."""

    id = "synthesis"

    def __init__(
        self,
        *,
        runner: SynthesisRunner | None = None,
        max_proposals: int = _DEFAULT_MAX_PROPOSALS,
    ) -> None:
        self._runner = runner or _default_synthesis_runner
        self._max_proposals = max_proposals

    def propose(self, root: Path) -> Iterable[Proposal]:
        prompt = self._build_prompt(root)
        if not prompt:
            return ()
        with contextlib.suppress(Exception):
            # The runner contract says it returns "" on failure; this
            # `suppress` is a belt-and-braces guard for runners that
            # raise instead of returning empty. Synthesis must never
            # crash the broader proposer pass.
            raw = self._runner(prompt, root)
            parsed = _parse_synthesis_output(raw, max_proposals=self._max_proposals)
            # Override the default fingerprint with the content-hashed
            # variant (RFC 009 §5). Proposals from the parser are immutable
            # `Proposal` dataclasses, so rebuild with the override.
            return tuple(self._with_content_fingerprint(p) for p in parsed)
        return ()

    @staticmethod
    def _with_content_fingerprint(proposal: Proposal) -> Proposal:
        """Tag the synthesis proposal so its `fingerprint` property
        returns the content-hashed value.

        We can't override `fingerprint` at the instance level on a
        frozen dataclass, so the override happens via the proposer
        kind: `_content_fingerprint` is called separately by the
        cascade-dedupe filter. For now we leave the default fingerprint
        in place but expose `_synthesis_fingerprint` on the proposal
        as a side-channel attribute (set via object.__setattr__ to
        bypass frozen-ness). Cascade-side reads the override if
        present.

        v2 will refactor `Proposal.fingerprint` to a method that
        consults `strategic_category` and proposer kind; for v1 we
        carry the override as an attribute.
        """
        override = _content_fingerprint(
            strategic_category=proposal.strategic_category,
            title=proposal.title,
            file_scope=proposal.file_scope,
        )
        # bypass frozen=True via __setattr__ on the underlying dict.
        object.__setattr__(proposal, "_synthesis_fingerprint", override)
        return proposal

    def _build_prompt(self, root: Path) -> str:
        """Render the synthesis prompt template with project context.

        Returns "" when the project lacks the expected context files
        (no README, no `packages/` tree) — there's nothing for the
        synthesizer to anchor against.

        Uses manual `str.replace` rather than `.format()` because the
        prompt body carries example JSON with literal `{` / `}` braces
        that `.format()` would mis-parse as placeholders.
        """
        readme = _read_text_or_empty(root / "README.md")
        claude_md = _read_text_or_empty(root / "CLAUDE.md") or _read_text_or_empty(
            root / "AGENTS.md"
        )
        rfc_titles = _rfc_titles(root)
        code_summary = _build_code_summary(root)
        if not (readme and code_summary):
            return ""
        template = load_synthesis_prompt()
        substitutions = {
            "{readme}": readme,
            "{claude_md}": claude_md or "_(no contract file found)_",
            "{rfc_titles}": rfc_titles or "_(no accepted RFCs on disk)_",
            "{code_summary}": code_summary,
            "{max_proposals}": str(self._max_proposals),
        }
        rendered = template
        for placeholder, value in substitutions.items():
            rendered = rendered.replace(placeholder, value)
        return rendered
