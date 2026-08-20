"""PR-h-field-07 item 5 — ``syncade --update`` proves or refuses, and never upgrades in place.

Install layouts are built on disk (a real ``uv-receipt.toml``, a real ``pipx_metadata.json``, a
real ``pyproject.toml``) and ``syncade.__file__`` is pointed inside them, so the detector is
exercised against the shape it actually looks for rather than a mocked answer.

The refusals matter more than the happy path. Upgrading a source checkout, or upgrading while a
review is running, are the two ways this command could destroy work rather than merely fail.
"""

from __future__ import annotations

import json
import pathlib
import sys
from pathlib import Path

import pytest

from syncade.cli import update_mode
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.process import SubprocessError, SubprocessResult

# Capture the real _resync_skills before any test's autouse fixture can replace it.
_real_resync_skills = update_mode._resync_skills


def _install_tree(tmp_path: Path, marker: str, content: str = "") -> Path:
    """A venv-shaped install with ``marker`` at its root; returns the syncade package dir."""
    root = tmp_path / "root"
    pkg = root / "lib" / "python3.11" / "site-packages" / "syncade"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (root / marker).write_text(content)
    return pkg


def _point_syncade_at(monkeypatch: pytest.MonkeyPatch, pkg: Path) -> None:
    monkeypatch.setattr(update_mode.syncade, "__file__", str(pkg / "__init__.py"))


@pytest.fixture(autouse=True)
def _no_live_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(update_mode, "live_run", lambda cwd: None)
    # Return True (all skills ok) so tests that focus on the upgrade path are not affected by
    # resync failure. Tests that exercise resync failure stub this to return False explicitly.
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: True)


# --------------------------------------------------------------------------- detection


def test_uv_is_proven_by_its_receipt(tmp_path, monkeypatch) -> None:
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    method = update_mode.detect_install()
    assert method.kind == "uv"
    assert method.command == ["uv", "tool", "upgrade", "syncade"]


def test_pipx_is_proven_by_its_metadata(tmp_path, monkeypatch) -> None:
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "pipx_metadata.json", "{}"))
    method = update_mode.detect_install()
    assert method.kind == "pipx"
    assert method.command == ["pipx", "upgrade", "syncade"]


def test_a_source_checkout_is_recognised_and_never_upgraded(tmp_path, monkeypatch, capsys) -> None:
    """The case a developer hits first. `uv tool upgrade` would not touch their tree and would
    not say why nothing changed."""
    root = tmp_path / "checkout"
    pkg = root / "src" / "syncade"
    pkg.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "syncade"\nversion = "0.6.2"\n')
    _point_syncade_at(monkeypatch, pkg)

    assert update_mode.detect_install().kind == "source"
    ran: list = []
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: ran.append(a))
    assert update_mode.run_update(cwd=tmp_path) == WORKTREE_ERROR
    assert ran == [], "a checkout must not be handed to a package manager"
    assert "source checkout" in capsys.readouterr().err


def test_someone_elses_pyproject_cannot_claim_us(tmp_path, monkeypatch) -> None:
    """A `pyproject.toml` for a DIFFERENT project sitting above the install must not be read as
    'syncade is a checkout here' — the name is what proves it."""
    root = tmp_path / "root"
    pkg = root / "lib" / "python3.11" / "site-packages" / "syncade"
    pkg.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "some-other-tool"\n')
    _point_syncade_at(monkeypatch, pkg)
    assert update_mode.detect_install().kind == "unknown"


def test_an_unprovable_install_refuses_and_prints_the_manual_command(
    tmp_path, monkeypatch, capsys
) -> None:
    """Prove or refuse. Guessing `uv tool upgrade` for a pip install would fail confusingly."""
    pkg = tmp_path / "somewhere" / "syncade"
    pkg.mkdir(parents=True)
    _point_syncade_at(monkeypatch, pkg)
    ran: list = []
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: ran.append(a))

    assert update_mode.run_update(cwd=tmp_path) == WORKTREE_ERROR
    err = capsys.readouterr().err
    assert "could not determine" in err and "-m pip install -U syncade" in err
    assert ran == []


# --------------------------------------------------------------------------- the upgrade


def _Result(code: int, stderr: str = "") -> SubprocessResult:
    """Build the REAL :class:`SubprocessResult`, never a hand-written stand-in.

    The first version of this file defined a local class with an ``exit_code`` attribute. The
    real type has ``returncode``. Every test passed, and so did every mutant, because the double
    encoded my belief about the contract rather than the contract — so the suite validated
    `run_update` against a type that does not exist, and a real upgrade would have raised
    AttributeError *after* upgrading. Constructing the real dataclass means a field rename breaks
    these tests instead of hiding in them.
    """
    return SubprocessResult(returncode=code, stdout="", stderr=stderr, duration_seconds=0.0)


