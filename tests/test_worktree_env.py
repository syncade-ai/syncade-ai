"""Tests for :mod:`syncade.worktree_env` — PR-23 (worktree Python-env isolation).

**The load-bearing proof.** The venv's editable-install ``.pth``
(``__editable__.syncade-<version>.pth``) is a bare path to the operator's MAIN repo
``src``; it is processed at interpreter startup and added to ``sys.path``, so
*any* Python in that venv resolves ``import syncade`` to MAIN regardless of cwd.
Every worktree subprocess inherits it. :func:`worktree_scoped_env` must make a
**real** child process resolve ``syncade`` to the *worktree's* ``src`` instead.

An env-dict assertion alone is necessary but not sufficient: the ``.pth``
precedence is exactly the thing that bit us, so the gate test launches a real
subprocess in *this* venv (whose ``.pth`` is live) and asserts ``syncade.__file__``
points under the worktree, not MAIN. This is the exact check claude-reviewer had
to hand-invent — now it is enforced.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from syncade.worktree_env import worktree_scoped_env

# A child one-liner that prints where it resolved `import syncade` from.
_PROBE_SRC = "import syncade, sys; sys.stdout.write(syncade.__file__)"

_STRICT_EDITABLE_PROBE_SRC = r"""
import importlib
import importlib.abc
import importlib.machinery
import sys
import types
from pathlib import Path

main_pkg = Path(sys.argv[1])
module = types.ModuleType("__editable___syncade_0_1_0_finder")
module.MAPPING = {"syncade": str(main_pkg)}


class StrictEditableFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "syncade":
            return None
        return importlib.machinery.ModuleSpec(
            fullname,
            importlib.machinery.SourceFileLoader(fullname, str(main_pkg / "__init__.py")),
            is_package=True,
        )


StrictEditableFinder.__module__ = module.__name__
sys.modules[module.__name__] = module
sys.meta_path.insert(0, StrictEditableFinder)

sitecustomize = importlib.import_module("sitecustomize")
importlib.reload(sitecustomize)

import syncade

sys.stdout.write(syncade.__file__)
"""


def make_worktree_src(worktree_root: Path) -> Path:
    """Create ``<worktree_root>/src/syncade/__init__.py`` so a child importing
    ``syncade`` from this worktree is distinguishable from MAIN's."""
    pkg = worktree_root / "src" / "syncade"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("MARKER = 'worktree'\n", encoding="utf-8")
    return worktree_root


def resolve_syncade_in_child(env: dict[str, str], cwd: Path) -> str:
    """Launch a REAL subprocess in the current venv and return the resolved
    ``syncade.__file__``. The venv's site-packages ``.pth`` is live in the child,
    so this faithfully reproduces the production bleed scenario."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE_SRC],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"probe subprocess failed: {proc.stderr!r}"
    return str(Path(proc.stdout.strip()).resolve())


def test_prepends_worktree_src_to_pythonpath(tmp_path):
    env = worktree_scoped_env(tmp_path)
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert (Path(entries[0]) / "sitecustomize.py").is_file()
    assert entries[1] == str(tmp_path / "src")


def test_preserves_inherited_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCADE_PROBE_SENTINEL", "keep-me")
    env = worktree_scoped_env(tmp_path)
    # Auth/keychain/OAuth and everything else must flow through unchanged.
    assert env["SYNCADE_PROBE_SENTINEL"] == "keep-me"
    assert "PATH" in env


def test_appends_to_existing_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/pre/existing")
    env = worktree_scoped_env(tmp_path)
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries[1] == str(tmp_path / "src")  # worktree wins after startup shim
    assert "/pre/existing" in entries  # operator's PYTHONPATH preserved, not clobbered


def test_startup_shim_first_use_is_safe_under_parallel_callers(tmp_path, monkeypatch):
    import syncade.worktree_env as worktree_env

    monkeypatch.setattr(worktree_env.tempfile, "gettempdir", lambda: str(tmp_path))

    caller_count = 64
    replace_barrier = threading.Barrier(caller_count)
    original_replace = worktree_env.os.replace

    def contended_replace(src, dst):
        replace_barrier.wait(timeout=10)
        return original_replace(src, dst)

    monkeypatch.setattr(worktree_env.os, "replace", contended_replace)

    with ThreadPoolExecutor(max_workers=caller_count) as pool:
        envs = list(
            pool.map(
                worktree_scoped_env,
                [tmp_path / f"wt-{index}" for index in range(caller_count)],
            )
        )

    shim_path = tmp_path / worktree_env._SHIM_DIRNAME / "sitecustomize.py"
    assert shim_path.read_text(encoding="utf-8") == worktree_env._SHIM_SOURCE
    assert all(env["PYTHONPATH"].split(os.pathsep)[0] == str(shim_path.parent) for env in envs)


def test_real_subprocess_imports_worktree_src_not_main(tmp_path):
    """LOAD-BEARING gate: a real child resolves the WORKTREE's src, not MAIN's
    — proving PYTHONPATH wins over the startup-prepended editable ``.pth``."""
    import syncade as in_process

    main_pkg_dir = str(Path(in_process.__file__).resolve().parent)
    worktree = make_worktree_src(tmp_path / "wt")
    worktree_pkg_dir = str((worktree / "src" / "syncade").resolve())

    # Sanity: a bare inherited env does NOT resolve to this fresh temp worktree.
    # Use the current process cwd so a caller-level relative PYTHONPATH=src
    # remains anchored to MAIN instead of accidentally pointing at worktree/src.
    bare_resolved = resolve_syncade_in_child(dict(os.environ), Path.cwd())
    assert not bare_resolved.startswith(worktree_pkg_dir)

    # The fix: worktree-scoped env makes the real child import the worktree's
    # src and NOT MAIN's.
    scoped_resolved = resolve_syncade_in_child(worktree_scoped_env(worktree), worktree)
    assert scoped_resolved.startswith(worktree_pkg_dir)
    assert not scoped_resolved.startswith(main_pkg_dir)


def test_strict_editable_finder_is_neutralized_before_syncade_import(tmp_path):
    """A setuptools strict-editable MetaPathFinder for MAIN must not beat the
    worktree's src path in child processes."""
    import syncade as in_process

    main_pkg_dir = str(Path(in_process.__file__).resolve().parent)
    worktree = make_worktree_src(tmp_path / "wt")
    worktree_pkg_dir = str((worktree / "src" / "syncade").resolve())

    proc = subprocess.run(
        [sys.executable, "-c", _STRICT_EDITABLE_PROBE_SRC, main_pkg_dir],
        env=worktree_scoped_env(worktree),
        cwd=str(worktree),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    resolved = str(Path(proc.stdout.strip()).resolve())
    assert resolved.startswith(worktree_pkg_dir)
    assert not resolved.startswith(main_pkg_dir)
