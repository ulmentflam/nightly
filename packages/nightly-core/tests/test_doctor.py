"""Tests for nightly_core.doctor — diagnose & repair drifted installs."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightly_core.cli import app
from nightly_core.doctor import (
    DEFAULT_NIGHTLY_SUBDIRS,
    DoctorReport,
    diagnose_and_repair,
)

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── library-level: diagnose_and_repair ────────────────────────────────────


class _FakeIntegration:
    """Minimal NightlyHostIntegration shim for doctor tests.

    Mirrors the surface doctor inspects: skill_path / conclude_skill_path /
    update_skill_path / is_keepalive_hook_installed / keepalive_support /
    install. Each path is a Path under the repo so writes are local.
    """

    def __init__(
        self,
        root: Path,
        name: str,
        *,
        keepalive_support: str = "forced",
        installed_pieces: tuple[str, ...] = ("main", "conclude", "update", "hook"),
    ) -> None:
        self._root = root
        self._name = name
        self.keepalive_support = keepalive_support
        self._installed_pieces = set(installed_pieces)
        self.install_calls = 0

    def skill_path(self, scope: str) -> Path:
        return self._root / f".fake-{self._name}/skills/nightly/SKILL.md"

    def conclude_skill_path(self, scope: str) -> Path:
        return self._root / f".fake-{self._name}/skills/nightly-conclude/SKILL.md"

    def update_skill_path(self, scope: str) -> Path:
        return self._root / f".fake-{self._name}/skills/nightly-update/SKILL.md"

    def is_installed(self, scope: str) -> bool:
        return self.skill_path(scope).is_file()

    def is_keepalive_hook_installed(self, scope: str = "project") -> bool:
        return "hook" in self._installed_pieces

    async def install(self, scope: str) -> None:
        self.install_calls += 1
        for kind, path in (
            ("main", self.skill_path(scope)),
            ("conclude", self.conclude_skill_path(scope)),
            ("update", self.update_skill_path(scope)),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{kind} for {self._name}\n", encoding="utf-8")
            self._installed_pieces.add(kind)
        self._installed_pieces.add("hook")


def _make_loaders(integrations: dict[str, _FakeIntegration]):
    """Wrap fake integrations as the `host_loader` dict diagnose_and_repair expects."""
    return {name: (lambda _root, inst=inst: inst) for name, inst in integrations.items()}


def test_diagnose_repairs_missing_scaffold(repo: Path) -> None:
    """Empty repo → scaffold + config get created."""
    report = diagnose_and_repair(repo, host_loader={})
    assert isinstance(report, DoctorReport)
    for sub in DEFAULT_NIGHTLY_SUBDIRS:
        assert (repo / ".nightly" / sub).is_dir(), f"missing {sub}"
    assert (repo / ".nightly" / "config.yml").is_file()
    by_name = {c.name: c for c in report.checks}
    assert by_name["nightly_scaffold"].status == "repaired"
    assert by_name["config"].status == "repaired"


def test_dry_run_does_not_write(repo: Path) -> None:
    report = diagnose_and_repair(repo, dry_run=True, host_loader={})
    assert not (repo / ".nightly").exists()
    assert report.dry_run is True
    by_name = {c.name: c for c in report.checks}
    assert by_name["nightly_scaffold"].status == "missing"
    assert by_name["config"].status == "missing"


def test_existing_config_is_not_overwritten(repo: Path) -> None:
    """User edits in config.yml must survive doctor."""
    (repo / ".nightly").mkdir()
    custom = "# user-customized\nhosts: [claude]\nfoo: bar\n"
    (repo / ".nightly" / "config.yml").write_text(custom, encoding="utf-8")
    diagnose_and_repair(repo, host_loader={})
    assert (repo / ".nightly" / "config.yml").read_text(encoding="utf-8") == custom


def test_rules_block_refreshed_when_present(repo: Path) -> None:
    """If AGENTS.md exists with a stale rules block, doctor refreshes it."""
    (repo / "AGENTS.md").write_text(
        "<!-- nightly:rules:start -->\n# old\n<!-- nightly:rules:end -->\n",
        encoding="utf-8",
    )
    report = diagnose_and_repair(repo, host_loader={})
    by_name = {c.name: c for c in report.checks}
    assert by_name["rules"].status == "repaired"
    new_text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "Nightly autonomy contract" in new_text
    assert "# old" not in new_text


def test_rules_skipped_when_no_rules_file(repo: Path) -> None:
    """Doctor doesn't create AGENTS.md / CLAUDE.md from scratch — repair-only."""
    report = diagnose_and_repair(repo, host_loader={})
    by_name = {c.name: c for c in report.checks}
    # No rules file present → seed_rules with create_if_absent=False is a no-op
    assert by_name["rules"].status == "ok"
    assert not (repo / "AGENTS.md").exists()
    assert not (repo / "CLAUDE.md").exists()


