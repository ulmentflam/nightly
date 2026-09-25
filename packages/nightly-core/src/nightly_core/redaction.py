"""Redaction — the contract for what leaves the operator's machine.

Nightly's run state is unusually revealing. A bug report bundles absolute
paths under `$HOME`, worktree roots, git remote URLs with hostnames,
branch names, plan slugs like `auth-rewrite-for-compliance-q3`, and
free-text `uncertainty.md` prose that may name internal systems. Today
that goes into a public GitHub issue.

The contract has two halves and the split is the whole design:

- **On-disk state is never redacted.** `.nightly/runs/<id>/` is the
  operator's own audit trail; redacting it would destroy the forensics
  it exists to provide.
- **Anything leaving the machine is always redacted.** `nightly bug`
  bodies, `--share` briefing output, `nightly supervisor status --share`.
  No opt-out on those paths.

Redaction here is *category-preserving*: a path becomes `~/<sha8>/...`,
not `[REDACTED]`. The reader of a bug report still learns "the same file
appeared twice" and "this was under `$HOME`" — the structural facts that
make a report debuggable — without learning what the file was. Hashes
are content-addressed by the original string, so repeat occurrences
collapse to the same token within a report.

Every pass writes its substitutions to a map that stays local
(`<report>.redaction-map.json`), so the operator can audit what was
removed before submitting, and decode it later if a maintainer asks a
follow-up question.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "RedactionConfig",
    "RedactionResult",
    "load_redaction_config",
    "redact",
    "write_redaction_map",
]

_log = logging.getLogger(__name__)


# ── configuration ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RedactionConfig:
    """The `redaction:` block plus any project-specific catalog."""

    llm_backstop: bool = True
    """Run the LLM pass after the pattern passes on `--share` payloads.

    On by default: the pattern passes catch known *shapes*, and the
    strings most likely to embarrass an operator (a client codename, an
    internal service name) have no shape at all. Failures degrade to the
    pattern passes silently — the report still ships."""

    project_specifics: tuple[tuple[str, str], ...] = ()
    """Literal `(pattern, replacement)` pairs from `.nightly/redaction.yml`
    and `~/.config/nightly/redaction.yml`, repo-local first."""


def load_redaction_config(root: Path | None = None) -> RedactionConfig:
    """Assemble redaction settings from config.yml + both redaction.yml files.

    The per-user catalog (`~/.config/nightly/redaction.yml`) and the
    per-repo one are concatenated, repo-local first. Both are optional
    and any parse failure degrades to "no project specifics" rather than
    raising — a typo in the catalog must not block a bug report, it just
    means fewer literal substitutions.
    """
    import yaml  # noqa: PLC0415

    from nightly_core.config import _coerce_bool, _load_block  # noqa: PLC0415
    from nightly_core.paths import nightly_dir  # noqa: PLC0415

    defaults = RedactionConfig()
    block = _load_block("redaction", root) or {}
    llm_backstop = _coerce_bool(block.get("llm_backstop"), defaults.llm_backstop)

    pairs: list[tuple[str, str]] = []
    candidates = [
        nightly_dir(root) / "redaction.yml",
        Path.home() / ".config" / "nightly" / "redaction.yml",
    ]
    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            _log.warning("ignoring malformed %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        entries = data.get("project_specifics")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pattern = entry.get("pattern")
            replacement = entry.get("replacement", "<redacted>")
            if isinstance(pattern, str) and pattern:
                pairs.append((pattern, str(replacement)))

    return RedactionConfig(llm_backstop=llm_backstop, project_specifics=tuple(pairs))


# ── result ────────────────────────────────────────────────────────────────


@dataclass
class RedactionResult:
    """Redacted text plus the local-only map of what was substituted."""

    text: str
    substitutions: dict[str, str] = field(default_factory=dict)
    """`original → replacement`. Stays on the operator's machine."""

    passes_applied: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.substitutions)


def _token(value: str, prefix: str = "") -> str:
    """Stable 8-hex-char tag for `value`, so repeats collapse together."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{digest}"


# ── pattern passes ────────────────────────────────────────────────────────

# Ordering matters and is load-bearing. Specific-and-structured first
# (URLs, emails), general-and-greedy last (bare paths, hostnames) — a
# greedy path pass run first would eat the path component of a URL and
# leave the host visible, which is exactly backwards.

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"')\]]+", re.IGNORECASE)
_GIT_SSH_RE = re.compile(r"\bgit@[\w.-]+:[\w./-]+(?:\.git)?")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")
_TOKEN_RE = re.compile(r"\b(?=[\w]{20,})(?=[\w]*[a-z])(?=[\w]*[A-Z])(?=[\w]*\d)[\w]{20,}\b")
"""Long mixed-case-with-digits runs — the shape of an API key.

