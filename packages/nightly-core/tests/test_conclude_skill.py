"""The four companion skill files, which nothing tested until now.

`nightly_core.conclude_skill` holds the markdown for `/nightly-conclude`,
`/nightly-update`, `/nightly-init`, and `/nightly-bug`. Every host
install writes these verbatim, so a malformed one breaks skill loading on
all seven harnesses at once, and a subtly edited one changes what the
agent believes it is allowed to do.

Two of them are **human-only off-ramps**. The rules block records that an
agent has already self-concluded in production — running `nightly
conclude` on its own initiative, freezing the cascade and ending a
session with unblocked work still on disk. The only thing standing
between that failure and a repeat is the warning text inside these
constants. Nothing asserted it was still there.
"""

from __future__ import annotations

import pytest

from nightly_core.conclude_skill import (
    BUG_SKILL_MD,
    CONCLUDE_SKILL_MD,
    INIT_SKILL_MD,
    UPDATE_SKILL_MD,
)

ALL_SKILLS = {
    "nightly-conclude": CONCLUDE_SKILL_MD,
    "nightly-update": UPDATE_SKILL_MD,
    "nightly-init": INIT_SKILL_MD,
    "nightly-bug": BUG_SKILL_MD,
}

# The off-ramps an agent must never invoke on its own initiative.
HUMAN_ONLY = ("nightly-conclude", "nightly-bug")


def _frontmatter(text: str) -> dict[str, str]:
    """Parse the leading `---` fenced block into a flat dict."""
    assert text.startswith("---\n"), "skill must open with a frontmatter fence"
    end = text.index("\n---\n", 3)
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


@pytest.mark.parametrize("name", sorted(ALL_SKILLS))
def test_skill_has_parseable_frontmatter(name: str) -> None:
    """A malformed fence breaks skill loading on every host at once."""
    meta = _frontmatter(ALL_SKILLS[name])
    assert meta.get("name"), f"{name} has no `name:`"
    assert meta.get("description"), f"{name} has no `description:`"


@pytest.mark.parametrize("name", sorted(ALL_SKILLS))
def test_frontmatter_name_matches_the_invocable_command(name: str) -> None:
    """The `name:` is what the operator types as `/<name>`. A mismatch
    installs a skill nobody can call."""
    assert _frontmatter(ALL_SKILLS[name])["name"] == name


@pytest.mark.parametrize("name", sorted(ALL_SKILLS))
def test_skill_body_is_more_than_frontmatter(name: str) -> None:
    text = ALL_SKILLS[name]
    body = text[text.index("\n---\n", 3) + 5 :]
    assert body.strip(), f"{name} has an empty body"


@pytest.mark.parametrize("name", HUMAN_ONLY)
def test_human_only_skills_forbid_self_invocation(name: str) -> None:
    """This is the regression guard that matters.

    An agent has already self-concluded in production. The warning lives
    only in this text; if an edit drops it, nothing else in the system
    objects and the failure recurs silently.
    """
    text = ALL_SKILLS[name]
    lowered = text.lower()
    assert "human" in lowered
    assert "never" in lowered
    # The prohibition must be visible in the *description*, not buried in
    # the body — the description is what a host surfaces in its skill
    # listing, and often all the agent reads before deciding to invoke.
    description = _frontmatter(text)["description"].lower()
    assert "never" in description or "human-only" in description


@pytest.mark.parametrize("name", HUMAN_ONLY)
def test_human_only_skills_name_the_correct_wrap_up(name: str) -> None:
    """Saying "don't do this" without saying what to do instead is how an
    agent talks itself back into doing it."""
    lowered = ALL_SKILLS[name].lower()
    assert "ideate" in lowered or "brief" in lowered


def test_conclude_skill_explains_the_consequence() -> None:
    """The rules block records the exact production failure; the skill
    should carry the same reasoning so it survives independently."""
    lowered = CONCLUDE_SKILL_MD.lower()
    assert "cascade" in lowered
    assert "concluded" in lowered


def test_bug_skill_says_the_agent_must_not_self_file() -> None:
    """Self-filing masks whatever the agent was about to do wrong."""
    lowered = BUG_SKILL_MD.lower()
    assert "nightly bug" in lowered
    assert "operator" in lowered or "human" in lowered


@pytest.mark.parametrize("name", sorted(ALL_SKILLS))
def test_skills_do_not_end_mid_sentence(name: str) -> None:
    """A truncated constant is the failure mode a string literal invites,
    and it reads as valid markdown right up to the cut."""
    assert ALL_SKILLS[name].rstrip().endswith((".", "`", ")", "]", ":", "—", "!"))


def test_every_exported_skill_is_covered_here() -> None:
    """A fifth companion skill added without a row would go untested the
    same way all four were until now."""
    from nightly_core import conclude_skill

    exported = {n for n in conclude_skill.__all__ if n.endswith("_SKILL_MD")}
    covered = {
        "CONCLUDE_SKILL_MD",
        "UPDATE_SKILL_MD",
        "INIT_SKILL_MD",
        "BUG_SKILL_MD",
    }
    assert exported == covered, f"untested companion skills: {exported - covered}"
