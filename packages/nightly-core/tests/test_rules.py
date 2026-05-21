"""Tests for nightly_core.rules."""

from __future__ import annotations

from pathlib import Path

from nightly_core.rules import (
    MARKER_END,
    MARKER_START,
    NIGHTLY_RULES_BODY,
    seed_rules,
)


def test_seed_rules_creates_file_when_absent(tmp_path: Path) -> None:
    results = seed_rules(tmp_path, files=("AGENTS.md",))
    assert len(results) == 1
    result = results[0]
    assert result.action == "created"
    assert result.changed is True
    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert MARKER_START in content
    assert MARKER_END in content
    assert "Never prompt the user" in content


def test_seed_rules_appends_when_file_exists_without_marker(tmp_path: Path) -> None:
    existing = "# My project\n\nSome custom rules here.\n"
    (tmp_path / "AGENTS.md").write_text(existing, encoding="utf-8")

    results = seed_rules(tmp_path, files=("AGENTS.md",))
    assert results[0].action == "updated"
    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # User's content preserved at the top
    assert content.startswith("# My project")
    assert "Some custom rules here." in content
    # Block appended below
    assert MARKER_START in content
    assert content.index("Some custom rules here") < content.index(MARKER_START)


def test_seed_rules_replaces_block_in_place(tmp_path: Path) -> None:
    """Existing marker-delimited block should be replaced atomically."""
    initial = (
        "# Header\n\nIntro.\n\n"
        f"{MARKER_START}\n"
        "## Stale Nightly rules\n\nThis is an out-of-date version.\n"
        f"{MARKER_END}\n\n"
        "# Footer\nKeep me.\n"
    )
    (tmp_path / "AGENTS.md").write_text(initial, encoding="utf-8")

    results = seed_rules(tmp_path, files=("AGENTS.md",))
    assert results[0].action == "updated"
    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # Header + footer preserved
    assert "# Header" in content
    assert "Intro." in content
    assert "# Footer" in content
    assert "Keep me." in content
    # Stale block gone, fresh block present
    assert "out-of-date" not in content
    assert "Never prompt the user" in content


def test_seed_rules_unchanged_when_block_matches(tmp_path: Path) -> None:
    """Calling seed twice in a row → second call is `unchanged`."""
    seed_rules(tmp_path, files=("AGENTS.md",))
    results = seed_rules(tmp_path, files=("AGENTS.md",))
    assert results[0].action == "unchanged"
    assert results[0].changed is False


def test_seed_rules_handles_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    # CLAUDE.md doesn't exist
    results = seed_rules(tmp_path, files=("AGENTS.md", "CLAUDE.md"))
    assert len(results) == 2
    actions = {r.path.name: r.action for r in results}
    assert actions["AGENTS.md"] == "updated"  # appended
    assert actions["CLAUDE.md"] == "created"


def test_seed_rules_skips_absent_files_when_opted_out(tmp_path: Path) -> None:
    results = seed_rules(
        tmp_path,
        files=("CLAUDE.md",),
        create_if_absent=False,
    )
    assert results[0].action == "skipped"
    assert not (tmp_path / "CLAUDE.md").exists()


def test_rules_body_mentions_the_load_bearing_constraints() -> None:
    """Belt-and-suspenders: the body must mention the actual rules."""
    body = NIGHTLY_RULES_BODY
    assert "Never prompt" in body
    assert "Never stop" in body
    assert "Always pick" in body
    assert "uncertainty.md" in body
    assert "refusal policy" in body.lower() or "refusal-policy" in body.lower()


def test_seed_rules_appends_blank_line_separator(tmp_path: Path) -> None:
    """Existing content ending with a newline should get one blank line of
    space before the appended block, not jam directly against it."""
    (tmp_path / "AGENTS.md").write_text("# top\n\ncontent\n", encoding="utf-8")
    seed_rules(tmp_path, files=("AGENTS.md",))
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # The block follows after a blank line, not glued onto the last char.
    pre = text[: text.index(MARKER_START)]
    assert pre.endswith("\n\n"), f"expected blank-line gap, got tail: {pre[-5:]!r}"