Underscores only, no dashes: including `-` made this swallow ISO-8601
run ids and dashed branch slugs, which are structure the report needs."""

_NIGHTLY_BRANCH_RE = re.compile(
    r"\bnightly/(?:wip-)?([\w.-]+?)(?:-\d{4}-\d{2}-\d{2}T[\d:-]+Z?)?(?=[\s\"'`,;:)\]}]|$)"
)
"""`nightly/<slug>` with an optional trailing run-id timestamp.

The lookahead is what makes the optional timestamp group actually fire:
with a plain optional suffix the non-greedy slug matches as little as
possible and the group matches empty, leaving the timestamp behind for
another pass to mangle."""

_TASK_SLUG_RE = re.compile(r"\btasks/(\d{1,4})-([\w-]+)")

_MIN_IDENTITY_LEN = 3
"""Shortest hostname / username the identity pass will substitute."""


def _apply(
    text: str,
    pattern: re.Pattern[str],
    replace: str | Callable[[re.Match[str]], str],
    subs: dict[str, str],
) -> str:
    """Run one regex pass, recording every substitution made.

    `replace` is either a literal replacement or a callable taking the
    match — the latter for passes that derive the placeholder from what
    they matched (URLs keep their TLD, branches keep their prefix).
    """

    def _sub(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = replace(match) if callable(replace) else replace
        if original != replacement:
            subs[original] = replacement
        return replacement

    return pattern.sub(_sub, text)


def _redact_urls(text: str, subs: dict[str, str]) -> str:
    """Keep scheme and TLD, drop host detail and the whole path.

    `https://internal.acme.example/deploy/prod-2` tells a reader far more
    than the fact that an HTTPS URL was present, which is all the
    debugging value it carries."""

    def _replace(match: re.Match[str]) -> str:
        url = match.group(0)
        try:
            rest = url.split("://", 1)[1]
        except IndexError:
            return f"<url:{_token(url)}>"
        host = rest.split("/", 1)[0]
        tld = host.rsplit(".", 1)[-1] if "." in host else "invalid"
        return f"https://<host:{_token(host)}>.{tld}/<path-redacted>"

    return _apply(text, _URL_RE, _replace, subs)


def _redact_paths(text: str, subs: dict[str, str], *, home: str) -> str:
    """Tokenize absolute paths, preserving the `$HOME` vs system split.

    A single left-to-right scan over both prefixes (`$HOME` and a literal
    `~`). Two sequential passes would be wrong: the first pass emits
    `~/<path:…>`, which the second then matches and re-redacts into a
    hash of a hash.

    Hand-rolled rather than a regex because path characters overlap prose
    punctuation badly — a pattern greedy enough to catch real paths also
    swallows the sentence's trailing period, and one tuned to avoid that
    misses paths containing spaces, which macOS produces constantly
    ("Mobile Documents", "Application Support").
    """
    prefixes = [p for p in ({home, "~"} if home else {"~"}) if p]
    # Longest first so `/Users/x` wins over a bare `~` at the same offset.
    prefixes.sort(key=len, reverse=True)

    pieces: list[str] = []
    idx = 0
    n = len(text)
    while idx < n:
        best: tuple[int, str] | None = None
        for prefix in prefixes:
            found = text.find(prefix + "/", idx)
            if found != -1 and (best is None or found < best[0]):
                best = (found, prefix)
        if best is None:
            pieces.append(text[idx:])
            break
        found, prefix = best
        pieces.append(text[idx:found])
        end = _path_end(text, found + len(prefix) + 1)
        original = text[found:end]
        replacement = f"~/<path:{_token(original)}>"
        subs[original] = replacement
        pieces.append(replacement)
        idx = end
    return "".join(pieces)


_PATH_STOP = set(" \t\n\r\"'`<>|,;()[]{}")


def _path_end(text: str, start: int) -> int:
    """Find where a filesystem path ends, allowing single interior spaces.

    macOS paths contain spaces routinely, so stopping at the first space
    would truncate `~/Library/Mobile Documents/...` to `~/Library/Mobile`
    and leak the rest. A space is absorbed only when the next character
    starts a plausible path segment (alphanumeric) *and* a `/` follows
    later in the run — otherwise a path at the end of a sentence would
    swallow the sentence.
    """
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _PATH_STOP:
            if ch == " " and _space_is_interior(text, i):
                i += 1
                continue
            break
        i += 1
    # Don't keep trailing sentence punctuation.
    while i > start and text[i - 1] in ".,:;":
        i -= 1
    return i


def _space_is_interior(text: str, idx: int) -> bool:
    """True when the space at `idx` sits inside a path rather than after it."""
    nxt = idx + 1
    if nxt >= len(text) or not (text[nxt].isalnum() or text[nxt] == "."):
        return False
    # Look ahead for a `/` before the next hard stop — the signal that the
    # run continues as a path rather than as prose.
    j = nxt
    while j < len(text) and text[j] not in "\n\r\t\"'`":
        if text[j] == "/":
            return True
        if text[j] == " ":
            # Two spaces in a row, or a second space before any `/` — prose.
            return False
        j += 1
    return False


def _redact_branches(text: str, subs: dict[str, str]) -> str:
    """Hash the slug of a Nightly branch, keep the `nightly/` prefix."""

    def _replace(match: re.Match[str]) -> str:
        slug = match.group(1)
        if not slug:
            return match.group(0)
        return f"nightly/<slug:{_token(slug)}>"

    return _apply(text, _NIGHTLY_BRANCH_RE, _replace, subs)


def _redact_task_slugs(text: str, subs: dict[str, str]) -> str:
    """Keep the task index (ordering signal), hash the slug."""

    def _replace(match: re.Match[str]) -> str:
        return f"tasks/{match.group(1)}-<slug:{_token(match.group(2))}>"

    return _apply(text, _TASK_SLUG_RE, _replace, subs)


def _redact_identity(text: str, subs: dict[str, str]) -> str:
    """Mask the machine hostname and the operator's username.

    Both are matched as literals rather than patterns — there is no shape
    that distinguishes a username from any other short word, so the only
    safe match is the actual value.
    """
    out = text
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    for value, replacement in ((host, "<host>"), (user, "<user>")):
        # Short values match inside unrelated words. A false positive here
        # only mangles prose, which beats a leak — but a two-character
        # username would rewrite half the report, so there is a floor.
        if len(value) < _MIN_IDENTITY_LEN:
            continue
        if value in out:
            subs[value] = replacement
            out = out.replace(value, replacement)
    return out


def _redact_project_specifics(
    text: str,
    subs: dict[str, str],
    pairs: tuple[tuple[str, str], ...],
) -> str:
    """Apply the operator's literal catalog, longest pattern first.

    Longest-first so that a catalog containing both `acme` and
    `acme-payments` doesn't leave `<org>-payments` behind."""
    out = text
    for pattern, replacement in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if pattern in out:
            subs[pattern] = replacement
            out = out.replace(pattern, replacement)
    return out


