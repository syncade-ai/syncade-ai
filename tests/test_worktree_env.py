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

import pytest

from syncade.env_scrub import value_references_repo_path
from syncade.worktree_env import (
    producer_scoped_env,
    reviewer_git_ceiling,
    reviewer_scoped_env,
    worktree_scoped_env,
)

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


def test_git_no_replace_objects_is_set(tmp_path):
    """Git subprocesses in a worktree must not honor replacement refs.

    A producer worktree can write to refs/replace/* in the shared common
    dir, so without GIT_NO_REPLACE_OBJECTS a reviewer's `git show HEAD:f.py`
    would silently return replacement object content rather than the object
    named by the snapshot OID.
    """
    env = worktree_scoped_env(tmp_path)
    assert env.get("GIT_NO_REPLACE_OBJECTS") == "1"


def test_reviewer_env_removes_all_inherited_git_channels(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    workspace = tmp_path / "reviewer"
    workspace.mkdir()
    poisoned = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": "/workspace/src",
        "TOKEN": "preserved",
        "GIT_DIR": "/operator/.git",
        "GIT_WORK_TREE": "/operator",
        "GIT_INDEX_FILE": "/operator/.git/index",
        "GIT_OBJECT_DIRECTORY": "/operator/.git/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/operator/.git/objects",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": "/operator",
    }

    env = reviewer_scoped_env(workspace, poisoned, repo_root=repo_root)

    assert env["TOKEN"] == "preserved"
    assert env["PYTHONPATH"] == "/workspace/src"
    assert set(key for key in env if key.startswith("GIT_")) == {
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NO_REPLACE_OBJECTS",
    }
    assert env["GIT_CEILING_DIRECTORIES"] == str(workspace.parent.resolve())


def test_reviewer_env_blocks_upward_repo_discovery(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ancestor, check=True)
    workspace = ancestor / "exports" / "reviewer"
    nested = workspace / "nested"
    nested.mkdir(parents=True)

    poisoned = dict(os.environ)
    poisoned["GIT_DIR"] = str(ancestor / ".git")
    poisoned["GIT_WORK_TREE"] = str(ancestor)
    env = reviewer_scoped_env(workspace, poisoned, repo_root=repo_root)

    for cwd in (workspace, nested):
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, proc.stdout