def test_a_successful_upgrade_tells_the_operator_to_re_run(tmp_path, monkeypatch, capsys) -> None:
    """It cannot switch to the version it just installed: modules already imported stay old
    while anything imported later comes back new."""
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(0))
    assert update_mode.run_update(cwd=tmp_path) == SUCCESS
    err = capsys.readouterr().err
    assert "Re-run your command" in err
    assert "cannot switch" in err


def test_a_failed_upgrade_reports_and_does_not_claim_success(tmp_path, monkeypatch, capsys) -> None:
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(1, "network down"))
    assert update_mode.run_update(cwd=tmp_path) == WORKTREE_ERROR
    err = capsys.readouterr().err
    assert "update failed" in err and "network down" in err
    assert "Re-run your command" not in err


def test_skills_are_resynced_only_after_a_successful_upgrade(tmp_path, monkeypatch) -> None:
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    calls: list = []
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: calls.append(1) or True)

    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(1))
    update_mode.run_update(cwd=tmp_path)
    assert calls == [], "a failed upgrade must not reinstall skills from the old package"

    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(0))
    update_mode.run_update(cwd=tmp_path)
    assert calls == [1]


def test_a_failed_skill_resync_returns_non_zero(tmp_path, monkeypatch, capsys) -> None:
    """run_update() must not report success when skill re-sync was refused.

    The field-motivating case: a successful package upgrade exits 0 while the installed skill
    remains stale and has no install record, so the operator does not know manual action is
    needed. Verified: this test fails against the pre-fix code (resync returns None, run_update
    returns SUCCESS unconditionally).
    """
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(0))
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: False)

    assert update_mode.run_update(cwd=tmp_path) == WORKTREE_ERROR
    err = capsys.readouterr().err
    # The upgrade message still fires — the package IS updated, skill state is the partial part.
    assert "Re-run your command" in err


def test_a_successful_resync_returns_zero(tmp_path, monkeypatch) -> None:
    """run_update() returns SUCCESS only when both the upgrade AND skill re-sync succeed."""
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(0))
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: True)

    assert update_mode.run_update(cwd=tmp_path) == SUCCESS


# --------------------------------------------------------------------------- live-run refusal


def _run_dir(repo: Path, name: str, state: str, pid: int) -> Path:
    d = repo / ".syncade" / "runs" / name
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"state": state, "pid": pid}))
    return d


def test_a_live_run_blocks_the_update(tmp_path, monkeypatch, capsys) -> None:
    """Upgrading underneath a running review is the same half-old/half-new hazard from outside
    the process: legs it has yet to spawn would load the new code."""
    import os

    monkeypatch.undo()  # the autouse fixture stubs live_run; this test needs the real one
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: None)
    _run_dir(tmp_path, "2026-08-17T10-00-00", "running", os.getpid())
    ran: list = []
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: ran.append(a))

    assert update_mode.run_update(cwd=tmp_path) == WORKTREE_ERROR
    assert "a run is still going" in capsys.readouterr().err
    assert ran == []


def test_a_hard_killed_run_does_not_block(tmp_path, monkeypatch) -> None:
    """`running` with a DEAD pid is the breadcrumb of a hard kill, not a live run. Treating it
    as live would wedge `--update` permanently after any SIGKILL."""
    monkeypatch.undo()
    _run_dir(tmp_path, "2026-08-17T09-00-00", "running", 2**31 - 1)  # certainly not alive
    assert update_mode.live_run(tmp_path) is None


def test_a_finished_run_does_not_block(tmp_path, monkeypatch) -> None:
    monkeypatch.undo()
    _run_dir(tmp_path, "2026-08-17T08-00-00", "terminated", 1)
    assert update_mode.live_run(tmp_path) is None


def test_a_corrupt_status_file_does_not_block(tmp_path, monkeypatch) -> None:
    monkeypatch.undo()
    d = tmp_path / ".syncade" / "runs" / "broken"
    d.mkdir(parents=True)
    (d / "status.json").write_text("{ not json")
    assert update_mode.live_run(tmp_path) is None


def test_no_runs_directory_does_not_block(tmp_path, monkeypatch) -> None:
    monkeypatch.undo()
    assert update_mode.live_run(tmp_path) is None


