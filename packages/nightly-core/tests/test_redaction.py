"""RFC 010 §A4/A5/A9 — the redaction contract.

These tests are the guard on the one Nightly surface that publishes to
the internet. Each pass gets a test for what it *removes* and, where it
matters, a test for what it *keeps* — a redactor that turns everything
into `[REDACTED]` passes any leak test and is useless for debugging, so
category preservation is tested as a requirement, not a nicety.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightly_core.redaction import (
    RedactionConfig,
    RedactionResult,
    _parse_findings,
    llm_backstop,
    load_redaction_config,
    redact,
    write_redaction_map,
)

HOME = "/Users/testoperator"


def _redact(text: str, **kw: object) -> RedactionResult:
    kw.setdefault("home", HOME)
    return redact(text, **kw)  # type: ignore[arg-type]


# ── paths ─────────────────────────────────────────────────────────────────


def test_home_path_is_tokenized() -> None:
    out = _redact(f"worktree at {HOME}/work/secret-project/plan.md").text
    assert "secret-project" not in out
    assert HOME not in out
    assert "~/<path:" in out


def test_same_path_twice_collapses_to_one_token() -> None:
    """The correlation signal is the point — a reader must be able to
    see that two log lines touched the same file."""
    text = f"first {HOME}/a/b.py then {HOME}/a/b.py again"
    out = _redact(text).text
    tokens = [piece for piece in out.split() if piece.startswith("~/<path:")]
    assert len(tokens) == 2
    assert tokens[0] == tokens[1]


def test_different_paths_get_different_tokens() -> None:
    out = _redact(f"{HOME}/a.py and {HOME}/b.py").text
    tokens = [p for p in out.replace(",", " ").split() if p.startswith("~/<path:")]
    assert len(set(tokens)) == 2


def test_path_with_spaces_is_fully_consumed() -> None:
    """macOS paths contain spaces routinely. Stopping at the first space
    would truncate the path and leak everything after it."""
    text = f"{HOME}/Library/Mobile Documents/com~apple~CloudDocs/Repo/file.py exists"
    out = _redact(text).text
    assert "Mobile" not in out
    assert "CloudDocs" not in out
    assert out.endswith("exists")


def test_trailing_sentence_punctuation_is_not_eaten() -> None:
    out = _redact(f"see {HOME}/a/b.py.").text
    assert out.endswith(".")
    assert "b.py" not in out


def test_tilde_path_is_redacted_once_not_twice() -> None:
    """Two sequential prefix passes would hash the first pass's output."""
    out = _redact("config at ~/.config/nightly/redaction.yml").text
    assert out.count("<path:") == 1
    assert "nightly/redaction.yml" not in out


def test_home_path_is_not_double_hashed() -> None:
    out = _redact(f"{HOME}/x/y").text
    assert out.count("<path:") == 1


# ── branches and plan slugs ───────────────────────────────────────────────


def test_nightly_branch_slug_is_hashed_prefix_kept() -> None:
    out = _redact("on branch nightly/auth-rewrite-compliance-q3 now").text
    assert "auth-rewrite" not in out
    assert "nightly/<slug:" in out


def test_branch_run_id_suffix_is_stripped_not_left_dangling() -> None:
    """A dangling timestamp gets mangled by a later pass and reads as
    corruption; the branch pass must consume it."""
    out = _redact("branch nightly/thing-2026-06-06T20-33-32Z pushed").text
    assert "2026-06-06T20-33-32Z" not in out
    assert "<token>" not in out


def test_wip_branch_prefix_is_handled() -> None:
    out = _redact("nightly/wip-secretname here").text
    assert "secretname" not in out


def test_task_slug_keeps_index_hashes_name() -> None:
    """The index is ordering signal worth keeping; the name is not."""
    out = _redact("plan at tasks/0003-payment-router/plan.md").text
    assert "tasks/0003-" in out
    assert "payment-router" not in out


# ── remotes, urls, contact details ────────────────────────────────────────


def test_git_ssh_remote_is_stripped_and_not_read_as_email() -> None:
    """`git@host:org/repo` matches the email shape; misreporting a remote
    as a contact address would be both wrong and a leak."""
    out = _redact("origin git@github.com:acme/private-repo.git").text
    assert "private-repo" not in out
    assert "<git-remote-redacted>" in out
    assert "<email>" not in out


def test_url_keeps_scheme_and_tld_drops_host_and_path() -> None:
    out = _redact("posted to https://internal.acme.example/deploy/prod-2").text
    assert "internal.acme" not in out
    assert "deploy/prod-2" not in out
    assert out.startswith("posted to https://<host:")
    assert ".example/<path-redacted>" in out


def test_email_is_masked() -> None:
    assert "<email>" in _redact("ping alice.smith+dev@corp.example").text


def test_ipv4_and_ipv6_are_masked() -> None:
    out = _redact("from 10.1.2.3 and fe80:0:0:0:1:2:3:4 seen").text
    assert "10.1.2.3" not in out
    assert out.count("<ip>") == 2


def test_key_shaped_string_is_masked() -> None:
    out = _redact("key sk_liveAbCdEf0123456789XyZ used").text
    assert "sk_liveAbCdEf0123456789XyZ" not in out
    assert "<token>" in out


def test_plain_prose_is_left_alone() -> None:
    """False positives cost debuggability. Ordinary sentences must survive."""
    text = "The cascade returned nothing so the agent entered the planning phase."
    assert _redact(text).text == text