def test_reviewer_env_scrubs_repo_path_breadcrumbs_and_blocks_pwd_git_recovery(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    secret = "repo-memory-secret"
    (repo / "CLAUDE.md").write_text(secret, encoding="utf-8")
    subprocess.run(["git", "add", "CLAUDE.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )

    workspace = tmp_path / "reviewer"
    workspace.mkdir()
    external_bin = tmp_path / "external-bin"
    external_bin.mkdir()
    workspace_src = workspace / "src"
    workspace_src.mkdir()
    poisoned = {
        "PWD": str(repo),
        "OLDPWD": str(repo / "subdir"),
        "PATH": os.pathsep.join([str(repo / "bin"), str(external_bin), os.environ["PATH"]]),
        "PYTHONPATH": os.pathsep.join([str(repo / "src"), str(workspace_src)]),
        "VIRTUAL_ENV": str(repo / ".venv"),
        "CUSTOM": f"read {repo / 'CLAUDE.md'}",
        "TOKEN": "preserved",
    }

    control = subprocess.run(
        ["git", f"--git-dir={poisoned['PWD']}/.git", "show", "HEAD:CLAUDE.md"],
        cwd=workspace,
        env=poisoned,
        capture_output=True,
        text=True,
    )
    assert control.returncode == 0
    assert secret in control.stdout

    env = reviewer_scoped_env(workspace, poisoned, repo_root=repo)

    assert env["PWD"] == str(workspace.resolve())
    assert env["TOKEN"] == "preserved"
    assert "OLDPWD" not in env
    assert "VIRTUAL_ENV" not in env
    assert "CUSTOM" not in env
    assert str(external_bin) in env["PATH"].split(os.pathsep)
    assert str(repo / "bin") not in env["PATH"].split(os.pathsep)
    assert str(workspace_src) in env["PYTHONPATH"].split(os.pathsep)
    assert str(repo / "src") not in env["PYTHONPATH"].split(os.pathsep)

    resolved_repo = repo.resolve(strict=False)
    leaking = {
        key: value
        for key, value in env.items()
        if not key.startswith("GIT_") and value_references_repo_path(value, resolved_repo)
    }
    assert leaking == {}

    proc = subprocess.run(
        ["/bin/sh", "-c", 'git --git-dir="$PWD/.git" show HEAD:CLAUDE.md'],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert secret not in proc.stdout


def test_reviewer_env_rejects_pathsep_in_git_ceiling_parent(tmp_path):
    workspace = tmp_path / f"colon{os.pathsep}name" / "run" / "reviewer"
    workspace.mkdir(parents=True)

    with pytest.raises(ValueError, match="path-list separator"):
        reviewer_git_ceiling(workspace)

    with pytest.raises(ValueError, match="path-list separator"):
        reviewer_scoped_env(workspace, {}, repo_root=tmp_path / "repo")


def test_reviewer_env_scrubs_repo_path_breadcrumbs(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    workspace = tmp_path / "exports" / "reviewer"
    workspace.mkdir(parents=True)
    sibling_bin = tmp_path / "bin"
    sibling_bin.mkdir()
    repo_bin = repo_root / "bin"
    repo_bin.mkdir()

    env = reviewer_scoped_env(
        workspace,
        {
            "PATH": os.pathsep.join([str(repo_bin), str(sibling_bin)]),
            "PYTHONPATH": str(repo_root / "src"),
            "PWD": str(repo_root),
            "OLDPWD": str(repo_root / "subdir"),
            "VIRTUAL_ENV": str(repo_root / ".venv"),
            "SHELL": "/bin/zsh",
            "TOKEN": "kept",
            "CACHE": f"cache={repo_root / '.cache'}",
            "FILE_URL": f"file://{repo_root / 'CLAUDE.md'}",
            f"{repo_root}/KEY": "path in an environment key",
            "SIBLING": str(tmp_path / "repo-sibling"),
        },
        repo_root=repo_root,
    )

    assert env["PWD"] == str(workspace.resolve())
    assert "OLDPWD" not in env
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONPATH" not in env
    assert "CACHE" not in env
    assert "FILE_URL" not in env
    assert f"{repo_root}/KEY" not in env
    assert env["PATH"] == str(sibling_bin)
    assert env["TOKEN"] == "kept"
    assert env["SIBLING"] == str(tmp_path / "repo-sibling")
    assert all(not value_references_repo_path(value, repo_root.resolve()) for value in env.values())


def test_reviewer_env_rejects_repo_local_workspace(tmp_path):
    repo_root = tmp_path / "repo"
    workspace = repo_root / "exports" / "reviewer"
    workspace.mkdir(parents=True)

    with pytest.raises(ValueError, match="inside the operator repository"):
        reviewer_scoped_env(workspace, {"PATH": os.environ["PATH"]}, repo_root=repo_root)


@pytest.mark.skipif(os.pathsep != ":", reason="colon path separator attack is POSIX-specific")
def test_reviewer_git_ceiling_rejects_path_list_separator(tmp_path):
    workspace = tmp_path / "colon:name" / "reviewer"
    workspace.mkdir(parents=True)

    with pytest.raises(ValueError, match="path-list separator"):
        reviewer_git_ceiling(workspace)


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


def test_producer_env_removes_operator_paths_and_git_routing(tmp_path):
    operator = tmp_path / "operator"
    workspace = tmp_path / "actor"
    operator.mkdir()
    (workspace / ".git").mkdir(parents=True)
    sibling = tmp_path / "operator-sibling"
    sibling.mkdir()
    poisoned = {
        "PATH": os.pathsep.join((str(operator / "bin"), str(sibling))),
        "PYTHONPATH": os.pathsep.join((str(operator / "src"), str(workspace / "src"))),
        "OPERATOR_FILE": str(operator / "secret"),
        "SIBLING_FILE": str(sibling / "allowed"),
        "GIT_DIR": str(operator / ".git"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": str(operator),
        "HOME": os.environ.get("HOME", "/nonexistent"),
    }

    env = producer_scoped_env(workspace, poisoned, repo_root=operator)

    assert env["PWD"] == str(workspace.resolve())
    assert env["TMPDIR"].startswith(str((workspace / ".git").resolve()))
    assert env["TMP"] == env["TEMP"] == env["TMPDIR"]
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert not any(key.startswith("GIT_") for key in env if key != "GIT_NO_REPLACE_OBJECTS")
    assert not any(value_references_repo_path(value, operator.resolve()) for value in env.values())
    assert env["SIBLING_FILE"] == str(sibling / "allowed")
    assert str(sibling) in env["PATH"]


def test_producer_env_requires_real_git_directory(tmp_path):
    workspace = tmp_path / "actor"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")

    with pytest.raises(ValueError, match="real .git directory"):
        producer_scoped_env(workspace, {}, repo_root=tmp_path / "operator")