def test_host_skipped_when_not_installed(repo: Path) -> None:
    """A host with no skill files in the repo is left alone by default."""
    claude = _FakeIntegration(repo, "claude", installed_pieces=())
    report = diagnose_and_repair(
        repo,
        host_loader=_make_loaders({"claude": claude}),
    )
    by_name = {c.name: c for c in report.checks}
    assert by_name["host:claude"].status == "skipped"
    assert claude.install_calls == 0


def test_host_repaired_when_main_skill_missing_but_companion_present(repo: Path) -> None:
    """Half-broken install: companion file exists but main SKILL.md is gone."""
    claude = _FakeIntegration(repo, "claude", installed_pieces=())
    # Drop only the conclude skill — main and update are missing.
    conclude_path = claude.conclude_skill_path("project")
    conclude_path.parent.mkdir(parents=True, exist_ok=True)
    conclude_path.write_text("stale conclude\n", encoding="utf-8")

    report = diagnose_and_repair(
        repo,
        host_loader=_make_loaders({"claude": claude}),
    )
    by_name = {c.name: c for c in report.checks}
    assert by_name["host:claude"].status == "repaired"
    assert claude.install_calls == 1
    assert claude.skill_path("project").is_file()
    assert claude.update_skill_path("project").is_file()


def test_host_ok_when_everything_present(repo: Path) -> None:
    claude = _FakeIntegration(repo, "claude")
    # Pre-create every file the integration would write
    for p in (
        claude.skill_path("project"),
        claude.conclude_skill_path("project"),
        claude.update_skill_path("project"),
    ):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("present\n", encoding="utf-8")

    report = diagnose_and_repair(
        repo,
        host_loader=_make_loaders({"claude": claude}),
    )
    by_name = {c.name: c for c in report.checks}
    assert by_name["host:claude"].status == "ok"
    assert claude.install_calls == 0


def test_extra_hosts_force_install(repo: Path) -> None:
    """`extra_hosts` installs even a host that isn't currently present."""
    cursor = _FakeIntegration(repo, "cursor", installed_pieces=())
    report = diagnose_and_repair(
        repo,
        host_loader=_make_loaders({"cursor": cursor}),
        extra_hosts=("cursor",),
    )
    by_name = {c.name: c for c in report.checks}
    assert by_name["host:cursor"].status == "repaired"
    assert cursor.install_calls == 1


def test_soft_host_does_not_require_stop_hook(repo: Path) -> None:
    """opencode's keep-alive is `soft` — doctor must not flag a missing hook."""
    opencode = _FakeIntegration(
        repo,
        "opencode",
        keepalive_support="soft",
        installed_pieces=(),  # no hook recorded
    )
    # Pre-create every skill file so only the hook is theoretically missing
    for p in (
        opencode.skill_path("project"),
        opencode.conclude_skill_path("project"),
        opencode.update_skill_path("project"),
    ):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("present\n", encoding="utf-8")

    report = diagnose_and_repair(
        repo,
        host_loader=_make_loaders({"opencode": opencode}),
    )
    by_name = {c.name: c for c in report.checks}
    assert by_name["host:opencode"].status == "ok"
    assert opencode.install_calls == 0


