"""Worktree-scoped Python environment for subprocess legs.

**The bleed this closes.** ``pip install -e .`` drops an editable-install
``.pth`` (``__editable__.syncade-<version>.pth``) into the venv's site-packages
containing a bare path to the operator's MAIN repo ``src``. A bare-path ``.pth``
line is processed by :mod:`site` at interpreter startup and added to
``sys.path`` — **cwd-independent**. So every actor-workspace subprocess (reviewers,
producer, the authoritative test leg, the checks leg) that inherits the
operator's environment resolves ``import syncade`` — and runs ``pytest`` — against
MAIN's ``src``, not the worktree's snapshot. That is a correctness hole (the test
leg certifies the wrong tree) and the cause of claude-reviewer's timeouts (it
burned ~20 min detecting and hand-working-around the wrong-src import).

**Isolation strategy.** The common editable install is the *plain-path* ``.pth``
variant: ``site`` appends MAIN's ``src`` after ``PYTHONPATH``, so putting
``<worktree>/src`` near the front makes the worktree resolve first. Setuptools
can also install a strict editable hook as a ``MetaPathFinder``; that hook beats
``sys.path`` and would still route ``syncade`` imports to MAIN. To cover both
forms, the child env prepends a tiny ``sitecustomize`` shim directory before
``<worktree>/src``. At interpreter startup the shim removes syncade-targeting
editable finders, then normal ``sys.path`` resolution imports the worktree copy.

This is verified empirically, not assumed:
:func:`tests.test_worktree_env.test_real_subprocess_imports_worktree_src_not_main`
launches a real subprocess in *this* venv (whose ``.pth`` is live) and asserts
``syncade.__file__`` resolves under the worktree, not MAIN. A companion test
injects a strict editable-style ``MetaPathFinder`` and verifies the startup shim
neutralizes it before the first ``syncade`` import.

All four legs use :func:`worktree_scoped_env` for Python import fidelity. Reviewer
dispatch then applies :func:`reviewer_scoped_env` to remove ambient Git routing and
block upward repository discovery from its Git-less export.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from syncade.env_scrub import path_is_relative_to, scrub_env_for_repo_paths

_SHIM_DIRNAME = "syncade-worktree-env-shim-v1"
_SHIM_SOURCE = """\
\"\"\"Neutralize parent editable-install hooks for syncade worktree children.\"\"\"

from __future__ import annotations

import sys


def _module_name(obj):
    return getattr(obj, "__module__", None) or type(obj).__module__


def _finder_targets_syncade(finder) -> bool:
    module_name = _module_name(finder)
    module = sys.modules.get(module_name)
    names = set()
    if module is not None:
        mapping = getattr(module, "MAPPING", {})
        namespaces = getattr(module, "NAMESPACES", {})
        if isinstance(mapping, dict):
            names.update(str(name) for name in mapping)
        if isinstance(namespaces, dict):
            names.update(str(name) for name in namespaces)
    return (
        module_name.startswith("__editable___syncade")
        or "syncade" in names
        or any(name.startswith("syncade.") for name in names)
    )


sys.meta_path[:] = [finder for finder in sys.meta_path if not _finder_targets_syncade(finder)]
sys.path[:] = [path for path in sys.path if "__editable__.syncade" not in path]
"""


def _ensure_startup_shim() -> str:
    shim_dir = Path(tempfile.gettempdir()) / _SHIM_DIRNAME
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "sitecustomize.py"
    if not shim_path.exists() or shim_path.read_text(encoding="utf-8") != _SHIM_SOURCE:
        handle, tmp_name = tempfile.mkstemp(
            prefix=f".sitecustomize.{os.getpid()}.",
            suffix=".tmp",
            dir=shim_dir,
            text=True,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                tmp.write(_SHIM_SOURCE)
            os.replace(tmp_path, shim_path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
    return str(shim_dir)


def worktree_scoped_env(worktree_path: Path) -> dict[str, str]:
    """Build a child environment that resolves ``syncade`` to *this worktree's*
    ``src`` instead of the operator's MAIN repo.

    Inherits the operator's environment verbatim (so keychain / OAuth /
    ``ANTHROPIC_API_KEY`` auth still flows through) and prepends a startup shim
    plus ``<worktree_path>/src`` to ``PYTHONPATH`` — ahead of any pre-existing
    ``PYTHONPATH`` and the editable install's MAIN path (see module docstring).

    Args:
        worktree_path: The git-worktree root the subprocess will run in. Its
            ``src`` need not yet exist; a non-existent ``PYTHONPATH`` entry is
            harmlessly ignored by the child interpreter.

    Returns:
        A new ``dict`` suitable for ``run_subprocess(env=...)`` / ``Invocation.env``.
        The caller's own ``os.environ`` is never mutated.
    """
    env = dict(os.environ)
    shim_dir = _ensure_startup_shim()
    worktree_src = str(worktree_path / "src")
    existing = env.get("PYTHONPATH")
    scoped_entries = [shim_dir, worktree_src]
    if existing:
        scoped_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(scoped_entries)
    # Any git command run inside a trusted linked-worktree subprocess must see
    # the ORIGINAL object graph. Without this flag, `git diff HEAD` could read
    # replacement objects rather than the objects named by the snapshot OID.
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def reviewer_git_ceiling(workspace_path: Path) -> str:
    """Return a safe Git discovery ceiling for a reviewer workspace."""
    ceiling = str(workspace_path.parent.resolve())
    if os.pathsep in ceiling:
        raise ValueError(
            "reviewer workspace parent contains the Git path-list separator "
            f"{os.pathsep!r}: {ceiling}"
        )
    return ceiling


def reviewer_scoped_env(
    workspace_path: Path,
    base_env: dict[str, str],
    *,
    repo_root: Path,
) -> dict[str, str]:
    """Remove Git discovery channels from a reviewer subprocess environment."""
    workspace = workspace_path.resolve(strict=False)
    if path_is_relative_to(workspace, repo_root):
        raise ValueError("reviewer workspace must not be inside the operator repository")
    env = {
        key: value
        for key, value in scrub_env_for_repo_paths(base_env, repo_root).items()
        if not key.startswith("GIT_")
    }
    env["PWD"] = str(workspace)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "0"
    # Git still searches above a ceiling equal to the current directory.
    # The workspace's parent is the first directory Git must not enter.
    env["GIT_CEILING_DIRECTORIES"] = reviewer_git_ceiling(workspace_path)
    return env


def producer_scoped_env(
    workspace_path: Path,
    base_env: dict[str, str],
    *,
    repo_root: Path,
) -> dict[str, str]:
    """Constrain producer environment paths to its standalone repository."""
    workspace = workspace_path.resolve(strict=True)
    git_dir = workspace / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise ValueError("producer workspace requires a real .git directory")
    tmp_dir = git_dir / "syncade-tmp"
    tmp_dir.mkdir(exist_ok=True)

    env = {
        key: value
        for key, value in scrub_env_for_repo_paths(base_env, repo_root).items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "PWD": str(workspace),
            "TMPDIR": str(tmp_dir),
            "TMP": str(tmp_dir),
            "TEMP": str(tmp_dir),
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    return env
