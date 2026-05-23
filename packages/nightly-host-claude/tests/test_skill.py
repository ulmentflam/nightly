"""Sanity checks on the SKILL.md content shipped to Claude Code."""

from __future__ import annotations

from nightly_host_claude.skill import SKILL_MD, load_skill_md

# Minimum sane size for the Phase 1 skill — anything shorter than this is
# almost certainly a truncated file rather than a legitimate change.
_MIN_SKILL_MD_BYTES = 1000


def test_skill_md_loads() -> None:
    assert load_skill_md() == SKILL_MD
    assert len(SKILL_MD) > _MIN_SKILL_MD_BYTES


def test_skill_md_has_yaml_frontmatter() -> None:
    assert SKILL_MD.startswith("---\n")
    # closing fence on its own line within the first 30 lines
    head = SKILL_MD.splitlines()[:30]
    assert "---" in head[1:]
    assert any(line.startswith("name: nightly") for line in head)
    assert any(line.startswith("description:") for line in head)


def test_skill_md_documents_refusal_policy() -> None:
    assert "Refusal policy" in SKILL_MD
    for category in (
        "Destructive git",
        "Production state",
        "External communication",
        "Network egress",
        "Scope creep",
        "Bypassing test",
    ):
        assert category in SKILL_MD, f"missing refusal category in skill: {category}"


def test_skill_md_documents_conclude_protocol() -> None:
    assert "Conclude" in SKILL_MD
    assert "Never SIGKILL" in SKILL_MD
    assert "Never abandon mid-task" in SKILL_MD


def test_skill_md_points_at_on_disk_state() -> None:
    for path in (
        ".nightly/config.yml",
        ".nightly/runs/CURRENT",
        ".planning/",
        "AGENTS.md",
        "CLAUDE.md",
    ):
        assert path in SKILL_MD, f"skill should reference {path}"


def test_skill_md_marks_current_phase_boundaries() -> None:
    # Skill should always declare its current phase and what's still pending.
    assert "Phase" in SKILL_MD
    assert "Not yet" in SKILL_MD


def test_skill_md_references_phase_2_specialists() -> None:
    for role in ("implementer", "tester", "reviewer", "researcher"):
        assert role in SKILL_MD, f"skill should reference specialist role {role}"
    assert "Task tool" in SKILL_MD
    assert "nightly specialist" in SKILL_MD


def test_skill_md_references_phase_2_cli_commands() -> None:
    for command in (
        "nightly start",
        "nightly conclude",
        "nightly brief",
        "nightly status",
    ):
        assert command in SKILL_MD, f"skill should reference {command}"


def test_skill_md_documents_narrative_slots() -> None:
    """The skill should instruct the agent to write the three narrative
    slots (briefing.md, per-task notes.md, lessons.md) before drain."""
    for slot in ("briefing.md", "notes.md", "lessons.md"):
        assert slot in SKILL_MD, f"skill should reference {slot}"
    # And the why — narrative before drain, raw HTML escaped
    assert "narrative" in SKILL_MD.lower()
    assert "compacted" in SKILL_MD.lower() or "compaction" in SKILL_MD.lower()


def test_skill_md_documents_priority_cascade() -> None:
    """Phase 5: the skill should reference all six cascade sources."""
    for source in (
        "resume_in_flight",
        "unblocked_approval",
        "accepted_rfc",
        "github_issue",
        "ideate",
        "nothing",
    ):
        assert source in SKILL_MD, f"skill should reference cascade source {source}"
    assert "nightly next" in SKILL_MD
    assert "cascade" in SKILL_MD.lower()


def test_skill_md_documents_proposer_suite() -> None:
    """Phase 5: the skill should reference ideate + the autonomy bar."""
    assert "nightly ideate" in SKILL_MD
    assert "nightly propose" in SKILL_MD
    assert "autonomy bar" in SKILL_MD.lower()
    assert "proposer" in SKILL_MD.lower()


def test_skill_md_documents_plan_status_lifecycle() -> None:
    """Phase 3: the skill should reference status transitions."""
    for status in ("ready", "in_progress", "done", "blocked: approval", "parked"):
        assert status in SKILL_MD, f"skill should reference status {status!r}"


# ── Phase 9n: anti-self-conclude guard ────────────────────────────────────


def test_skill_md_forbids_self_conclude() -> None:
    """Skill must mirror rules.py rule 10: the agent never runs
    `nightly conclude`, `nightly stop`, or `nightly bug` itself."""
    text = SKILL_MD
    assert "never invoke" in text.lower() or "Never invoke" in text
    # Both the off-ramp triplet and the correct end-of-cascade pattern
    # must be explicit.
    for cmd in ("nightly conclude", "nightly ideate", "nightly brief"):
        assert cmd in text, f"skill should reference {cmd}"
    # The "Conclude — human-only" section header (or equivalent phrasing)
    # gives a re-read of just the conclude section a clear stop sign.
    assert "human-only" in text.lower() or "human only" in text.lower()


def test_skill_md_off_ramp_list_is_marked_human_only() -> None:
    """The off-ramp bullets near session start must remind the agent
    those are operator controls — not part of the agent's wrap-up flow."""
    text = SKILL_MD
    # The phrase that disambiguates operator-vs-agent intent.
    assert "operator controls" in text.lower() or "never invoke them yourself" in text.lower()