def test_loader_error_recorded_not_raised(repo: Path) -> None:
    """A loader that explodes shouldn't crash doctor — it gets a row."""

    def broken_loader(_root):
        raise RuntimeError("loader broke")

    report = diagnose_and_repair(
        repo,
        host_loader={"weird": broken_loader},
    )
    by_name = {c.name: c for c in report.checks}
    assert by_name["host:weird"].status == "error"
    assert "loader broke" in by_name["host:weird"].detail


def test_report_healthy_helpers(repo: Path) -> None:
    """`healthy` should be True after a successful repair."""
    claude = _FakeIntegration(repo, "claude", installed_pieces=())
    # Pre-touch one skill so the host counts as present
    claude.skill_path("project").parent.mkdir(parents=True, exist_ok=True)
    claude.skill_path("project").write_text("stale main\n", encoding="utf-8")

    report = diagnose_and_repair(
        repo,
        host_loader=_make_loaders({"claude": claude}),
    )
    assert report.healthy is True
    assert report.repaired  # at least one item fixed
    assert not report.errors


# ── CLI-level: nightly doctor ─────────────────────────────────────────────


def test_cli_doctor_repairs_drifted_install(repo: Path) -> None:
    """Run `nightly init` then delete a companion skill — doctor restores it."""
    runner.invoke(app, ["init"])
    # Remove the /nightly-conclude SKILL.md — drift
    conclude = repo / ".claude" / "skills" / "nightly-conclude" / "SKILL.md"
    assert conclude.is_file()
    conclude.unlink()

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert conclude.is_file()
    assert "host claude" in result.output or "host:claude" in result.output
    assert "fixed" in result.output or "repaired" in result.output


def test_cli_doctor_dry_run_writes_nothing(repo: Path) -> None:
    """Empty repo + --dry-run should not create `.nightly/`."""
    result = runner.invoke(app, ["doctor", "--dry-run"])
    assert "dry-run" in result.output
    assert not (repo / ".nightly").exists()
    # Empty repo has drift → exit 1
    assert result.exit_code == 1


def test_cli_doctor_dry_run_healthy_repo(repo: Path) -> None:
    """A healthy repo (post-init) shows no drift on --dry-run."""
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["doctor", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "all clear" in result.output


def test_cli_doctor_initializes_empty_repo(repo: Path) -> None:
    """Doctor in an empty repo creates the scaffold even with no host installed."""
    result = runner.invoke(app, ["doctor"])
    # Without any hosts installed and no --host flag, doctor will create
    # the scaffold + config but won't add a host → still exits ok.
    assert (repo / ".nightly" / "config.yml").is_file()
    for sub in DEFAULT_NIGHTLY_SUBDIRS:
        assert (repo / ".nightly" / sub).is_dir()
    assert result.exit_code == 0, result.output


def test_cli_doctor_host_flag_forces_install(repo: Path) -> None:
    """`--host cursor` installs cursor even in a fresh repo."""
    result = runner.invoke(app, ["doctor", "--host", "cursor"])
    assert result.exit_code == 0, result.output
    assert (repo / ".cursor" / "commands" / "nightly.md").is_file()
    assert (repo / ".cursor" / "commands" / "nightly-conclude.md").is_file()
    assert (repo / ".cursor" / "commands" / "nightly-update.md").is_file()


def test_cli_doctor_all_flag_installs_every_host(repo: Path) -> None:
    """`--all` installs every supported host."""
    result = runner.invoke(app, ["doctor", "--all"])
    assert result.exit_code == 0, result.output
    # Spot-check three hosts
    assert (repo / ".claude" / "skills" / "nightly" / "SKILL.md").is_file()
    assert (repo / ".codex" / "skills" / "nightly" / "SKILL.md").is_file()
    assert (repo / ".cursor" / "commands" / "nightly.md").is_file()
