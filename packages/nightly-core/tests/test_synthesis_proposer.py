"""Tests for nightly_core.proposers.synthesis (RFC 009 Phase A)."""

from __future__ import annotations

from pathlib import Path

from nightly_core.proposers.base import (
    STRATEGIC_CATEGORIES,
    Proposal,
)
from nightly_core.proposers.lint_debt import LintDebtProposer
from nightly_core.proposers.synthesis import (
    SynthesisProposer,
    _content_fingerprint,
    _parse_synthesis_output,
    load_synthesis_prompt,
)
from nightly_core.proposers.todo_fixme import TodoFixmeProposer
from nightly_core.proposers.type_holes import TypeHoleProposer

# ── StrategicCategory + Proposal default ──────────────────────────────────


def test_strategic_categories_have_operator_stated_ordering() -> None:
    """RFC 009 §3 — cleaning → refactoring → housekeeping → convenience →
    capability. The list-index IS the priority; reordering breaks the
    cascade sort."""
    assert STRATEGIC_CATEGORIES == (
        "cleaning",
        "refactoring",
        "housekeeping",
        "convenience",
        "capability",
    )


def test_proposal_default_strategic_category_is_housekeeping() -> None:
    """The default backfills cleanly onto Phase-5 proposers (lint_debt,
    todo_fixme, type_holes) — they're individual-line nits, all
    housekeeping flavor per RFC 009 §3."""
    p = Proposal(
        proposer="anything",
        category="lint_debt",
        title="t",
        body="b",
        score=1.0,
    )
    assert p.strategic_category == "housekeeping"


# ── Backfill: the three Phase-5 proposers emit housekeeping ──────────────


def test_lint_debt_proposer_emits_housekeeping(tmp_path: Path) -> None:
    """`LintDebtProposer` with a stubbed runner returns housekeeping
    proposals (the default). The proposer doesn't override the field —
    the dataclass default kicks in."""
    finding = {
        "code": "F401",
        "message": "Unused import",
        "filename": str(tmp_path / "x.py"),
        "fix": {"applicability": "safe"},
    }
    proposer = LintDebtProposer(runner=lambda _root: [finding])
    proposals = list(proposer.propose(tmp_path))
    assert proposals  # non-empty
    assert all(p.strategic_category == "housekeeping" for p in proposals)


def test_todo_fixme_proposer_emits_housekeeping(tmp_path: Path) -> None:
    """Same default-backfill check for `TodoFixmeProposer`."""
    (tmp_path / "a.py").write_text("# TODO: clean this up\n", encoding="utf-8")
    proposer = TodoFixmeProposer()
    proposals = list(proposer.propose(tmp_path))
    assert proposals
    assert all(p.strategic_category == "housekeeping" for p in proposals)


def test_type_holes_proposer_emits_housekeeping(tmp_path: Path) -> None:
    """Same default-backfill check for `TypeHoleProposer`."""
    src = tmp_path / "packages" / "x" / "src" / "x" / "mod.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        "from typing import Any\n\ndef f(x: Any) -> Any:\n    return x\n", encoding="utf-8"
    )
    proposer = TypeHoleProposer()
    proposals = list(proposer.propose(tmp_path))
    # type_holes only fires on module boundaries; even if zero proposals,
    # any that exist must be housekeeping.
    assert all(p.strategic_category == "housekeeping" for p in proposals)


# ── synthesis_prompt.md template contents ─────────────────────────────────


def test_synthesis_prompt_contains_objectives_anchor() -> None:
    """RFC 009 §11 — the load-bearing constraint that keeps the LLM
    from generating generic best-practice filler. Prompt must instruct
    proposals to anchor their rationale to project objectives."""
    prompt = load_synthesis_prompt()
    assert "objectives" in prompt.lower()
    assert "rationale" in prompt.lower()
    assert "README" in prompt


def test_synthesis_prompt_lists_five_categories_in_order() -> None:
    """RFC 009 §3 — the five-category ordering is canonical. The prompt
    must enumerate them in priority sequence so the model produces
    output sortable by `STRATEGIC_CATEGORIES.index(strategic_category)`."""
    prompt = load_synthesis_prompt()
    cleaning_idx = prompt.find("`cleaning`")
    refactoring_idx = prompt.find("`refactoring`")
    housekeeping_idx = prompt.find("`housekeeping`")
    convenience_idx = prompt.find("`convenience`")
    capability_idx = prompt.find("`capability`")
    assert -1 < cleaning_idx < refactoring_idx < housekeeping_idx < convenience_idx < capability_idx


