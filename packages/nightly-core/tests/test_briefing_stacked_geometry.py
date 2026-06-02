"""RFC 001 §B2 — briefing renders a `stacked PR geometry` panel."""

from __future__ import annotations

from pathlib import Path

import nightly_core.briefing as briefing_mod
from nightly_core.briefing import build_context, render_briefing
from nightly_core.runs import Run


def _make_run(tmp_path: Path) -> Run:
    run_path = tmp_path / ".nightly" / "runs" / "2026-05-30T12-00-00Z"
    (run_path / "tasks").mkdir(parents=True)
    return Run(id="2026-05-30T12-00-00Z", path=run_path, is_concluded=False)


def test_briefing_context_carries_empty_geometry_on_main(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(briefing_mod, "_load_stacked_geometry", lambda: ("main", []))
    ctx = build_context(_make_run(tmp_path))
    assert ctx.stacked_geometry == []
    assert ctx.current_branch == "main"


def test_briefing_context_carries_geometry_when_stacked(tmp_path: Path, monkeypatch):
    chain = [
        {
            "number": 57,
            "branch": "nightly/in-flight",
            "url": "https://example/57",
            "declared": False,
        }
    ]
    monkeypatch.setattr(
        briefing_mod, "_load_stacked_geometry", lambda: ("nightly/in-flight", chain)
    )
    ctx = build_context(_make_run(tmp_path))
    assert ctx.stacked_geometry == chain
    assert ctx.current_branch == "nightly/in-flight"


def test_briefing_html_renders_panel_when_stacked(tmp_path: Path, monkeypatch):
    chain = [
        {
            "number": 57,
            "branch": "nightly/in-flight",
            "url": "https://example/57",
            "declared": False,
        },
        {
            "number": 58,
            "branch": "nightly/in-flight",
            "url": "https://example/58",
            "declared": False,
        },
    ]
    monkeypatch.setattr(
        briefing_mod, "_load_stacked_geometry", lambda: ("nightly/in-flight", chain)
    )
    html = render_briefing(_make_run(tmp_path))
    assert "stacked PR geometry" in html
    assert "#57" in html
    assert "#58" in html
    assert "nightly/in-flight" in html
    # Accidental chain → rose border + per-entry accidental marker.
    assert "--rose" in html
    assert "accidental" in html


def test_briefing_html_renders_teal_panel_when_all_declared(tmp_path: Path, monkeypatch):
    """RFC 004 §C — an all-declared chain renders with the teal panel header
    `declared dependency chain` (not the rose `stacked PR geometry`)."""
    chain = [
        {"number": 57, "branch": "nightly/in-flight", "url": "https://example/57", "declared": True}
    ]
    monkeypatch.setattr(
        briefing_mod, "_load_stacked_geometry", lambda: ("nightly/in-flight", chain)
    )
    html = render_briefing(_make_run(tmp_path))
    assert "declared dependency chain" in html
    assert "stacked PR geometry" not in html
    assert "--teal" in html
    assert "depends_on_pr" in html  # explainer mentions the frontmatter key
    assert "Depends on #N" in html  # explainer mentions the PR-body convention


def test_briefing_html_renders_rose_panel_for_mixed_chain(tmp_path: Path, monkeypatch):
    """RFC 004 §C — a chain with even one accidental entry stays rose,
    because accidental is the dominant signal (it's the failure mode)."""
    chain = [
        {
            "number": 57,
            "branch": "nightly/in-flight",
            "url": "https://example/57",
            "declared": True,
        },
        {
            "number": 58,
            "branch": "nightly/in-flight",
            "url": "https://example/58",
            "declared": False,
        },
    ]
    monkeypatch.setattr(
        briefing_mod, "_load_stacked_geometry", lambda: ("nightly/in-flight", chain)
    )
    html = render_briefing(_make_run(tmp_path))
    assert "stacked PR geometry" in html
    assert "declared dependency chain" not in html
    # Per-entry annotations: #57 declared (teal), #58 accidental (rose).
    assert "declared" in html
    assert "accidental" in html


def test_briefing_html_omits_panel_when_geometry_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(briefing_mod, "_load_stacked_geometry", lambda: ("main", []))
    html = render_briefing(_make_run(tmp_path))
    assert "stacked PR geometry" not in html
    assert "declared dependency chain" not in html


def test_load_stacked_geometry_degrades_to_empty_on_cascade_failure(monkeypatch):
    """If detect_stacked_geometry raises, the briefing still renders."""

    def boom():
        raise RuntimeError("cascade unreachable")

    monkeypatch.setattr("nightly_core.cascade.detect_stacked_geometry", boom)
    branch, chain = briefing_mod._load_stacked_geometry()
    assert branch == ""
    assert chain == []
