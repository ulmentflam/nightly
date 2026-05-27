"""Loader for the Gemini CLI skill content shipped in this package.

Gemini CLI custom commands are TOML files, not markdown. We keep the
canonical Nightly skill content as `skill.md` (matching every other
host package) and convert it to TOML at install time via
`md_to_gemini_toml`. Companion skills (`/nightly-conclude`,
`/nightly-update`, `/nightly-bug`, `/nightly-init`) get the same
treatment — `nightly_core.conclude_skill` ships them as markdown
strings; we re-shape each into TOML before writing.
"""

from __future__ import annotations

from importlib.resources import files

__all__ = ["SKILL_MD", "load_skill_md", "md_to_gemini_toml"]


def load_skill_md() -> str:
    """Return the packaged SKILL.md as a string."""
    return files("nightly_host_gemini").joinpath("skill.md").read_text(encoding="utf-8")


SKILL_MD: str = load_skill_md()
"""The Gemini CLI skill markdown — converted to TOML by `GeminiHostIntegration.install`."""


def md_to_gemini_toml(md_text: str) -> str:
    """Convert a Nightly skill markdown blob into Gemini CLI's TOML schema.

    Gemini CLI custom commands expect two fields:

        description = "..."
        prompt = \"\"\"...\"\"\"

    We pull `description:` out of the YAML frontmatter (if present) and
    feed the rest of the document into the triple-quoted `prompt` field.
    The skill markdown shipped by Nightly never contains `\"\"\"`, so a
    naive multi-line string literal is safe — we assert that invariant
    so a future edit doesn't silently break the TOML emit.
    """
    description, body = _split_frontmatter(md_text)
    if '"""' in body:
        msg = (
            "Skill body contains a triple-quoted string literal — Gemini CLI "
            "TOML emit would break. Edit the skill markdown to avoid `\"\"\"`."
        )
        raise ValueError(msg)
    escaped_desc = description.replace("\\", "\\\\").replace('"', '\\"')
    # Triple-quoted TOML strings preserve content verbatim except for `"""`
    # (asserted absent above) and a trailing backslash (none of our skills
    # end with one). A leading newline after `"""` is stripped by TOML.
    return f'description = "{escaped_desc}"\nprompt = """\n{body}"""\n'


def _split_frontmatter(md_text: str) -> tuple[str, str]:
    """Return `(description, body)`. Description defaults to empty string."""
    lines = md_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", md_text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return "", md_text
    description = ""
    for fl in lines[1:end_idx]:
        stripped = fl.strip()
        if stripped.startswith("description:"):
            description = stripped.split(":", 1)[1].strip()
            break
    # Drop the leading blank that usually follows the closing `---`.
    body_lines = lines[end_idx + 1 :]
    while body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    return description, "".join(body_lines)
