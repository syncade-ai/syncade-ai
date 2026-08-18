"""PR-h-field-07 item 3 — is the INSTALLED skill still the one this package ships?

Every case is built by running the REAL installer into a tmp home and then perturbing the result,
rather than by hand-writing a record. A hand-written fixture would encode my belief about the
record format; running the installer encodes the format itself, including the self-hash whose
exact canonical bytes are the whole point of it.

The state that matters most is ``modified`` staying SILENT. An update notice aimed at an
operator's own edits is worse than no notice at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.cli.install_skill import _MANIFEST, _bundle, _dest, install_skill
from syncade.cli.skill_status import _classify, skill_status, stale_skills


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".codex"


def _install(home: Path, codex_home: Path) -> None:
    assert install_skill("all", home=home, codex_home=codex_home) == 0


def _status(home: Path, codex_home: Path, harness: str = "claude") -> str:
    return skill_status(harness, home=home, codex_home=codex_home).status


def _package_newer_than_disk(
    monkeypatch: pytest.MonkeyPatch, *, only: str | tuple[str, ...]
) -> None:
    """Make the PACKAGE newer than what is installed, for the named harnesses.

    Harness-aware on purpose: a lambda ignoring its argument hands claude's bundle to codex,
    which then reads as stale against a bundle that was never its own — a fixture manufacturing
    the very verdict the test is checking for. Takes a tuple rather than a sentinel string for
    the same reason: an unmatched sentinel silently patches NOTHING, which is just as invisible.
    """
    targets = (only,) if isinstance(only, str) else tuple(only)
    assert set(targets) <= {"claude", "codex"}, f"typo in harness name: {targets}"
    real = {h: _bundle(h) for h in ("claude", "codex")}

    def patched(harness: str) -> dict[str, bytes]:
        bundle = dict(real[harness])
        if harness in targets:
            bundle["SKILL.md"] = bundle["SKILL.md"] + b"\na line added in a later release\n"
        return bundle

    monkeypatch.setattr("syncade.cli.skill_status._bundle", patched)


# --------------------------------------------------------------------------- the five states


def test_absent_when_nothing_is_installed(home: Path, codex_home: Path) -> None:
    assert _status(home, codex_home) == "absent"
    assert stale_skills(home=home, codex_home=codex_home) == []


def test_in_sync_straight_after_a_real_install(home: Path, codex_home: Path) -> None:
    _install(home, codex_home)
    assert _status(home, codex_home, "claude") == "in_sync"
    assert _status(home, codex_home, "codex") == "in_sync"
    assert stale_skills(home=home, codex_home=codex_home) == []


def test_stale_when_our_own_older_output_is_on_disk(
    home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install, then make the PACKAGE newer than what is on disk. The files still match the
    record exactly, which is what proves they are ours rather than the operator's."""
    _install(home, codex_home)
    _package_newer_than_disk(monkeypatch, only="claude")
    assert _status(home, codex_home, "claude") == "stale"
    assert [s.harness for s in stale_skills(home=home, codex_home=codex_home)] == ["claude"]


