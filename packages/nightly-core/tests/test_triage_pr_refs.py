"""The guard that stops the cascade re-picking an issue a PR already covers.

`fetch_open_pr_issue_refs_via_gh` is the v0.0.11 fix for issue #27: an
issue claimed by an open PR must not be handed to the agent again. When
it under-reports, the cascade re-picks covered work every boundary — the
exact livelock shape issue #30 documents, 130 identical reroutes deep.
When it over-reports, real work is skipped and never surfaces at all.

It was uncovered, along with the `gh` failure paths around it. Those
failures are ordinary overnight events — no `gh` on PATH, an expired
token, a rate limit, a truncated body — and every one of them must
degrade to "no refs known", never raise into the cascade.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nightly_core import triage
from nightly_core.triage import (
    OpenPRRefs,
    fetch_open_pr_issue_refs_via_gh,
    fetch_via_gh,
)


def _stub(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", exc: BaseException | None = None):
    def fake(*_a, **_k):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(triage.subprocess, "run", fake)
    monkeypatch.setattr(triage.shutil, "which", lambda _n: "/usr/local/bin/gh")


ROOT = Path("/tmp")


def _pr(title: str = "", body: str = "", head: str = "feature/x") -> dict:
    return {"title": title, "body": body, "headRefName": head}


# ── closing keywords (any author, any branch) ─────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "closes #30",
        "close #30",
        "closed #30",
        "fix #30",
        "fixes #30",
        "fixed #30",
        "resolve #30",
        "resolves #30",
        "resolved #30",
        "Fixes: #30",
        "FIXES #30",
    ],
)
def test_every_closing_keyword_form_is_recognised(
    monkeypatch: pytest.MonkeyPatch, phrase: str
) -> None:
    """GitHub's documented grammar. A form this misses is an issue the
    cascade will keep re-picking while a PR sits open against it."""
    _stub(monkeypatch, stdout=json.dumps([_pr(body=phrase)]))
    assert 30 in fetch_open_pr_issue_refs_via_gh(ROOT).closing_refs


def test_closing_keyword_counts_from_any_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A human's PR closing an issue is just as disqualifying as a
    Nightly one — the work is in flight either way."""
    _stub(monkeypatch, stdout=json.dumps([_pr(body="fixes #12", head="someone/else")]))
    assert 12 in fetch_open_pr_issue_refs_via_gh(ROOT).closing_refs


def test_closing_keyword_found_in_the_title(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, stdout=json.dumps([_pr(title="fix #5: the thing")]))
    assert 5 in fetch_open_pr_issue_refs_via_gh(ROOT).closing_refs


def test_a_bare_mention_is_not_a_closing_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "See #9" is a cross-reference, not a claim to be fixing it.
    Treating it as closing would silently drop #9 from triage forever."""
    _stub(monkeypatch, stdout=json.dumps([_pr(body="see #9 for context")]))
    assert 9 not in fetch_open_pr_issue_refs_via_gh(ROOT).closing_refs


# ── bare mentions (Nightly-authored branches only) ────────────────────────


def test_bare_mention_on_a_nightly_branch_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare `#N` in an orchestrator-owned PR means the issue is in
    flight even without a closing keyword — the issue #27 fix."""
    _stub(monkeypatch, stdout=json.dumps([_pr(body="context: #27", head="nightly/thing")]))
    refs = fetch_open_pr_issue_refs_via_gh(ROOT)
    assert 27 in refs.nightly_mention_refs


def test_bare_mention_on_a_human_branch_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberately asymmetric. Over-matching on human PRs would let one
    stray `#N` in a description hide a real issue from triage."""
    _stub(monkeypatch, stdout=json.dumps([_pr(body="context: #27", head="feature/x")]))
    assert 27 not in fetch_open_pr_issue_refs_via_gh(ROOT).nightly_mention_refs


def test_the_two_channels_stay_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    """They carry different evidentiary weight, so the cascade must be
    able to tell them apart."""
    _stub(
        monkeypatch,
        stdout=json.dumps(
            [
                _pr(body="fixes #1", head="human/a"),
                _pr(body="touches #2", head="nightly/b"),
            ]
        ),
    )
    refs = fetch_open_pr_issue_refs_via_gh(ROOT)
    assert refs.closing_refs == frozenset({1})
    assert 2 in refs.nightly_mention_refs
    assert 1 not in refs.nightly_mention_refs


# ── malformed and hostile input ───────────────────────────────────────────


def test_non_dict_entries_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, stdout=json.dumps(["not-a-pr", 42, None, _pr(body="fixes #3")]))
    assert 3 in fetch_open_pr_issue_refs_via_gh(ROOT).closing_refs


def test_null_title_and_body_do_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """gh emits `null` for an empty body; string-concatenating it would
    crash the whole scan and take the cascade with it."""
    _stub(
        monkeypatch,
        stdout=json.dumps([{"title": None, "body": None, "headRefName": "nightly/x"}]),
    )
    assert fetch_open_pr_issue_refs_via_gh(ROOT) == OpenPRRefs()


def test_non_json_output_yields_no_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, stdout="rate limit exceeded")
    assert fetch_open_pr_issue_refs_via_gh(ROOT) == OpenPRRefs()


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "gh"),
        subprocess.TimeoutExpired("gh", 30),
        OSError("gh missing"),
    ],
    ids=["gh-failed", "gh-timed-out", "gh-missing"],
)
def test_subprocess_failures_degrade_to_no_refs(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """Under-reporting here re-picks covered issues, which is bad — but
    raising kills the cascade entirely, which is worse."""
    _stub(monkeypatch, exc=exc)
    assert fetch_open_pr_issue_refs_via_gh(ROOT) == OpenPRRefs()


def test_empty_output_is_treated_as_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, stdout="")
    assert fetch_open_pr_issue_refs_via_gh(ROOT) == OpenPRRefs()


# ── issue fetch failure paths ─────────────────────────────────────────────


def test_issue_fetch_returns_empty_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage.shutil, "which", lambda _n: None)
    assert fetch_via_gh(ROOT) == []


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "gh"),
        subprocess.TimeoutExpired("gh", 30),
        OSError("gh missing"),
    ],
    ids=["gh-failed", "gh-timed-out", "gh-missing"],
)
def test_issue_fetch_swallows_subprocess_failures(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    _stub(monkeypatch, exc=exc)
    assert fetch_via_gh(ROOT) == []


def test_issue_parser_skips_entries_missing_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One malformed issue must not discard the whole triage list."""
    good = {
        "number": 7,
        "title": "t",
        "body": "b",
        "labels": [{"name": "bug"}],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "url": "u",
        "author": {"login": "me"},
    }
    _stub(monkeypatch, stdout=json.dumps([{"title": "no number"}, good]))
    issues = fetch_via_gh(ROOT)
    assert [i.number for i in issues] == [7]


def test_issue_parser_survives_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, stdout="<html>502</html>")
    assert fetch_via_gh(ROOT) == []


def test_updated_at_falls_back_to_created_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ranking sorts on recency; a missing `updatedAt` must not crash it."""
    entry = {
        "number": 8,
        "title": "t",
        "body": "",
        "labels": [],
        "createdAt": "2026-01-01T00:00:00Z",
        "url": "u",
        "author": {"login": "me"},
    }
    _stub(monkeypatch, stdout=json.dumps([entry]))
    issue = fetch_via_gh(ROOT)[0]
    assert issue.updated_at == issue.created_at
