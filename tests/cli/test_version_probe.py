"""The post-update version comes from the exact install tree running syncade.

The old probe spawned another interpreter and asked which distribution the name ``syncade``
resolved to. Isolation could hide a user-site install; relaxing it let CWD, user-site metadata,
``sitecustomize``, and shutdown hooks influence the answer. These tests pin the re-derived
property instead: read fresh metadata beside ``syncade.__file__`` and never resolve the name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from syncade.cli import update_mode
from syncade.exit_codes import SUCCESS
from syncade.process import SubprocessResult


def _metadata(version: str, *, name: str = "syncade") -> str:
    return f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"


def _install_tree(
    tmp_path: Path, version: str = "1.2.3", *, installer: str | None = None
) -> tuple[Path, Path]:
    site_packages = tmp_path / "site-packages"
    package_file = site_packages / "syncade" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("__version__ = 'loaded-before-the-update'\n", encoding="utf-8")
    dist_info = site_packages / f"syncade-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(_metadata(version), encoding="utf-8")
    if installer is not None:
        (dist_info / "INSTALLER").write_text(installer, encoding="utf-8")
    return package_file, dist_info


def _point_syncade_at(monkeypatch: pytest.MonkeyPatch, package_file: Path) -> None:
    monkeypatch.setattr(update_mode.syncade, "__file__", str(package_file))


def test_reads_fresh_version_from_the_running_install_tree(tmp_path, monkeypatch) -> None:
    package_file, dist_info = _install_tree(tmp_path, "1.2.3")
    _point_syncade_at(monkeypatch, package_file)

    assert update_mode._installed_version() == "1.2.3"

    replacement = dist_info.with_name("syncade-1.2.4.dist-info")
    dist_info.rename(replacement)
    (replacement / "METADATA").write_text(_metadata("1.2.4"), encoding="utf-8")
    assert update_mode._installed_version() == "1.2.4", "the post-condition must not be cached"


def test_ambient_python_state_and_shutdown_hooks_are_not_probe_channels(
    tmp_path, monkeypatch
) -> None:
    package_file, _ = _install_tree(tmp_path / "real", "1.2.3")
    _point_syncade_at(monkeypatch, package_file)

    hostile_path = tmp_path / "hostile-pythonpath"
    hostile_path.mkdir()
    (hostile_path / "sitecustomize.py").write_text(
        "import atexit\nprint('98.0.0')\natexit.register(lambda: print('99.0.0'))\n",
        encoding="utf-8",
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    cwd_dist = cwd / "syncade-97.0.0.dist-info"
    cwd_dist.mkdir()
    (cwd_dist / "METADATA").write_text(_metadata("97.0.0"), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PYTHONPATH", str(hostile_path))
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "unrelated-user-base"))
    monkeypatch.setattr(
        update_mode,
        "run_subprocess",
        lambda *a, **k: pytest.fail("reading an installed version must not spawn a subprocess"),
    )

    assert update_mode._installed_version() == "1.2.3"


@pytest.mark.parametrize(
    "metadata_bytes",
    [
        None,
        b"\xff",
        b"Metadata-Version: 2.1\nVersion: 1.2.3\n",
        b"Metadata-Version: 2.1\nName: other\nVersion: 1.2.3\n",
        b"Metadata-Version: 2.1\nName: syncade\n",
        b"Metadata-Version: 2.1\nName: syncade\nVersion: 1.2\n",
        b"Metadata-Version: 2.1\nName: syncade\nName: syncade\nVersion: 1.2.3\n",
        b"Metadata-Version: 2.1\nName: syncade\nVersion: 1.2.3\nVersion: 9.9.9\n",
    ],
)
def test_missing_or_malformed_metadata_is_unverifiable(
    tmp_path, monkeypatch, metadata_bytes
) -> None:
    package_file, dist_info = _install_tree(tmp_path)
    metadata_path = dist_info / "METADATA"
    if metadata_bytes is None:
        metadata_path.unlink()
    else:
        metadata_path.write_bytes(metadata_bytes)
    _point_syncade_at(monkeypatch, package_file)

    assert update_mode._installed_version() is None


def test_duplicate_adjacent_dist_info_is_unverifiable(tmp_path, monkeypatch) -> None:
    package_file, _ = _install_tree(tmp_path, "1.2.3")
    site_packages = package_file.parent.parent
    stale = site_packages / "syncade-9.9.9.dist-info"
    stale.mkdir()
    (stale / "METADATA").write_text(_metadata("9.9.9"), encoding="utf-8")
    _point_syncade_at(monkeypatch, package_file)

    assert update_mode._installed_version() is None


def test_non_adjacent_metadata_and_editable_metadata_are_ignored(tmp_path, monkeypatch) -> None:
    package_file = tmp_path / "checkout" / "src" / "syncade" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("__version__ = '0.0.0'\n", encoding="utf-8")
    egg_info = package_file.parent.parent / "syncade.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text(_metadata("8.8.8"), encoding="utf-8")
    _install_tree(tmp_path / "other-install", "9.9.9")
    _point_syncade_at(monkeypatch, package_file)

    assert update_mode._installed_version() is None


def test_user_site_noop_update_reports_already_current(tmp_path, monkeypatch, capsys) -> None:
    current = update_mode.syncade.__version__
    package_file, _ = _install_tree(tmp_path, current, installer="pip")
    site_packages = package_file.parent.parent.resolve()
    _point_syncade_at(monkeypatch, package_file)
    monkeypatch.setattr(update_mode.site, "getusersitepackages", lambda: str(site_packages))
    monkeypatch.setattr(update_mode, "live_run", lambda cwd: None)
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: True)
    monkeypatch.setattr(update_mode, "manifest_once", lambda *, enabled=True: {"latest": current})
    monkeypatch.delenv("CI", raising=False)
    commands: list[list[str]] = []

    def successful_package_manager(cmd, **kwargs):
        commands.append(cmd)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(update_mode, "run_subprocess", successful_package_manager)

    assert update_mode.run_update(cwd=tmp_path) == SUCCESS
    assert commands == [[sys.executable, "-m", "pip", "install", "-U", "--user", "syncade"]]
    out = capsys.readouterr().err
    assert f"already up to date ({current})" in out
    assert "[syncade] updated." not in out