def test_synthesis_prompt_documents_refusal_policy() -> None:
    """RFC 009 §risks — the prompt names the six refusal categories so
    the LLM doesn't propose work the autonomy bar would refuse."""
    prompt = load_synthesis_prompt()
    for refusal in (
        "Destructive git",
        "Production state",
        "External communication",
        "Network egress",
        "Scope creep",
        "Bypassing test or type safety",
    ):
        assert refusal in prompt, f"refusal category missing: {refusal!r}"


def test_synthesis_prompt_specifies_json_array_output() -> None:
    """The output format must be a strict JSON array. The parser has
    no fallback for prose-wrapped or fence-wrapped output."""
    prompt = load_synthesis_prompt()
    assert "JSON array" in prompt
    assert "No markdown code fences" in prompt or "no markdown code fences" in prompt.lower()


# ── _parse_synthesis_output — JSON parsing ────────────────────────────────


def test_parse_synthesis_output_well_formed() -> None:
    payload = """[
        {
            "strategic_category": "cleaning",
            "title": "Remove abandoned vault-knowledge-graph draft HTML",
            "description": "The draft HTML predates RFC 003 and is unused.",
            "file_scope": [".planning/drafts/vault-knowledge-graph.html"],
            "estimated_loc": 200,
            "rationale": "README states vault dashboard ships via the briefing renderer; the draft is unreachable."
        },
        {
            "strategic_category": "capability",
            "title": "Add `nightly watch` verb",
            "description": "Real-time keepalive.log tail with color.",
            "file_scope": ["packages/nightly-core/src/nightly_core/cli.py"],
            "estimated_loc": 80,
            "rationale": "Cross-host suspend/resume objective benefits from a unified watcher."
        }
    ]"""
    proposals = _parse_synthesis_output(payload, max_proposals=25)
    assert len(proposals) == 2
    assert proposals[0].strategic_category == "cleaning"
    assert proposals[1].strategic_category == "capability"
    assert proposals[0].title.startswith("Remove abandoned")
    assert proposals[0].estimated_loc == 200
    assert proposals[1].file_scope == ("packages/nightly-core/src/nightly_core/cli.py",)
    # Each proposal carries the proposer-kind eligibility bucket "synthesis".
    assert all(p.proposer == "synthesis" for p in proposals)
    assert all(p.category == "synthesis" for p in proposals)


def test_parse_synthesis_output_empty_string_returns_empty() -> None:
    assert _parse_synthesis_output("", max_proposals=25) == []
    assert _parse_synthesis_output("   \n", max_proposals=25) == []


def test_parse_synthesis_output_malformed_json_returns_empty() -> None:
    """LLMs sometimes wrap output in fences, prose, or truncate mid-string.
    The parser refuses to guess — it returns empty so the cascade falls
    through to the narrow proposers."""
    assert _parse_synthesis_output("```json\n[]\n```", max_proposals=25) == []
    assert _parse_synthesis_output("Here are the proposals: [", max_proposals=25) == []
    assert _parse_synthesis_output("not json at all", max_proposals=25) == []


def test_parse_synthesis_output_non_array_root_returns_empty() -> None:
    """A JSON object at root (instead of array) is a structural mistake;
    refuse rather than guess at fields."""
    assert _parse_synthesis_output('{"proposals": []}', max_proposals=25) == []


def test_parse_synthesis_output_drops_items_with_unknown_category() -> None:
    """An LLM that hallucinates a sixth category (`maintenance`,
    `documentation`, etc.) gets its item dropped — refuse rather than
    bucket-by-default."""
    payload = """[
        {"strategic_category": "cleaning", "title": "Valid", "description": "ok"},
        {"strategic_category": "maintenance", "title": "Bad bucket", "description": "x"},
        {"strategic_category": "refactoring", "title": "Also valid", "description": "y"}
    ]"""
    proposals = _parse_synthesis_output(payload, max_proposals=25)
    assert len(proposals) == 2
    assert [p.strategic_category for p in proposals] == ["cleaning", "refactoring"]