def test_live_run_is_detected_from_a_subdirectory(tmp_path, monkeypatch, capsys) -> None:
    """A run at the repo root must block --update even when invoked from a subdirectory.

    Without the discover_repo_root call in run_update(), live_run() checked the INVOCATION
    directory for .syncade/runs, which is empty in a subdirectory — the run at the root was
    invisible and the upgrade proceeded under an active review.
    """
    import os

    monkeypatch.undo()  # the autouse fixture stubs live_run; this test needs the real one
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: None)

    _run_dir(tmp_path, "2026-08-17T10-00-00", "running", os.getpid())

    subdir = tmp_path / "src" / "pkg"
    subdir.mkdir(parents=True)

    # Patch discover_repo_root to simulate what git would return when run from the subdir.
    import syncade.snapshot as _snap

    monkeypatch.setattr(_snap, "discover_repo_root", lambda p: tmp_path)
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: None)

    assert update_mode.run_update(cwd=subdir) == WORKTREE_ERROR
    assert "a run is still going" in capsys.readouterr().err


def test_a_package_manager_missing_from_path_is_reported_not_escaped(
    tmp_path, monkeypatch, capsys
) -> None:
    """`run_subprocess` RAISES SubprocessError when the binary cannot be launched (verified:
    "executable not found on PATH"). Letting it escape would show an operator a traceback for
    the ordinary case of uv not being installed."""
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))

    def boom(*a, **k):
        raise SubprocessError("executable not found on PATH: 'uv'")

    monkeypatch.setattr(update_mode, "run_subprocess", boom)
    assert update_mode.run_update(cwd=tmp_path) == WORKTREE_ERROR
    err = capsys.readouterr().err
    assert "update failed" in err and "not found on PATH" in err
    assert "Traceback" not in err


def test_the_result_contract_is_the_real_one(tmp_path, monkeypatch, capsys) -> None:
    """Guards the specific defect: `run_update` must read the field the real type HAS.

    Asserted structurally as well as behaviourally, so this fails loudly if either side is
    renamed rather than silently passing against a stale double.
    """
    import dataclasses

    fields = {f.name for f in dataclasses.fields(SubprocessResult)}
    assert "returncode" in fields and "exit_code" not in fields
    assert "result.exit_code" not in (pathlib.Path("src/syncade/cli/update_mode.py").read_text()), (
        "run_update must not read an attribute SubprocessResult does not define"
    )

    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(0))
    assert update_mode.run_update(cwd=tmp_path) == SUCCESS
    assert "Re-run your command" in capsys.readouterr().err


def test_a_venv_under_a_checkout_is_not_a_source_install(tmp_path, monkeypatch, capsys) -> None:
    """`<checkout>/.venv/lib/pythonX/site-packages/syncade` is an ordinary pip install that
    happens to sit inside a checkout. Reading the ancestor `pyproject.toml` as proof of "source"
    made `--update` answer "use git" — remediation that would not update the installed package
    at all. The environment marker has to end the walk before the checkout can claim it.
    """
    checkout = tmp_path / "syncade-checkout"
    venv = checkout / ".venv"
    pkg = venv / "lib" / "python3.11" / "site-packages" / "syncade"
    pkg.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text('[project]\nname = "syncade"\n')
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    _point_syncade_at(monkeypatch, pkg)

    method = update_mode.detect_install()
    assert method.kind == "unknown", "an environment install is not a checkout"
    assert update_mode.run_update(cwd=tmp_path) == WORKTREE_ERROR
    err = capsys.readouterr().err
    assert "-m pip install -U syncade" in err
    assert "source checkout" not in err, "git is the wrong remediation for a venv install"


def test_a_uv_tool_install_still_wins_over_its_own_pyvenv_cfg(tmp_path, monkeypatch) -> None:
    """uv tool roots carry BOTH `uv-receipt.toml` and `pyvenv.cfg`. The specific marker must be
    checked first, or every uv install would degrade to 'unknown' and lose its exact command."""
    root = tmp_path / "root"
    pkg = root / "lib" / "python3.11" / "site-packages" / "syncade"
    pkg.mkdir(parents=True)
    (root / "uv-receipt.toml").write_text("")
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    _point_syncade_at(monkeypatch, pkg)
    assert update_mode.detect_install().kind == "uv"


def test_a_real_checkout_is_still_detected(tmp_path, monkeypatch) -> None:
    """The narrowing must not swallow the case it was built for: a src-layout checkout has no
    `pyvenv.cfg` between the package and the project file."""
    checkout = tmp_path / "co"
    pkg = checkout / "src" / "syncade"
    pkg.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text('[project]\nname = "syncade"\n')
    _point_syncade_at(monkeypatch, pkg)
    assert update_mode.detect_install().kind == "source"


# ----------------------------------------------------------------------- resync remediation message


