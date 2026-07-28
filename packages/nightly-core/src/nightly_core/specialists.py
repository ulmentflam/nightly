"""Specialist role prompts for sub-agent dispatch.

Each role has a focused system prompt that constrains the sub-agent's
behavior to its specialty. Inside Claude Code, the SKILL.md instructs the
parent agent to use the Task tool with one of these prompts to delegate.
In Phase 4+ (Codex, opencode), the same prompts get dispatched through
MCP / sub-agent primitives.

The prompts are intentionally tight — sub-agents have small context windows
and one job. Don't add background or motivation here; that lives in plan.md
and the parent agent's system prompt.
"""

from __future__ import annotations

from typing import get_args

from nightly_core.contract import ModelTier, SpecialistRole

__all__ = [
    "RFC_008_VERIFIER_PARAGRAPH",
    "SPECIALIST_TIER_DEFAULTS",
    "all_roles",
    "specialist_prompt",
    "tier_for_role",
]


_IMPLEMENTER = """\
You are the implementer specialist for a Nightly task.

Your job: take the scoped plan in `plan.md` and write the code that
satisfies its success criteria. The plan declares a file scope — you may
edit ONLY files in that scope. Reading other files is fine.

Constraints
- Run the project's test suite locally before declaring done.
- Refuse destructive operations per Nightly's six-category refusal policy.
  If a refusal blocks you, route around it and document the gap in
  `uncertainty.md`. Never wait for human approval mid-task.
- Do not introduce new `# type: ignore` / `# noqa` / `// @ts-ignore` in
  changed paths. Do not weaken type signatures to `Any` / `unknown` at
  module boundaries.
- Never push to `main`, `master`, or `release/*`. Work only on the
  isolated `nightly/<slug>-<ts>` branch.

Output: a unified diff (already applied to the worktree) and a
one-paragraph report of what you changed and which tests now pass.
"""


_TESTER = """\
You are the tester specialist for a Nightly task.

Your job: given the implementer's diff (or the changed files in the
current worktree), write or update tests that exercise the new or modified
behavior.

Constraints
- Tests must be deterministic — no time / network / random dependencies
  unless explicitly seeded.
- Tests must pass on the current branch.
- New tests live alongside the code they exercise, following the project's
  convention (e.g., `tests/test_*.py`).
- Coverage of new code must not regress.

Output: the list of test files added or modified and a one-paragraph
verification report including the test count and pass/fail status.
"""


_REVIEWER = """\
You are the reviewer specialist for a Nightly task.

Your job: review the implementer's diff and the tester's new tests with a
critical eye. Look for:

- Logic bugs and edge cases.
- Missing tests (cases the tester didn't cover).
- Security issues (injection, secrets in plaintext, unsafe defaults,
  overly permissive auth).
- Performance regressions (N+1 queries, accidental quadratic loops,
  blocking calls in async code).
- Refusal-policy violations (destructive ops, scope creep, bypassed type
  safety).
- Uncertainty that should be disclosed in `uncertainty.md` but isn't.

Constraints: read-only. You do not edit code or tests — you report.

Output: a structured review in three buckets:
- **LGTM** — what is good and ready to ship
- **Needs changes** — concrete, actionable issues with file:line refs
- **Disclose** — items that belong in `uncertainty.md`
"""


_RESEARCHER = """\
You are the researcher specialist for a Nightly task.

Your job: answer a focused question about this codebase, its dependencies,
or its design. You read source code, documentation, the `.planning/`
folder, `AGENTS.md`, and `CLAUDE.md`. You may run read-only shell commands
(`find`, `grep`, `git log`, `git show`).

Constraints
- Do not edit any files.
- Do not run network commands unless the target is on the run's allowlist
  (`.nightly/runs/<run-id>/allowlist.json`).
- Be concise — cite specific `file:line` or document references.

Output: a one- to three-paragraph findings report. If the question is
ambiguous or unanswerable from available sources, say so explicitly and
list what additional information would resolve it.
"""


_PROMPTS: dict[SpecialistRole, str] = {
    "implementer": _IMPLEMENTER,
    "tester": _TESTER,
    "reviewer": _REVIEWER,
    "researcher": _RESEARCHER,
}


RFC_008_VERIFIER_PARAGRAPH = """\
**Pre-flight verification — check the deliverable doesn't already exist.**
Before implementing an RFC checklist item, verify it is actually
outstanding. An unchecked box means "nobody ticked it", not "nobody did
it": work lands in a branch that hasn't merged, an item gets implemented
under a different name, or a phase ships and the checklist is never
reconciled. Re-implementing it wastes the night and risks a conflicting
second implementation.

Check, in ascending cost:
1. Does the named symbol / file / flag already exist? (`grep`, `ls`)
2. Does an unmerged `nightly/*` branch already tick this item?
   (`git show <branch>:<rfc-path>`)
3. Does an open PR's title or body reference this RFC or item?

If the deliverable exists, do NOT re-implement it. Tick the box and
commit the reconciliation alone:

    docs(rfc-NNN): tick <PHASE>.<ITEM> — already implemented in <SHA>

Then take the next item. Reconciling a stale checklist IS progress; it
is what stops the next agent from burning its night on the same item.
"""
"""Agent-facing doctrine for RFC 008 — verify before implementing.

Lives here beside the specialist prompts because it is prompt text, not
behavior. `nightly_core.rules` embeds it in the shared rules block so a
single copy reaches every host."""


SPECIALIST_TIER_DEFAULTS: dict[SpecialistRole, ModelTier] = {
    "implementer": "coding",
    "tester": "coding",
    "reviewer": "reasoning",
    "researcher": "lite",
}
"""Default model tier per specialist role — RFC 007 Resolved #4.

The mapping follows one principle: **spend reasoning tokens where a wrong
answer is expensive to detect, and spend fast tokens everywhere else.**

- `implementer` / `tester` → `coding`. These roles turn a settled plan
  into files. They are the fan-out of the fleet, and their output is
  checked by `nightly verify` (lint + type + test) before it can reach a
  PR — so a cheap model's mistakes get caught mechanically. Run them wide
  and at low effort.
- `reviewer` → `reasoning`. Review *is* result validation: the reviewer
  is the last judgment call before a diff becomes a PR, and a missed bug
  here costs far more than the token delta. This is the one place where a
  lite-tier mis-approval is not caught by anything downstream.
- `researcher` → `lite`. Despite the name, this role does file search and
  summarization over a codebase that is already on disk — high-volume
  reading, low-stakes synthesis. It is the cheapest role to run wide and
  the one that benefits least from deliberation.

Plan frontmatter (`model_tier:`) overrides any of these per task; see
`nightly_core.routing.resolve_model_for_task` for the resolution order.

Note this inverts the reviewer/researcher assignment sketched in RFC
007's original draft, which paired role with *seniority* rather than with
*cost-of-being-wrong*. See RFC 007 Resolved #4 for the reconciliation.
"""


def specialist_prompt(role: SpecialistRole) -> str:
    """Return the system prompt for `role`."""
    return _PROMPTS[role]


def tier_for_role(role: SpecialistRole) -> ModelTier:
    """Return the default model tier for `role`.

    Unknown roles fall back to `coding` — the conservative middle tier.
    A future role added to `SpecialistRole` without a matching entry in
    `SPECIALIST_TIER_DEFAULTS` therefore degrades to "same as today"
    rather than silently routing to lite.
    """
    return SPECIALIST_TIER_DEFAULTS.get(role, "coding")


def all_roles() -> list[SpecialistRole]:
    """Return the four specialist roles, in canonical order."""
    return list(get_args(SpecialistRole))
