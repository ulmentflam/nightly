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
    # New core directive: "if you can recommend, execute."
    assert "recommend" in body
    assert "execute" in body
    assert "uncertainty.md" in body
    assert "refusal policy" in body.lower() or "refusal-policy" in body.lower()


def test_rules_body_narrows_uncertainty_md_to_refusal_only() -> None:
    """The new contract: uncertainty.md is for refusal-policy gaps only."""
    body = NIGHTLY_RULES_BODY
    # The phrase that flags the new narrowed scope must appear.
    assert "refusal-policy gaps" in body or "refusal-policy" in body.lower()
    # And we must not still be telling agents to log every assumption.
    assert "record your assumption" not in body
    assert "Record uncertainty" not in body


def test_seed_rules_appends_blank_line_separator(tmp_path: Path) -> None:
    """Existing content ending with a newline should get one blank line of
    space before the appended block, not jam directly against it."""
    (tmp_path / "AGENTS.md").write_text("# top\n\ncontent\n", encoding="utf-8")
    seed_rules(tmp_path, files=("AGENTS.md",))
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # The block follows after a blank line, not glued onto the last char.
    pre = text[: text.index(MARKER_START)]
    assert pre.endswith("\n\n"), f"expected blank-line gap, got tail: {pre[-5:]!r}"


# ── Phase 9n: anti-self-conclude rule ─────────────────────────────────────


def test_rules_body_forbids_self_conclude() -> None:
    """Rule 10: the agent must never invoke `nightly conclude` itself.

    Regression guard for the 2026-05 corpus-forge incident where the
    Nightly agent ran `nightly conclude` after `nightly brief` on its
    own initiative, freezing the cascade short-circuit at `concluded`
    and ending the session with unblocked RFC items on disk.
    """
    body = NIGHTLY_RULES_BODY
    # The new rule must mention the off-ramp commands by name.
    assert "Never invoke the human shutdown off-ramps" in body
    for command in ("nightly conclude", "nightly stop", "nightly bug"):
        assert command in body, f"rule should reference {command}"
    # And it must call out the agent's correct end-of-cascade flow.
    assert "nightly ideate" in body
    assert "nightly brief" in body
    # Past failure citation — keeps future edits from softening the rule
    # without realising why it exists.
    assert (
        "self-conclude" in body.lower()
        or "self-invoke" in body.lower()
        or ("freeze" in body.lower() and "concluded" in body)
    )


def test_rules_body_marks_off_ramps_as_human_only() -> None:
    """The `Human shutdown intervention` section must say the agent
    doesn't run those commands itself — otherwise a re-read of just the
    off-ramp list could re-suggest the wrong behavior."""
    body = NIGHTLY_RULES_BODY
    assert "Human shutdown intervention" in body
    # The clarifying line that distinguishes operator-from-agent.
    assert "human controls" in body.lower() or "operator control" in body.lower()


def test_rules_body_documents_nightly_bug_off_ramp() -> None:
    """The new `nightly bug` debugging tool needs to be visible in the
    rules block so the operator knows about it and the agent knows it's
    out of bounds."""
    body = NIGHTLY_RULES_BODY
    assert "nightly bug" in body
    assert "Filing a bug" in body or "file a bug" in body.lower()


# ── v0.0.3: Rule 11 reframed from "host caps" to "consolidate, never stop" ──


def test_rules_body_documents_pr_consolidation_directive() -> None:
    """Rule 11 (v0.0.3+): the orchestrator never stops because of PR count.

    Regression guard for the 2026-06-05 directive that ripped out the
    `MAX_OPEN_PRS=5` Stop-hook cap: the cap was producing mid-session
    stops with unblocked RFC work still on disk. Rule 11 now mandates
    consolidation (pr_rescue → extend existing PR → bundle adjacent
    RFC phases) instead of gating on count. The cap reference should be
    gone from the agent-facing contract.
    """
    body = NIGHTLY_RULES_BODY
    # Positive: rule 11 names the new directive.
    assert "Minimize PR count" in body or "consolidating" in body
    assert "never stop because of it" in body or "monotonic forward progress" in body.lower()
    # Positive: the rule names the preference order.
    assert "pr_rescue" in body
    assert "Extend the most recently-opened" in body
    # Negative: the cap-based off-ramp must NOT appear as an active rule.
    # The historical mention may still appear inside a "previous cap was
    # removed in v0.0.3" sentence; we just want to be sure it's not the
    # current behavior. Check that the v0.0.3 removal note is present.
    assert "v0.0.3" in body
    assert "MAX_OPEN_PRS" in body  # mentioned as the removed constant
