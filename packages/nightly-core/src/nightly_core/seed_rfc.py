"""Stub `accepted` RFCs from an interactive seed (RFC 005).

The interactive `/nightly` host skill calls `write_seed_rfc` (via the
`nightly seed-rfc` CLI command) when an operator's seed describes a
feature or multi-step initiative. The function:

1. Computes the next RFC number by scanning `.planning/rfcs/` for the
   highest existing `NNN-*.md` filename.
2. Renders a minimal `accepted`-status frontmatter with `author:
   nightly-seed` so retro audits can distinguish agent-stubbed RFCs
   from hand-authored ones.
3. Writes a section-by-section body skeleton (Status / Context /
   Non-goals / Proposed direction / Resolved technical decisions /
   Risks / Implementation phases / Sized checklist) with `_TODO_`
   placeholders the agent overwrites in its first Edit pass.

The cascade's `_find_accepted_rfc` walks `.planning/rfcs/` in
filename order and picks the first unchecked `- [ ]` item; a freshly
stubbed RFC carries one placeholder unchecked item so the cascade
has something to dispatch against until the agent fills in the real
checklist.

Frontmatter is rendered through `nightly_core.plans.render_frontmatter`
so the cascade reader (`parse_frontmatter`) sees the exact shape it
expects from hand-authored RFCs. The numbering scheme matches the
existing 001-004 RFCs verbatim — three-digit zero-padded prefix,
kebab-case slug, `.md` extension.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

from nightly_core.paths import planning_dir
from nightly_core.plans import render_frontmatter
from nightly_core.runs import slugify

__all__ = [
    "RFC_BODY_SKELETON",
    "RFC_FRONTMATTER_TEMPLATE",
    "SEED_SOURCES",
    "next_rfc_number",
    "write_seed_rfc",
]


SEED_SOURCES: tuple[str, ...] = (
    "interactive_seed",
    "interactive_context",
    "headless",
)
"""Valid values for the `--source` flag / `source` frontmatter field.

- `interactive_seed` — operator typed `/nightly <seed>` and the agent
  judged it feature-shape.
- `interactive_context` — operator typed bare `/nightly` and the agent
  distilled the prior conversation into a title.
- `headless` — programmatic caller (future `nightly run` integration,
  triage-driven RFC seeder, etc.). Reserved for now; no in-tree
  caller uses it yet.
"""


# Filename pattern: `NNN-<slug>.md` where NNN is the zero-padded RFC
# number. The regex tolerates 3+ digits so a future jump past 999 still
# parses (the cascade does not care about width).
_RFC_FILENAME_RE = re.compile(r"^(\d{3,})-")


RFC_FRONTMATTER_TEMPLATE: dict[str, str] = {
    "status": "accepted",
    "sized": "false",
    "title": "",
    "created": "",
    "accepted_on": "",
    "author": "nightly-seed",
    "source": "interactive_seed",
}
"""The frontmatter shape stamped onto a freshly stubbed seed-RFC.

Empty-string slots (`title`, `created`, `accepted_on`) are populated
by `write_seed_rfc`. `sized: false` flips manually once the agent
commits to the Sized checklist — the stub starts unsized because the
agent's first Edit pass typically refines the phases. `author:
nightly-seed` is the distinguishing field for retro filtering."""


RFC_BODY_SKELETON = """
# RFC {number:03d} — {title}

## Status

`accepted` — stubbed by `nightly seed-rfc` from an interactive
operator seed. The agent's first Edit pass fleshes out this Status
section with the agreed-upon direction, then expands the remaining
sections.

## Context

_TODO — describe the problem and why the current shape doesn't fit._

## Non-goals

_TODO — list what this RFC is **not** doing, to keep scope honest._

## Proposed direction

_TODO — name 2-3 approaches with pros/cons. The selected approach
ships as v1; the others are documented for the record._

## Resolved technical decisions

_TODO — numbered, terse, each with a one-line rationale._

## Risks

_TODO — what could go wrong and the mitigation._

## Implementation phases

_TODO — phases with hour estimates and merge gates._

## Sized checklist

- [ ] Stub: agent fleshes out this RFC's body in its first Edit pass
"""
"""Body skeleton with one placeholder unchecked checklist item.

The placeholder gives the cascade something to dispatch against
between stub-time and the agent's first Edit pass — if the cascade
walks `.planning/rfcs/` before the agent has expanded the checklist,
the placeholder routes the agent back to this RFC to fill it in.
The agent's first Edit pass replaces the placeholder with the real
Phase A/B/C items.

`{number}` and `{title}` are `str.format` placeholders rendered by
`write_seed_rfc`."""


def next_rfc_number(root: Path | None = None) -> int:
    """Return the next RFC number to use under `.planning/rfcs/`.

    Scans the directory for filenames matching `NNN-<slug>.md`,
    parses the leading NNN, returns `max(found) + 1`. Defaults to 1
    when the directory is absent or empty.

    Non-conforming filenames (no leading NNN, wrong extension) are
    ignored — the directory may carry README or template files that
    are not RFCs.
    """
    rfcs = planning_dir(root) / "rfcs"
    if not rfcs.is_dir():
        return 1
    highest = 0
    for entry in rfcs.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        match = _RFC_FILENAME_RE.match(entry.name)
        if match is None:
            continue
        try:
            highest = max(highest, int(match.group(1)))
        except ValueError:
            continue
    return highest + 1


def write_seed_rfc(
    root: Path | None = None,
    *,
    title: str,
    slug: str | None = None,
    source: str = "interactive_seed",
    today: date | None = None,
) -> Path:
    """Stub a new `accepted` RFC under `.planning/rfcs/` and return its path.

    `title` is the human-readable RFC title and the source for the
    auto-derived slug. `slug` overrides the derivation when set —
    useful when the operator wants a slug that diverges from the
    title (e.g. shorter, or pre-existing convention).

    `source` records the trigger that fired (`interactive_seed`,
    `interactive_context`, or `headless`). Unknown values are
    accepted verbatim — the CLI surface validates against
    `SEED_SOURCES`; programmatic callers may extend the set.

    `today` is exposed for tests so the rendered `created` /
    `accepted_on` dates are deterministic. Production calls let it
    default to UTC today.
    """
    if not title.strip():
        msg = "title must be non-empty"
        raise ValueError(msg)

    rfcs = planning_dir(root) / "rfcs"
    rfcs.mkdir(parents=True, exist_ok=True)

    chosen_slug = slugify(slug) if slug else slugify(title)
    if not chosen_slug:
        chosen_slug = "untitled"

    stamp = (today or datetime.now(UTC).date()).isoformat()
    metadata = dict(RFC_FRONTMATTER_TEMPLATE)
    metadata["title"] = title
    metadata["created"] = stamp
    metadata["accepted_on"] = stamp
    metadata["source"] = source

    number = next_rfc_number(root)
    filename = f"{number:03d}-{chosen_slug}.md"
    path = rfcs / filename
    body = RFC_BODY_SKELETON.format(number=number, title=title)
    # Atomic exclusive-create — `open("x")` raises FileExistsError if
    # the path already exists, so a racing writer (or an operator who
    # passed a slug whose `NNN-<slug>.md` is already on disk) surfaces
    # rather than silently clobbering. Per RFC 005 §Resolved-4 we
    # intentionally do not lock — two concurrent operators is a
    # pathological case outside the single-process contract.
    with path.open("x", encoding="utf-8") as f:
        f.write(render_frontmatter(metadata, body))
    return path
