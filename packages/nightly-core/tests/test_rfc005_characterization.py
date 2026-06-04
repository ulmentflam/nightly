"""Characterization tests for RFC 005 — interactive seed → accepted RFC.

Phase C1 of RFC 005's Sized checklist. These tests pin down the
end-to-end behavior the operator sees when they invoke `nightly
seed-rfc` from an interactive `/nightly` session:

1. The CLI writes an RFC stub at the next NNN with `status: accepted`
   and `author: nightly-seed`.
2. The cascade's `accepted_rfc` step picks the stub's placeholder
   item — proving the loop closes from seed-rfc → cascade dispatch.
3. The new RFC does NOT outrank older RFCs that still have unchecked
   items, preserving RFC 005 §Resolved-6's "finish what's started"
   invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core.cascade import next_task, pick_accepted_rfc
from nightly_core.cli import app
from nightly_core.plans import parse_frontmatter
from nightly_core.seed_rfc import write_seed_rfc


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare-bones repo root with `.planning/rfcs/` available."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".planning" / "rfcs").mkdir(parents=True)
    # The cascade's `_conclude_requested` / `_session_armed` read from
    # `.nightly/runs/CURRENT`; absence is fine — cascade treats no run
    # as armed=False and won't gate the accepted_rfc step on it.
    return tmp_path


def test_c1_seed_rfc_round_trip_via_cli(repo: Path) -> None:
    """CLI invocation produces a discoverable accepted RFC.

    End-to-end: `nightly seed-rfc "<title>"` writes the file, the
    cascade reader returns it via `_find_accepted_rfc`, and the
    frontmatter carries `author: nightly-seed` for retro filtering.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["seed-rfc", "Interactive seed feature"])
    assert result.exit_code == 0, result.output

    # The file lands at NNN=001 because the test repo starts empty.
    rfc_path = repo / ".planning" / "rfcs" / "001-interactive-seed-feature.md"
    assert rfc_path.is_file()

    metadata, _ = parse_frontmatter(rfc_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "accepted"
    assert metadata["author"] == "nightly-seed"
    assert metadata["source"] == "interactive_seed"

    match = pick_accepted_rfc(repo)
    assert match is not None
    assert match.rfc_path == rfc_path


def test_c1_older_rfc_outranks_newer_seed_rfc(repo: Path) -> None:
    """RFC 005 §Resolved-6 — newer seed-RFC must NOT preempt older RFCs.

    A pre-existing 001 with one unchecked item is picked before a
    seed-stubbed 002, because the cascade walks filenames in numeric
    order and `accepted_rfc` returns the first unchecked match.
    """
    rfcs = repo / ".planning" / "rfcs"
    (rfcs / "001-older.md").write_text(
        "---\nstatus: accepted\n---\n\n## Sized checklist\n\n- [ ] older RFC's unblocked work\n",
        encoding="utf-8",
    )
    write_seed_rfc(repo, title="Newer seed RFC")

    match = pick_accepted_rfc(repo)
    assert match is not None
    assert match.rfc_path.name == "001-older.md"
    assert match.item_text == "older RFC's unblocked work"


def test_c1_seed_rfc_drives_cascade_when_no_older_work_pending(repo: Path) -> None:
    """With no older work, the cascade routes to the seed-RFC's placeholder.

    Mirrors the operator's intended flow: in an interactive session
    with no other pending RFC items, invoking `nightly seed-rfc`
    produces work the cascade dispatches against on the next
    `nightly next` walk.
    """
    write_seed_rfc(repo, title="Only seed in the repo")
    choice = next_task(repo)
    assert choice.source == "accepted_rfc"
    assert "001-only-seed-in-the-repo.md" in str(choice.target_path)


def test_c1_seed_rfc_numbering_skips_past_older_rfcs(repo: Path) -> None:
    """RFC 005 §Resolved-4 — next number is max(found) + 1, not count + 1.

    With 001, 002, 004 present (003 missing), the next seed lands at
    005. Skipped numbers stay skipped — the RFC history is auditable.
    """
    rfcs = repo / ".planning" / "rfcs"
    for n in (1, 2, 4):
        (rfcs / f"{n:03d}-old.md").write_text("---\nstatus: accepted\n---\n# x\n", encoding="utf-8")
    path = write_seed_rfc(repo, title="After the gap")
    assert path.name == "005-after-the-gap.md"