def test_modified_is_silent(home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator edited SKILL.md. Even though the package is ALSO newer, they are never
    nagged — the record contradicts the file on disk, which is proof of their authorship."""
    _install(home, codex_home)
    (_dest("claude", home, codex_home) / "SKILL.md").write_bytes(b"my own notes\n")
    _package_newer_than_disk(monkeypatch, only="claude")
    assert _status(home, codex_home, "claude") == "modified"
    assert [s.harness for s in stale_skills(home=home, codex_home=codex_home)] == []


def test_unknown_when_files_differ_and_no_record_exists(home: Path, codex_home: Path) -> None:
    """The real state of both installs on the author's machine: placed by the shell installer
    PR-h-04.5 deleted, so there is no record and ownership cannot be proven either way."""
    _install(home, codex_home)
    dest = _dest("claude", home, codex_home)
    (dest / _MANIFEST).unlink()
    (dest / "SKILL.md").write_bytes(b"different from the package\n")
    assert _status(home, codex_home, "claude") == "unknown"


def test_a_missing_record_never_produces_a_false_modified_claim(
    home: Path, codex_home: Path
) -> None:
    """The bug real data caught: an ABSENT record was being read as evidence of an edit, which
    silently suppressed the notice for every install predating the current installer."""
    _install(home, codex_home)
    dest = _dest("claude", home, codex_home)
    (dest / _MANIFEST).unlink()
    (dest / "SKILL.md").write_bytes(b"changed\n")
    assert _status(home, codex_home, "claude") != "modified"


# --------------------------------------------------------------------------- record integrity


def test_a_tampered_record_is_not_trusted_to_prove_staleness(
    home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record edited to claim a file is ours must not be able to manufacture a "stale"
    verdict. The self-hash covers the canonical bytes, so any edit reads as no record at all."""
    _install(home, codex_home)
    dest = _dest("claude", home, codex_home)
    record = json.loads((dest / _MANIFEST).read_text())
    record["files"]["SKILL.md"] = "0" * 64
    (dest / _MANIFEST).write_text(json.dumps(record, indent=2) + "\n")
    (dest / "SKILL.md").write_bytes(b"attacker content\n")
    assert _status(home, codex_home, "claude") == "unknown"


def test_a_deleted_file_we_recorded_writing_is_the_operators_doing(
    home: Path, codex_home: Path
) -> None:
    _install(home, codex_home)
    (_dest("claude", home, codex_home) / "SKILL.md").unlink()
    assert _status(home, codex_home, "claude") == "modified"


def test_a_file_new_in_this_package_version_reads_as_stale(
    home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later release adds a file the installed copy never had. Absent from disk AND absent
    from the record is staleness, not an operator deletion."""
    _install(home, codex_home)
    real = _bundle("claude")
    monkeypatch.setattr(
        "syncade.cli.skill_status._bundle", lambda h: {**real, "GUIDE.md": b"new in 0.7.0\n"}
    )
    assert _status(home, codex_home, "claude") == "stale"


# --------------------------------------------------------------------------- never raises


def test_an_unreadable_destination_never_raises(
    home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(home, codex_home)
    monkeypatch.setattr(
        "syncade.cli.skill_status._bundle",
        lambda h: (_ for _ in ()).throw(RuntimeError("packaged bundle unreadable")),
    )
    assert _status(home, codex_home, "claude") == "absent"
    assert stale_skills(home=home, codex_home=codex_home) == []


def test_each_harness_is_classified_independently(
    home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude edited by the operator, Codex genuinely stale: only Codex is reported."""
    _install(home, codex_home)
    (_dest("claude", home, codex_home) / "SKILL.md").write_bytes(b"mine\n")
    _package_newer_than_disk(monkeypatch, only=("claude", "codex"))
    assert _status(home, codex_home, "claude") == "modified"
    assert _status(home, codex_home, "codex") == "stale"
    assert [s.harness for s in stale_skills(home=home, codex_home=codex_home)] == ["codex"]


def test_a_mixed_tree_with_an_operator_extra_file_is_silent(
    home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install, then add an operator-owned extra file and make the package newer than disk.

    The skill should be reported as "modified" (silent), NOT "stale" (actionable).
    "stale" would tell the operator to run --install-skill, which would immediately refuse
    because the extra file is not ours to overwrite — a notice that leads nowhere is worse
    than no notice at all. The mixed-tree case resolves to operator-edited.
    """
    _install(home, codex_home)
    dest = _dest("claude", home, codex_home)
    (dest / "NOTES.md").write_bytes(b"my own notes\n")
    _package_newer_than_disk(monkeypatch, only="claude")
    assert _status(home, codex_home, "claude") == "modified"
    assert stale_skills(home=home, codex_home=codex_home) == []


def test_syncade_owned_file_removed_from_bundle_is_stale_not_modified(
    home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file removed from the current bundle but proven ours by the install record is stale.

    Without this fix: the extra-file scan returns "modified" for any destination entry not in the
    current bundle, ignoring the install record — so an upgrade that drops a file leaves the
    installed skill silently misclassified as operator-modified, and stale_skills() returns empty.
    """
    _install(home, codex_home)
    # Simulate a package upgrade that removes README.md from the bundle.
    real_bundle = dict(_bundle("claude"))
    assert "README.md" in real_bundle, "fixture assumption: README.md is a bundled skill file"
    bundle_without_readme = {k: v for k, v in real_bundle.items() if k != "README.md"}

    def patched_bundle(h: str) -> dict[str, bytes]:
        return bundle_without_readme if h == "claude" else _bundle(h)

    monkeypatch.setattr("syncade.cli.skill_status._bundle", patched_bundle)
    # README.md is still on disk (from the install) and in the record (we wrote it).
    # The skill should be stale (syncade-owned difference), not modified (operator-owned).
    assert _status(home, codex_home, "claude") == "stale"
    assert [s.harness for s in stale_skills(home=home, codex_home=codex_home)] == ["claude"]


def test_a_record_holding_only_its_own_self_hash_proves_nothing(
    home: Path, codex_home: Path
) -> None:
    """The self-hash entry is the record vouching for ITSELF, not a file we wrote.

    Counting it as evidence would let a record that accounts for zero files still produce a
    "stale" verdict — a claim of ownership with nothing behind it. Asserted directly on
    `_classify` because the state is not reachable through a normal install.
    """
    dest = _dest("claude", home, codex_home)
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_bytes(b"whatever is on disk\n")
    only_self_hash = {_MANIFEST: "0" * 64}
    assert _classify(dest, {"SKILL.md": b"what the package ships\n"}, only_self_hash) == "unknown"


def test_codex_home_is_honoured_at_runtime_like_the_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both reviewers flagged this independently: the installer honours ``CODEX_HOME`` and the
    runtime status path re-derived ``~/.codex`` by hand. A Codex skill under a custom home then
    read as ABSENT — no stale notice, and ``--update`` skipped re-installing it, which is the
    exact drift this feature exists to close, hidden by a second copy of one decision.
    """
    home = tmp_path / "home"
    custom = tmp_path / "elsewhere" / "codex"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert install_skill("all", home=home, codex_home=None) == 0
    assert (custom / "skills" / "syncade" / "SKILL.md").is_file(), "installer used CODEX_HOME"

    # The runtime path must look in the SAME place, with no explicit codex_home passed.
    status = skill_status("codex")
    assert status.path == custom / "skills" / "syncade"
    assert status.status == "in_sync", "a real install must not read as absent"