def test_parse_synthesis_output_drops_items_with_empty_title() -> None:
    payload = """[
        {"strategic_category": "cleaning", "title": "", "description": "x"},
        {"strategic_category": "cleaning", "title": "real", "description": "y"}
    ]"""
    proposals = _parse_synthesis_output(payload, max_proposals=25)
    assert len(proposals) == 1
    assert proposals[0].title == "real"


def test_parse_synthesis_output_respects_max_proposals_cap() -> None:
    """The model may emit > N proposals; the parser truncates at the cap."""
    payload = (
        "["
        + ",".join(
            f'{{"strategic_category": "cleaning", "title": "p{i}", "description": "d"}}'
            for i in range(40)
        )
        + "]"
    )
    proposals = _parse_synthesis_output(payload, max_proposals=10)
    assert len(proposals) == 10


# ── Content-hashed fingerprint (RFC 009 §5) ──────────────────────────────


def test_content_fingerprint_stable_for_same_title_and_scope() -> None:
    """Two synthesis runs that propose the same conceptual change with
    identical wording must dedupe — the fingerprint is a hash of
    (title, sorted file_scope), so identical inputs hash equal."""
    a = _content_fingerprint(
        strategic_category="cleaning",
        title="Remove dead code in cascade.py",
        file_scope=("packages/nightly-core/src/nightly_core/cascade.py",),
    )
    b = _content_fingerprint(
        strategic_category="cleaning",
        title="Remove dead code in cascade.py",
        file_scope=("packages/nightly-core/src/nightly_core/cascade.py",),
    )
    assert a == b
    assert a.startswith("synthesis:cleaning:")


def test_content_fingerprint_differs_for_different_titles() -> None:
    """Two synthesis runs that propose *different* changes in the same
    category + scope both surface — the operator decides at morning
    review time which one wins."""
    a = _content_fingerprint(
        strategic_category="refactoring",
        title="Extract helper from next_task",
        file_scope=("packages/nightly-core/src/nightly_core/cascade.py",),
    )
    b = _content_fingerprint(
        strategic_category="refactoring",
        title="Inline _RFCMatch dataclass",
        file_scope=("packages/nightly-core/src/nightly_core/cascade.py",),
    )
    assert a != b


def test_content_fingerprint_canonicalizes_file_scope_order() -> None:
    """Same set of scope files in different order should hash equal —
    `("a.py", "b.py")` and `("b.py", "a.py")` are the same conceptual
    scope."""
    a = _content_fingerprint(
        strategic_category="cleaning",
        title="Same title",
        file_scope=("a.py", "b.py"),
    )
    b = _content_fingerprint(
        strategic_category="cleaning",
        title="Same title",
        file_scope=("b.py", "a.py"),
    )
    assert a == b


# ── SynthesisProposer end-to-end with injected runner ─────────────────────


def _stub_runner_returning(payload: str):
    def _runner(_prompt: str, _root: Path) -> str:
        return payload

    return _runner


def _stage_nightly_repo(root: Path) -> None:
    """Minimum scaffolding so `SynthesisProposer._build_prompt` doesn't
    short-circuit to empty. Needs a README + at least one packaged
    Python file to populate the code summary."""
    (root / "README.md").write_text("# Test repo\n\nObjective: ship code.\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# Contract\n\nAlways advance.\n", encoding="utf-8")
    src = root / "packages" / "demo" / "src" / "demo" / "__init__.py"
    src.parent.mkdir(parents=True)
    src.write_text("# demo module\n", encoding="utf-8")


def test_synthesis_proposer_empty_repo_returns_no_proposals(tmp_path: Path) -> None:
    """No README + no `packages/` tree → empty prompt → no spawn → no
    proposals. The proposer doesn't even invoke its runner."""
    runner_calls: list[tuple[str, Path]] = []

    def _tracking_runner(prompt: str, root: Path) -> str:
        runner_calls.append((prompt, root))
        return "[]"

    proposer = SynthesisProposer(runner=_tracking_runner)
    proposals = list(proposer.propose(tmp_path))
    assert proposals == []
    assert runner_calls == []


