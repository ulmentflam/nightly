"""Tests for nightly_core.seed_rfc (RFC 005 Phase A)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core.cascade import pick_accepted_rfc
from nightly_core.cli import app
from nightly_core.plans import parse_frontmatter
from nightly_core.seed_rfc import (
    RFC_BODY_SKELETON,
    RFC_FRONTMATTER_TEMPLATE,
    SEED_SOURCES,
    next_rfc_number,
    write_seed_rfc,
)

# ── next_rfc_number ───────────────────────────────────────────────────────


def test_next_rfc_number_empty_dir_returns_one(tmp_path: Path) -> None:
    """Empty `.planning/rfcs/` is the first-ever-RFC case; default to 1."""
    assert next_rfc_number(tmp_path) == 1


def test_next_rfc_number_missing_dir_returns_one(tmp_path: Path) -> None:
    """No `.planning/` at all — still 1, no exception."""
    assert next_rfc_number(tmp_path) == 1


def test_next_rfc_number_scans_existing_rfcs(tmp_path: Path) -> None:
    """With 001 + 002 + 004 present, next is 005 (max + 1, not count + 1)."""
    rfcs = tmp_path / ".planning" / "rfcs"
    rfcs.mkdir(parents=True)
    for name in ("001-alpha.md", "002-beta.md", "004-delta.md"):
        (rfcs / name).write_text("---\nstatus: accepted\n---\n# body\n", encoding="utf-8")
    assert next_rfc_number(tmp_path) == 5


def test_next_rfc_number_ignores_non_rfc_files(tmp_path: Path) -> None:
    """README.md, templates, hand-named drafts without NNN prefix — skipped."""
    rfcs = tmp_path / ".planning" / "rfcs"
    rfcs.mkdir(parents=True)
    (rfcs / "README.md").write_text("# rfcs index\n", encoding="utf-8")
    (rfcs / "draft-notes.md").write_text("# draft\n", encoding="utf-8")
    (rfcs / "001-real.md").write_text("---\nstatus: accepted\n---\n# x\n", encoding="utf-8")
    (rfcs / "002.txt").write_text("wrong extension\n", encoding="utf-8")
    assert next_rfc_number(tmp_path) == 2


def test_next_rfc_number_tolerates_wider_numbering(tmp_path: Path) -> None:
    """4-digit numbering past 999 still parses (future-proofing)."""
    rfcs = tmp_path / ".planning" / "rfcs"
    rfcs.mkdir(parents=True)
    (rfcs / "0999-old.md").write_text("---\nstatus: accepted\n---\n# x\n", encoding="utf-8")
    (rfcs / "1000-wide.md").write_text("---\nstatus: accepted\n---\n# x\n", encoding="utf-8")
    assert next_rfc_number(tmp_path) == 1001


# ── write_seed_rfc ────────────────────────────────────────────────────────


def test_write_seed_rfc_creates_file_with_correct_numbering(tmp_path: Path) -> None:
    """First RFC → 001-<slug>.md."""
    path = write_seed_rfc(tmp_path, title="Add a dashboard for vault stats")
    assert path.exists()
    assert path.name == "001-add-a-dashboard-for-vault-stats.md"
    assert path.parent == tmp_path / ".planning" / "rfcs"


def test_write_seed_rfc_numbers_after_existing(tmp_path: Path) -> None:
    """With 001-004 present, seed-rfc lands at 005."""
    rfcs = tmp_path / ".planning" / "rfcs"
    rfcs.mkdir(parents=True)
    for i in range(1, 5):
        (rfcs / f"{i:03d}-old.md").write_text("---\nstatus: accepted\n---\n# x\n", encoding="utf-8")
    path = write_seed_rfc(tmp_path, title="New feature seed")
    assert path.name == "005-new-feature-seed.md"


def test_write_seed_rfc_honors_explicit_slug(tmp_path: Path) -> None:
    """`--slug` overrides the title-derived slug."""
    path = write_seed_rfc(
        tmp_path,
        title="A very long title that would slugify to something unwieldy",
        slug="short-slug",
    )
    assert path.name == "001-short-slug.md"


def test_write_seed_rfc_frontmatter_round_trips(tmp_path: Path) -> None:
    """Rendered frontmatter parses cleanly through parse_frontmatter."""
    today = date(2026, 6, 4)
    path = write_seed_rfc(tmp_path, title="Test title", today=today)
    text = path.read_text(encoding="utf-8")
    metadata, _ = parse_frontmatter(text)
    assert metadata["status"] == "accepted"
    assert metadata["sized"] == "false"
    assert metadata["title"] == "Test title"
    assert metadata["created"] == "2026-06-04"
    assert metadata["accepted_on"] == "2026-06-04"
    assert metadata["author"] == "nightly-seed"
    assert metadata["source"] == "interactive_seed"


def test_write_seed_rfc_default_source_is_interactive_seed(tmp_path: Path) -> None:
    """Omitting --source records the common case verbatim."""
    path = write_seed_rfc(tmp_path, title="Anything")
    metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert metadata["source"] == "interactive_seed"


def test_write_seed_rfc_records_explicit_source(tmp_path: Path) -> None:
    """All three documented sources round-trip into the frontmatter."""
    for src in SEED_SOURCES:
        rfc_dir = tmp_path / src
        path = write_seed_rfc(rfc_dir, title=f"Title for {src}", source=src)
        metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert metadata["source"] == src


def test_write_seed_rfc_body_skeleton_renders_all_sections(tmp_path: Path) -> None:
    """Eight section headings from the RFC 001-004 convention all present."""
    path = write_seed_rfc(tmp_path, title="Section test")
    text = path.read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    for heading in (
        "# RFC 001 — Section test",
        "## Status",
        "## Context",
        "## Non-goals",
        "## Proposed direction",
        "## Resolved technical decisions",
        "## Risks",
        "## Implementation phases",
        "## Sized checklist",
    ):
        assert heading in body, f"missing heading: {heading}"


def test_write_seed_rfc_body_has_unchecked_placeholder(tmp_path: Path) -> None:
    """The stub carries one unchecked item so the cascade can route to it
    before the agent's first Edit pass expands the checklist."""
    path = write_seed_rfc(tmp_path, title="Placeholder check")
    _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert "- [ ]" in body