def test_run_id_is_not_mistaken_for_a_token() -> None:
    """Run ids are structure the report needs to correlate events."""
    text = "run 2026-06-06T20-33-32Z started"
    assert _redact(text).text == text


# ── project-specific catalog ──────────────────────────────────────────────


def test_project_specific_literal_is_replaced() -> None:
    cfg = RedactionConfig(project_specifics=(("acmecorp", "<org>"),))
    out = _redact("built for acmecorp today", config=cfg).text
    assert "acmecorp" not in out
    assert "<org>" in out


def test_longer_catalog_pattern_wins() -> None:
    """With `acme` applied first, `acme-payments` would leave `<org>-payments`."""
    cfg = RedactionConfig(
        project_specifics=(("acme", "<org>"), ("acme-payments", "<service>")),
    )
    out = _redact("the acme-payments service", config=cfg).text
    assert "payments" not in out
    assert "<service>" in out


# ── config loading ────────────────────────────────────────────────────────


def test_load_redaction_config_defaults_when_absent(tmp_path: Path) -> None:
    cfg = load_redaction_config(tmp_path)
    assert cfg.llm_backstop is True
    assert cfg.project_specifics == ()


def test_load_redaction_config_reads_repo_catalog(tmp_path: Path) -> None:
    nightly = tmp_path / ".nightly"
    nightly.mkdir()
    (nightly / "config.yml").write_text("redaction:\n  llm_backstop: false\n", encoding="utf-8")
    (nightly / "redaction.yml").write_text(
        'project_specifics:\n  - {pattern: "widgetco", replacement: "<org>"}\n',
        encoding="utf-8",
    )
    cfg = load_redaction_config(tmp_path)
    assert cfg.llm_backstop is False
    assert ("widgetco", "<org>") in cfg.project_specifics


def test_malformed_catalog_degrades_rather_than_raising(tmp_path: Path) -> None:
    """A typo in the catalog must not block a bug report."""
    nightly = tmp_path / ".nightly"
    nightly.mkdir()
    (nightly / "redaction.yml").write_text("project_specifics: [oops\n", encoding="utf-8")
    assert load_redaction_config(tmp_path).project_specifics == ()


# ── redaction map ─────────────────────────────────────────────────────────


def test_redaction_map_round_trips_the_substitutions(tmp_path: Path) -> None:
    result = _redact(f"file {HOME}/secret/thing.py")
    report = tmp_path / "report.md"
    report.write_text(result.text, encoding="utf-8")

    written = write_redaction_map(result, report)
    assert written is not None
    payload = json.loads(written.read_text(encoding="utf-8"))

    # Every substitution must be reversible: applying the map backwards
    # to the redacted body reproduces the original text.
    restored = result.text
    for original, replacement in payload["substitutions"].items():
        restored = restored.replace(replacement, original)
    assert f"{HOME}/secret/thing.py" in restored


def test_redaction_map_warns_it_is_local_only(tmp_path: Path) -> None:
    result = _redact(f"{HOME}/a")
    report = tmp_path / "report.md"
    written = write_redaction_map(result, report)
    assert written is not None
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert "LOCAL ONLY" in payload["note"]


def test_redaction_map_is_a_sibling_not_part_of_the_report(tmp_path: Path) -> None:
    """It must never travel with the report it decodes."""
    result = _redact(f"{HOME}/a")
    report = tmp_path / "report.md"
    written = write_redaction_map(result, report)
    assert written is not None
    assert written != report
    assert written.name == "report.md.redaction-map.json"


# ── LLM backstop ──────────────────────────────────────────────────────────


def test_llm_backstop_applies_returned_findings() -> None:
    base = RedactionResult(text="the Bluebird project shipped", passes_applied=("path",))
    out = llm_backstop(
        base,
        runner=lambda _p: json.dumps([{"original": "Bluebird", "redacted": "<codename>"}]),
    )
    assert "Bluebird" not in out.text
    assert "<codename>" in out.text
    assert "llm_backstop" in out.passes_applied


def test_llm_backstop_degrades_silently_when_the_runner_fails() -> None:
    """A scrub stage that crashes must not stop the report shipping."""

    def _boom(_prompt: str) -> str:
        raise RuntimeError("no host on PATH")

    base = RedactionResult(text="body", passes_applied=("path",))
    out = llm_backstop(base, runner=_boom)
    assert out.text == "body"
    assert "llm_backstop" not in out.passes_applied


def test_llm_backstop_ignores_findings_not_present_in_the_body() -> None:
    """A hallucinated finding must not corrupt the report."""
    base = RedactionResult(text="clean body")
    out = llm_backstop(
        base,
        runner=lambda _p: json.dumps([{"original": "nonexistent", "redacted": "<x>"}]),
    )
    assert out.text == "clean body"


@pytest.mark.parametrize(
    "raw",
    [
        '[{"original": "X", "redacted": "<y>"}]',
        '```json\n[{"original": "X", "redacted": "<y>"}]\n```',
        'Here you go:\n[{"original": "X", "redacted": "<y>"}]\nHope that helps.',
    ],
)
def test_parse_findings_survives_fenced_and_prose_wrapped_json(raw: str) -> None:
    """Models fence JSON more often than not; a strict parser would make
    the backstop useless in practice."""
    assert _parse_findings(raw) == [{"original": "X", "redacted": "<y>"}]


@pytest.mark.parametrize("raw", ["", "   ", "no json here", "{}", "not: json"])
def test_parse_findings_returns_empty_on_junk(raw: str) -> None:
    assert _parse_findings(raw) == []
