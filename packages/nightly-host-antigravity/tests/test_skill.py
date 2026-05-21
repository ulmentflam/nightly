"""Sanity checks on the SKILL.md content shipped to Antigravity."""

from __future__ import annotations

from nightly_host_antigravity.skill import SKILL_MD, load_skill_md

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


def test_skill_md_mentions_antigravity_specifics() -> None:
    # Agent Manager + brain/<GUID>/ mirroring + Gemini API
    assert "Agent Manager" in SKILL_MD
    assert "brain/<GUID>/" in SKILL_MD or "brain/" in SKILL_MD
    assert ".gemini/antigravity" in SKILL_MD


def test_skill_md_acknowledges_no_os_sandbox() -> None:
    body = SKILL_MD.lower()
    assert "no os" in body or "no equivalent" in body


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
    assert "Never SIGKILL" in SKILL_MD
    assert "Never abandon" in SKILL_MD
    assert "mid-task" in SKILL_MD
