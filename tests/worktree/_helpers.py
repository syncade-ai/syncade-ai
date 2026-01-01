"""Shared helpers for the ``tests/worktree/`` package.

Real ``git`` subprocess helpers used by the worktree fixtures and tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``repo`` and return the completed process."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def _make_repo(repo_path: Path, files: dict[str, str]) -> str:
    """Initialize a git repo at ``repo_path``, commit ``files`` (mapping
    of relative-path to contents), and return the resulting commit SHA."""
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init")
    # User identity is required for `git commit` even on a throwaway repo.
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test")
    # commit.gpgsign would prompt for a key on machines that have it
    # configured globally; force it off for the throwaway repo.
    _git(repo_path, "config", "commit.gpgsign", "false")
    for name, content in files.items():
        (repo_path / name).write_text(content)
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-m", "initial")
    return _git(repo_path, "rev-parse", "HEAD").stdout.strip()