def redact(
    text: str,
    *,
    config: RedactionConfig | None = None,
    home: str | None = None,
) -> RedactionResult:
    """Run every pattern pass over `text`, in order.

    The LLM backstop is deliberately NOT run here — it needs a host CLI,
    a working network, and a few seconds, none of which belong in a pure
    function. `llm_backstop()` is the separate, optional second stage.
    """
    cfg = config or RedactionConfig()
    subs: dict[str, str] = {}
    home_dir = home if home is not None else str(Path.home())

    out = text
    # Order is load-bearing throughout; each step notes what it must
    # precede or follow.

    # Catalog first: the operator's literals are the only pass that knows
    # what is actually sensitive here, and running it before the shape
    # passes means a catalogued term inside a path still gets its own
    # named replacement rather than vanishing into a path hash.
    out = _redact_project_specifics(out, subs, cfg.project_specifics)
    # URLs and git remotes before email: `git@github.com:org/repo` matches
    # the email pattern, and losing it to `<email>` would misreport a
    # remote as a contact address.
    out = _redact_urls(out, subs)
    out = _apply(out, _GIT_SSH_RE, "<git-remote-redacted>", subs)
    out = _apply(out, _EMAIL_RE, "<email>", subs)
    # Paths before branch/task slugs: a path containing `nightly/<slug>`
    # or `tasks/<n>-<slug>` would otherwise be partially slug-redacted
    # first, leaving a half-hashed path the path pass can no longer match
    # as a unit.
    out = _redact_paths(out, subs, home=home_dir)
    out = _redact_task_slugs(out, subs)
    out = _redact_branches(out, subs)
    out = _apply(out, _IPV4_RE, "<ip>", subs)
    out = _apply(out, _IPV6_RE, "<ip>", subs)
    out = _apply(out, _TOKEN_RE, "<token>", subs)
    # Identity last: hostname and username are substrings of many of the
    # forms above (a `$HOME` path contains the username), so running this
    # first would rewrite path interiors and defeat the path hashing.
    out = _redact_identity(out, subs)

    return RedactionResult(
        text=out,
        substitutions=subs,
        passes_applied=(
            "project_specifics",
            "url",
            "git_remote",
            "email",
            "path",
            "task_slug",
            "branch",
            "ip",
            "token",
            "identity",
        ),
    )