def test_write_seed_rfc_rejects_empty_title(tmp_path: Path) -> None:
    """A whitespace-only title is operator error — surface, don't fall back."""
    with pytest.raises(ValueError, match="non-empty"):
        write_seed_rfc(tmp_path, title="   ")


def test_write_seed_rfc_refuses_to_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filename collision raises rather than clobbering an existing RFC.

    The guard is defensive — in single-threaded usage `next_rfc_number`
    keeps the path unique. We simulate the race by pinning the
    next-number to a value that already exists on disk.
    """
    rfcs = tmp_path / ".planning" / "rfcs"
    rfcs.mkdir(parents=True)
    (rfcs / "001-already-there.md").write_text(
        "---\nstatus: accepted\n---\n# x\n", encoding="utf-8"
    )
    monkeypatch.setattr("nightly_core.seed_rfc.next_rfc_number", lambda _root=None: 1)
    with pytest.raises(FileExistsError):
        write_seed_rfc(tmp_path, title="Will clash", slug="already-there")


# ── cascade interop ───────────────────────────────────────────────────────


def test_seed_rfc_is_discoverable_by_cascade(tmp_path: Path) -> None:
    """`_find_accepted_rfc` walks `.planning/rfcs/` and the stub appears.

    The stub carries one unchecked placeholder item, so the cascade
    returns it as the first match — the agent then reads the RFC and
    overwrites the placeholder with real Phase items.
    """
    path = write_seed_rfc(tmp_path, title="Cascade integration test")
    match = pick_accepted_rfc(tmp_path)
    assert match is not None
    assert match.rfc_path == path
    assert match.item_text.startswith("Stub:")


def test_seed_rfc_does_not_outrank_older_rfc_with_unchecked_items(
    tmp_path: Path,
) -> None:
    """RFC 005 §Resolved-6: new seed-RFC lands at the standard slot — older
    RFCs with remaining unchecked items continue to outrank it.

    The cascade walks `sorted(rfcs.iterdir())`, which for NNN-prefixed
    filenames is numeric. So a freshly written 002-... must not be picked
    before 001-...'s own unchecked item.
    """
    rfcs = tmp_path / ".planning" / "rfcs"
    rfcs.mkdir(parents=True)
    (rfcs / "001-older.md").write_text(
        "---\nstatus: accepted\n---\n\n## Sized checklist\n\n- [ ] older unchecked item\n",
        encoding="utf-8",
    )
    write_seed_rfc(tmp_path, title="Newer seed")
    match = pick_accepted_rfc(tmp_path)
    assert match is not None
    assert match.rfc_path.name == "001-older.md"
    assert match.item_text == "older unchecked item"


# ── CLI surface ───────────────────────────────────────────────────────────


def test_cli_seed_rfc_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`nightly seed-rfc "<title>"` creates the file and prints the path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "nightly_core.seed_rfc.planning_dir", lambda _root=None: tmp_path / ".planning"
    )
    monkeypatch.setattr("nightly_core.cli.repo_root", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["seed-rfc", "Feature title"])
    assert result.exit_code == 0, result.output
    assert "✓ stubbed" in result.output
    rfcs = tmp_path / ".planning" / "rfcs"
    assert (rfcs / "001-feature-title.md").exists()


def test_cli_seed_rfc_rejects_invalid_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown `--source` exits non-zero with a clear message."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("nightly_core.cli.repo_root", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["seed-rfc", "anything", "--source", "nonsense"])
    assert result.exit_code != 0
    assert "unknown source" in result.output


def test_cli_seed_rfc_honors_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--slug` reaches the filename verbatim."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "nightly_core.seed_rfc.planning_dir", lambda _root=None: tmp_path / ".planning"
    )
    monkeypatch.setattr("nightly_core.cli.repo_root", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["seed-rfc", "A very long title", "--slug", "compact"],
    )
    assert result.exit_code == 0
    assert (tmp_path / ".planning" / "rfcs" / "001-compact.md").exists()


# ── module-level constants ────────────────────────────────────────────────


def test_frontmatter_template_carries_nightly_seed_author() -> None:
    """RFC 005 §Resolved-5 — author field distinguishes agent-stubbed RFCs."""
    assert RFC_FRONTMATTER_TEMPLATE["author"] == "nightly-seed"


def test_frontmatter_template_default_source_in_known_set() -> None:
    """The default `source` must be one of the documented SEED_SOURCES."""
    assert RFC_FRONTMATTER_TEMPLATE["source"] in SEED_SOURCES


def test_body_skeleton_carries_format_placeholders() -> None:
    """The skeleton must accept `number` and `title` formatting kwargs."""
    rendered = RFC_BODY_SKELETON.format(number=42, title="Sample")
    assert "# RFC 042 — Sample" in rendered