def test_syncade_argv_uses_current_interpreter() -> None:
    """_syncade_argv() must invoke the running interpreter, not ambient PATH.

    A user may run syncade via absolute path or from inside a venv while a different
    syncade is earlier on PATH.  shutil.which would find the shadow; sys.executable
    always resolves to the interpreter whose site-packages were just upgraded.

    Verified: this test would fail against the pre-fix code that called shutil.which.
    """

    assert update_mode._syncade_argv() == [sys.executable, "-m", "syncade"]


def test_refused_skill_resync_prints_valid_force_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remediation command printed when skill re-sync is refused must be a valid invocation.

    argparse assigns an optional operand to --install-skill positionally, so
    `--install-skill --force-install claude` parses `--force-install` as a flag and `claude`
    as PR_DOC, making the suggested command unusable. The correct form puts the harness name
    before any flags: `--install-skill claude --force-install`.

    Verified: this test fails against the pre-fix code where the message read
    `--install-skill --force-install {harness}`.
    """
    import io
    import subprocess as _subprocess

    from syncade.cli import skill_status as _skill_status_mod

    class _FakeStatus:
        status = "stale"

    monkeypatch.setattr(_skill_status_mod, "skill_status", lambda h: _FakeStatus())
    monkeypatch.setattr(_skill_status_mod, "HARNESSES", ("claude",))

    def _refusing_run(cmd, **kwargs):
        return type("R", (), {"returncode": 60})()

    monkeypatch.setattr(_subprocess, "run", _refusing_run)

    buf = io.StringIO()
    # Call the real function (captured before the autouse fixture replaced it).
    _real_resync_skills(out=buf)
    msg = buf.getvalue()

    # Harness name must appear BEFORE --force-install so argparse assigns it correctly.
    assert "--install-skill claude --force-install" in msg, (
        f"remediation message has wrong argument order: {msg!r}"
    )
    assert "--install-skill --force-install claude" not in msg, (
        f"remediation message uses invalid argument order: {msg!r}"
    )


# ------------------------------------------------------------------ pip is provable (field-09)
#
# A pip install was `unknown` and `--update` refused. Correct under "prove, never guess" — and
# wrong about the facts: pip writes `dist-info/INSTALLER` naming itself, exactly the kind of
# marker the walk already trusts, and syncade never read it. Reported by an operator whose real
# install was `pip install --user` on macOS.


def _write_dist_info_installer(pkg: Path, value: str | None) -> None:
    """Write (or omit) INSTALLER in a syncade dist-info directory adjacent to pkg.

    Uses the same site-packages layout that ``_read_scoped_installer`` looks for:
    ``<site-packages>/syncade-X.Y.Z.dist-info/INSTALLER``. When value is ``None``, no
    dist-info is written — the tree appears unproven, as if pip never ran.

    This is disk-based rather than a monkeypatch of ``metadata.distribution`` because the
    fix it tests is a PATH-SCOPED lookup: ambient metadata from a different interpreter's
    dist-info must not classify a markerless tree as pip. Writing a real dist-info in the
    correct directory exercises the actual path; patching the global lookup would not.
    """
    if value is None:
        return
    site_packages = pkg.parent  # pkg == <site-packages>/syncade
    dist_info = site_packages / "syncade-0.1.0.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "INSTALLER").write_text(value, encoding="utf-8")


def test_a_pip_install_is_proven_and_upgraded_with_its_own_interpreter(
    tmp_path, monkeypatch
) -> None:
    pkg = _install_tree(tmp_path, "unrelated.txt")  # no uv/pipx/venv/checkout marker anywhere
    _point_syncade_at(monkeypatch, pkg)
    _write_dist_info_installer(pkg, "pip\n")
    monkeypatch.setattr(update_mode, "_in_user_site", lambda: False)

    method = update_mode.detect_install()

    assert method.kind == "pip"
    assert method.command == [sys.executable, "-m", "pip", "install", "-U", "syncade"]
    assert "pip" not in method.command[:1], (
        "must invoke pip through THIS interpreter, never a bare `pip` from PATH — that resolves "
        "to whichever pip is first and upgrades a different interpreter's syncade"
    )


def test_a_pip_install_in_a_different_site_packages_is_not_proven(tmp_path, monkeypatch) -> None:
    """A dist-info that lives in a DIFFERENT site-packages must not prove this tree.

    Regression for the ambient-metadata bug: ``metadata.distribution('syncade')`` found the
    correct dist-info by project name regardless of which interpreter's tree it belonged to,
    so a markerless worktree where ``syncade.__file__`` pointed elsewhere was classified as
    pip because some ambient install had INSTALLER=pip in the ambient interpreter's site dir.
    """
    pkg = _install_tree(tmp_path, "unrelated.txt")
    _point_syncade_at(monkeypatch, pkg)
    # Write the dist-info in a DIFFERENT site-packages tree, not the one pkg lives in.
    other_site = tmp_path / "other_site"
    other_site.mkdir()
    dist_info = other_site / "syncade-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "INSTALLER").write_text("pip", encoding="utf-8")
    # The install tree has no dist-info → must be unknown, not pip.
    assert update_mode.detect_install().kind == "unknown"


def test_a_user_site_install_gets_the_user_flag(tmp_path, monkeypatch) -> None:
    """`--user` is required by a user-site install and rejected by a system-site one, so it is
    derived from where syncade actually lives rather than assumed either way."""
    pkg = _install_tree(tmp_path, "unrelated.txt")
    _point_syncade_at(monkeypatch, pkg)
    _write_dist_info_installer(pkg, "pip")
    monkeypatch.setattr(update_mode, "_in_user_site", lambda: True)

    assert update_mode.detect_install().command == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-U",
        "--user",
        "syncade",
    ]


def test_an_installer_that_is_not_pip_is_still_unknown(tmp_path, monkeypatch) -> None:
    """uv and pipx write INSTALLER naming THEMSELVES. Reaching here means their own markers did
    not match, so the environment is not one we can act on — unproven stays unproven."""
    for value in ("uv", "pipx", "poetry", "", None, "PIP", " pip x "):
        pkg = _install_tree(tmp_path / str(abs(hash(str(value)))), "unrelated.txt")
        _point_syncade_at(monkeypatch, pkg)
        _write_dist_info_installer(pkg, value)
        method = update_mode.detect_install()
        assert method.kind == "unknown", f"INSTALLER={value!r} must not be treated as pip"
        assert method.command is None


def test_a_missing_distribution_is_unknown_not_a_crash(tmp_path, monkeypatch) -> None:
    """No dist-info directory present means the tree is unproven — must not crash."""
    pkg = _install_tree(tmp_path, "unrelated.txt")
    _point_syncade_at(monkeypatch, pkg)
    # No dist-info written: _read_scoped_installer returns "" → unknown.
    assert update_mode.detect_install().kind == "unknown"


def test_contradictory_dist_info_directories_are_unknown(tmp_path, monkeypatch) -> None:
    """Two syncade dist-info dirs with different INSTALLER values must not prove pip.

    Broken packaging state (e.g. a leftover from a previous install), but the conservative
    posture is: first-match is order-dependent and therefore not proof; refuse.
    """
    pkg = _install_tree(tmp_path, "unrelated.txt")
    _point_syncade_at(monkeypatch, pkg)
    site_packages = pkg.parent
    # Write two dist-info dirs with contradictory INSTALLER values.
    for name, installer in [
        ("syncade-0.1.0.dist-info", "pip"),
        ("syncade-0.2.0.dist-info", "poetry"),
    ]:
        d = site_packages / name
        d.mkdir()
        (d / "INSTALLER").write_text(installer, encoding="utf-8")

    assert update_mode.detect_install().kind == "unknown"


@pytest.mark.parametrize(
    ("marker", "expected"),
    [("uv-receipt.toml", "uv"), ("pipx_metadata.json", "pipx"), ("pyvenv.cfg", "pip")],
)
def test_the_specific_markers_are_consulted_before_installer(
    tmp_path, monkeypatch, marker, expected
) -> None:
    """ORDERING IS LOAD-BEARING. pipx installs *through* pip, so its dist-info can name pip —
    consulting INSTALLER first would run bare pip against a pipx-managed venv. The specific
    markers win; INSTALLER is the fallback. `pyvenv.cfg` is the deliberate exception: a venv whose
    manager is unproven IS answerable by INSTALLER, which is the case this PR adds.
    """
    pkg = _install_tree(tmp_path / marker, marker)
    _point_syncade_at(monkeypatch, pkg)
    _write_dist_info_installer(pkg, "pip")
    assert update_mode.detect_install().kind == expected


def test_a_source_checkout_still_refuses_even_when_installer_says_pip(
    tmp_path, monkeypatch
) -> None:
    """An editable install records pip in INSTALLER while the code IS the operator's worktree.
    Upgrading it would be actively wrong, so the checkout marker must keep winning."""
    pkg = _install_tree(tmp_path, "pyproject.toml", 'name = "syncade"\n')
    _point_syncade_at(monkeypatch, pkg)
    _write_dist_info_installer(pkg, "pip")

    method = update_mode.detect_install()
    assert method.kind == "source"
    assert method.command is None
