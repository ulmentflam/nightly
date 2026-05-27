"""Sanity checks on the SKILL.md content shipped to Gemini CLI."""

from __future__ import annotations

from nightly_host_gemini.skill import SKILL_MD, load_skill_md, md_to_gemini_toml

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


def test_skill_md_mentions_gemini_cli_specifics() -> None:
    # Vanilla Gemini CLI — custom commands at `.gemini/commands/*.toml`.
    assert "Gemini CLI" in SKILL_MD
    assert ".gemini/commands" in SKILL_MD
    # Hook surface is AfterAgent (shared with Antigravity).
    assert "AfterAgent" in SKILL_MD


def test_skill_md_documents_priority_cascade() -> None:
    for source in (
        "resume_in_flight",
        "unblocked_approval",
        "accepted_rfc",
        "github_issue",
        "pr_rescue",
        "ideate",
        "nothing",
    ):
        assert source in SKILL_MD


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


# ── md_to_gemini_toml ────────────────────────────────────────────────────


def test_md_to_gemini_toml_extracts_description_from_frontmatter() -> None:
    md = '---\nname: example\ndescription: Short description with "quotes".\n---\n\nBody line 1\nBody line 2\n'
    toml_out = md_to_gemini_toml(md)
    assert toml_out.startswith('description = "Short description with \\"quotes\\"."\n')
    assert 'prompt = """' in toml_out
    assert "Body line 1" in toml_out
    assert "Body line 2" in toml_out


def test_md_to_gemini_toml_handles_missing_frontmatter() -> None:
    md = "no frontmatter here\njust a body"
    toml_out = md_to_gemini_toml(md)
    assert toml_out.startswith('description = ""\n')
    assert "no frontmatter here" in toml_out


def test_md_to_gemini_toml_preserves_skill_body() -> None:
    """The packaged SKILL_MD round-trips into TOML without losing content."""
    toml_out = md_to_gemini_toml(SKILL_MD)
    # The toolkit and refusal sections must survive the transform.
    assert "Toolkit" in toml_out
    assert "Refusal policy" in toml_out


def test_md_to_gemini_toml_rejects_embedded_triple_quotes() -> None:
    import pytest

    with pytest.raises(ValueError, match="triple-quoted"):
        md_to_gemini_toml('---\ndescription: x\n---\n\nbody with """ embedded')
