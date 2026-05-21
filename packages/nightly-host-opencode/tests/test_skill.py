"""Sanity checks on the SKILL.md content shipped to opencode."""

from __future__ import annotations

from nightly_host_opencode.skill import SKILL_MD, load_skill_md

_MIN_SKILL_MD_BYTES = 1000


def test_skill_md_loads() -> None:
    assert load_skill_md() == SKILL_MD
    assert len(SKILL_MD) > _MIN_SKILL_MD_BYTES


def test_skill_md_has_yaml_frontmatter() -> None:
    assert SKILL_MD.startswith("---\n")
    head = SKILL_MD.splitlines()[:30]
    assert "---" in head[1:]
    assert any(line.startswith("name: nightly") for line in head)
    assert any(line.startswith("description:") for line in head)


def test_skill_md_mentions_opencode_specifics() -> None:
    # opencode-flavored dispatch (session forking) + SSE event stream
    assert "POST /session/:id/fork" in SKILL_MD
    assert "/global/event" in SKILL_MD
    assert "SSE" in SKILL_MD


def test_skill_md_acknowledges_lack_of_os_sandbox() -> None:
    # opencode has no Seatbelt/Landlock equivalent; the skill should be
    # explicit about that so the agent doesn't assume host enforcement.
    body = SKILL_MD.lower()
    assert "no os" in body or "no equivalent" in body or "no os sandbox" in body


def test_skill_md_documents_priority_cascade() -> None:
    """Phase 5: six cascade sources, including `ideate`."""
    for source in (
        "resume_in_flight",
        "unblocked_approval",
        "accepted_rfc",
        "github_issue",
        "ideate",
        "nothing",
    ):
        assert source in SKILL_MD


def test_skill_md_documents_proposer_suite() -> None:
    """Phase 5: the skill should reference ideate and the autonomy bar."""
    assert "nightly ideate" in SKILL_MD
    assert "nightly propose" in SKILL_MD
    assert "autonomy bar" in SKILL_MD.lower()


def test_skill_md_documents_refusal_categories() -> None:
    for category in (
        "Destructive git",
        "Production state",
        "External communication",
        "Network egress",
        "Scope creep",
        "Bypassing test",
    ):
        assert category in SKILL_MD


def test_skill_md_documents_narrative_slots() -> None:
    for slot in ("briefing.md", "notes.md", "lessons.md"):
        assert slot in SKILL_MD


def test_skill_md_documents_conclude_protocol() -> None:
    # The "Never abandon mid-task" phrase straddles a line break in the
    # paragraph wrap; check both tokens rather than the contiguous phrase.
    assert "Never SIGKILL" in SKILL_MD
    assert "Never abandon" in SKILL_MD
    assert "mid-task" in SKILL_MD