def test_synthesis_proposer_returns_parsed_proposals(tmp_path: Path) -> None:
    """End-to-end: staged repo + stubbed runner → parsed Proposal objects."""
    _stage_nightly_repo(tmp_path)
    payload = """[
        {"strategic_category": "cleaning", "title": "drop X", "description": "dead", "file_scope": ["packages/demo/src/demo/x.py"], "estimated_loc": 50, "rationale": "Obj: ship code."},
        {"strategic_category": "capability", "title": "add Y verb", "description": "new", "file_scope": ["packages/demo/src/demo/cli.py"], "estimated_loc": 100, "rationale": "Obj: ship code."}
    ]"""
    proposer = SynthesisProposer(runner=_stub_runner_returning(payload))
    proposals = list(proposer.propose(tmp_path))
    assert len(proposals) == 2
    assert [p.strategic_category for p in proposals] == ["cleaning", "capability"]


def test_synthesis_proposer_stamps_content_fingerprint(tmp_path: Path) -> None:
    """RFC 009 §5 — synthesis proposals get a `_synthesis_fingerprint`
    attribute carrying the content-hashed value. Cascade-side dedupe
    uses it when present."""
    _stage_nightly_repo(tmp_path)
    payload = """[
        {"strategic_category": "cleaning", "title": "X", "description": "d", "file_scope": ["a.py"]}
    ]"""
    proposer = SynthesisProposer(runner=_stub_runner_returning(payload))
    proposals = list(proposer.propose(tmp_path))
    assert len(proposals) == 1
    p = proposals[0]
    assert hasattr(p, "_synthesis_fingerprint")
    assert p._synthesis_fingerprint.startswith("synthesis:cleaning:")  # type: ignore[attr-defined]


def test_synthesis_proposer_silent_on_runner_error(tmp_path: Path) -> None:
    """A runner that raises (instead of returning empty) must not crash
    the broader proposer pass — the three narrow proposers depend on
    synthesis failing gracefully."""
    _stage_nightly_repo(tmp_path)

    def _exploding_runner(_prompt: str, _root: Path) -> str:
        msg = "synthetic LLM failure"
        raise RuntimeError(msg)

    proposer = SynthesisProposer(runner=_exploding_runner)
    proposals = list(proposer.propose(tmp_path))
    assert proposals == []


def test_synthesis_proposer_respects_max_proposals_init(tmp_path: Path) -> None:
    """`SynthesisProposer(max_proposals=3)` caps the parser output at 3."""
    _stage_nightly_repo(tmp_path)
    payload = (
        "["
        + ",".join(
            f'{{"strategic_category": "cleaning", "title": "p{i}", "description": "d"}}'
            for i in range(20)
        )
        + "]"
    )
    proposer = SynthesisProposer(runner=_stub_runner_returning(payload), max_proposals=3)
    proposals = list(proposer.propose(tmp_path))
    assert len(proposals) == 3


# ── Default proposer registry includes synthesis ─────────────────────────


def test_default_proposers_includes_synthesis() -> None:
    """RFC 009 §A5 — `SynthesisProposer` joins the three Phase-5 proposers
    in `default_proposers()`. Without registration, `nightly ideate`
    would still produce only the narrow output the operator reported."""
    from nightly_core.proposers.registry import default_proposers

    proposers = default_proposers()
    ids = {p.id for p in proposers}
    assert "synthesis" in ids
    # The three Phase-5 proposers still ship alongside.
    assert {"todo_fixme", "lint_debt", "type_holes"}.issubset(ids)


# ── Belt-and-suspenders: ProposerCategory has the new "synthesis" bucket ─


def test_proposer_category_literal_includes_synthesis() -> None:
    """RFC 009 — `synthesis` joins the existing six proposer-kind buckets.
    The autonomy bar reads this for auto-PR eligibility; `synthesis` is
    NOT eligible (per Resolved decision in §A3 — proposals always land
    as draft issues for human review)."""
    from nightly_core.autonomy import AUTO_PR_CATEGORIES

    # Defensive: the autonomy bar must not auto-PR synthesis output.
    assert "synthesis" not in AUTO_PR_CATEGORIES
