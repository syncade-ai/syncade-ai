"""Skill install: the `--install-skill` subcommand / script land the skill, and the
wheel-bundled copies stay in sync with the canonical ones (PR-v2-26 items 6 + E)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "install-skill.sh"

# The convenience shell installer is dev-only and is not part of the pip-installable package
# (`syncade --install-skill`, covered below, is the shipped installer). Skip its tests wherever the
# script is absent — e.g. the public release tree — so the suite stays green there.
_needs_script = pytest.mark.skipif(
    not _SCRIPT.exists(),
    reason="scripts/install-skill.sh not present (dev-only convenience installer)",
)


@pytest.mark.parametrize("name", ["SKILL.md", "README.md"])
@pytest.mark.parametrize(
    ("harness", "canonical"),
    [("claude", ".claude/skills/syncade"), ("codex", ".codex/skills/syncade")],
)
def test_bundled_skill_matches_canonical(harness, canonical, name):
    """The package-data copies under src/syncade/skills/ (what the wheel ships and
    `syncade --install-skill` lays down for pip users) must be byte-identical to the
    canonical .claude/ / .codex/ skill the repo uses. Guards the deliberate duplication."""
    bundled = _REPO / "src" / "syncade" / "skills" / harness / name
    src = _REPO / canonical / name
    assert bundled.read_bytes() == src.read_bytes(), (
        f"{bundled} drifted from {src} — re-copy after editing the canonical skill"
    )


def _run(target: str, home: Path, codex_home: Path | None = None):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        [str(_SCRIPT), target], capture_output=True, text=True, env=env, cwd=str(_REPO)
    )


@_needs_script
def test_installs_both_harnesses(tmp_path):
    home, codex_home = tmp_path / "home", tmp_path / "codex"
    r = _run("all", home, codex_home)
    assert r.returncode == 0, r.stderr
    claude_skill = home / ".claude" / "skills" / "syncade" / "SKILL.md"
    codex_skill = codex_home / "skills" / "syncade" / "SKILL.md"
    assert claude_skill.is_file() and codex_skill.is_file()
    # content is the repo's canonical skill, not a stub
    assert claude_skill.read_text() == (_REPO / ".claude/skills/syncade/SKILL.md").read_text()


@_needs_script
def test_codex_home_defaults_to_dot_codex(tmp_path):
    # No CODEX_HOME → must fall back to $HOME/.codex.
    r = _run("codex", tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".codex" / "skills" / "syncade" / "SKILL.md").is_file()


@_needs_script
def test_idempotent_reinstall(tmp_path):
    assert _run("claude", tmp_path).returncode == 0
    assert _run("claude", tmp_path).returncode == 0  # second run must not fail
    assert (tmp_path / ".claude" / "skills" / "syncade" / "SKILL.md").is_file()


@_needs_script
def test_unknown_target_fails(tmp_path):
    r = _run("bogus", tmp_path)
    assert r.returncode != 0
    assert "unknown target" in r.stderr.lower()


# --- the `syncade --install-skill` subcommand (item E: pip-friendly, no checkout) ---


def test_subcommand_installs_from_bundled_package(tmp_path, monkeypatch):
    from syncade.cli.install_skill import install_skill

    monkeypatch.delenv("CODEX_HOME", raising=False)
    home, codex_home = tmp_path / "home", tmp_path / "cx"
    assert install_skill("all", home=home, codex_home=codex_home) == 0
    claude = home / ".claude" / "skills" / "syncade" / "SKILL.md"
    codex = codex_home / "skills" / "syncade" / "SKILL.md"
    assert claude.is_file() and codex.is_file()
    # content is the bundled package copy
    assert claude.read_text() == (_REPO / "src/syncade/skills/claude/SKILL.md").read_text()


def test_subcommand_codex_home_defaults_under_home(tmp_path, monkeypatch):
    from syncade.cli.install_skill import install_skill

    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert install_skill("codex", home=tmp_path) == 0
    assert (tmp_path / ".codex" / "skills" / "syncade" / "SKILL.md").is_file()


def test_subcommand_unknown_target_is_usage_error(tmp_path):
    from syncade.cli.install_skill import install_skill
    from syncade.exit_codes import CLI_USAGE_ERROR

    assert install_skill("bogus", home=tmp_path) == CLI_USAGE_ERROR


def test_subcommand_replaces_a_symlinked_prior_install(tmp_path, monkeypatch):
    # The README documents a symlink-to-checkout option; re-running the installer over it
    # must not crash (shutil.rmtree refuses symlinks).
    from syncade.cli.install_skill import install_skill

    monkeypatch.delenv("CODEX_HOME", raising=False)
    dest = tmp_path / ".claude" / "skills" / "syncade"
    dest.parent.mkdir(parents=True)
    (tmp_path / "some_checkout").mkdir()
    dest.symlink_to(tmp_path / "some_checkout")
    assert dest.is_symlink()
    assert install_skill("claude", home=tmp_path) == 0
    assert not dest.is_symlink() and (dest / "SKILL.md").is_file()


def test_cli_dispatches_install_skill(tmp_path, monkeypatch):
    from syncade.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert main(["--install-skill", "claude"]) == 0
    assert (tmp_path / ".claude" / "skills" / "syncade" / "SKILL.md").is_file()


def test_install_skill_rejected_with_doctor(tmp_path, monkeypatch, capsys):
    # --install-skill must be refused when combined with --doctor or --quick, not silently
    # installed while bypassing the read-only mode guard.
    from syncade.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["--doctor", "--install-skill", "claude"]) == 2
    assert not (tmp_path / ".claude" / "skills" / "syncade" / "SKILL.md").exists()


def test_install_skill_rejected_with_quick(tmp_path, monkeypatch):
    # --quick implies --doctor; --install-skill must likewise be rejected, not installed.
    from syncade.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["--quick", "--install-skill", "claude"]) == 2
    assert not (tmp_path / ".claude" / "skills" / "syncade" / "SKILL.md").exists()