def write_redaction_map(result: RedactionResult, report_path: Path) -> Path | None:
    """Write `<report>.redaction-map.json` next to the report.

    This file is the operator's decoder ring and **must never be
    included in a share payload**. It exists so the operator can answer a
    maintainer's "which file was `<path:a1b2c3d4>`?" without guessing.
    """
    path = report_path.with_suffix(report_path.suffix + ".redaction-map.json")
    payload = {
        "report": report_path.name,
        "passes_applied": list(result.passes_applied),
        "substitutions": result.substitutions,
        "note": "LOCAL ONLY — never attach this file to a shared report.",
    }
    try:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        _log.debug("redaction map write failed for %s: %s", path, exc)
        return None
    return path


# ── LLM backstop ──────────────────────────────────────────────────────────


LLM_BACKSTOP_PROMPT = """\
You are scrubbing a debug report for public posting. Read the redacted \
body below and identify any remaining strings that look like (a) absolute \
filesystem paths, (b) project codenames, (c) personal names, (d) internal-\
system identifiers, (e) anything else that would let a reader infer the \
operator's employer, residence, or workflow. For each finding, return \
{"original": "<text>", "redacted": "<placeholder>"}. The redaction must \
remain useful for debugging — preserve the *category* of the redacted \
thing. Return a JSON array only, no prose.

--- BEGIN REPORT BODY ---
%s
--- END REPORT BODY ---
"""


def llm_backstop(
    result: RedactionResult,
    *,
    root: Path | None = None,
    runner: Any = None,
    timeout: int = 90,
) -> RedactionResult:
    """Second-stage scrub for share payloads: catch what patterns can't.

    Pattern passes match known shapes. The strings that actually
    embarrass an operator — a client codename, an internal service name,
    a colleague's surname in a plan title — have no shape. This pass asks
    a model to find them.

    Degrades silently to `result` on every failure path (no host, no
    network, bad JSON, timeout). A bug report that ships with pattern-only
    redaction is fine; a bug report that fails to ship because the scrub
    stage crashed is not.
    """
    try:
        findings = _run_llm_pass(result.text, root=root, runner=runner, timeout=timeout)
    except Exception as exc:
        _log.debug("LLM redaction backstop unavailable: %s", exc)
        return result

    if not findings:
        return result

    text = result.text
    subs = dict(result.substitutions)
    for finding in findings:
        original = finding.get("original")
        replacement = finding.get("redacted") or "<redacted>"
        if not isinstance(original, str) or not original.strip():
            continue
        if original not in text:
            continue
        subs[original] = str(replacement)
        text = text.replace(original, str(replacement))

    return RedactionResult(
        text=text,
        substitutions=subs,
        passes_applied=(*result.passes_applied, "llm_backstop"),
    )


def _run_llm_pass(
    body: str,
    *,
    root: Path | None,
    runner: Any,
    timeout: int,
) -> list[dict[str, Any]]:
    """Spawn the host CLI headlessly and parse its JSON array reply."""
    prompt = LLM_BACKSTOP_PROMPT % body

    if runner is not None:
        raw = runner(prompt)
    else:
        import subprocess  # noqa: PLC0415

        completed = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root) if root else None,
            check=False,
        )
        if completed.returncode != 0:
            return []
        raw = completed.stdout

    return _parse_findings(raw)


def _parse_findings(raw: str) -> list[dict[str, Any]]:
    """Pull a JSON array out of a model reply that may wrap it in prose."""
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    # Models fence JSON far more often than they don't.
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.removeprefix("json").strip()
            if candidate.startswith("["):
                text = candidate
                break
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]
