"""Subdir-scoped fixtures for the ``tests/worktree/`` package."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.worktree._helpers import _make_repo


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """Ephemeral git repo with one commit. Returns ``(repo_path,
    commit_sha)``. The commit contains both ``README.md`` and
    ``CLAUDE.md`` so tests can exercise the ``strip_files`` path."""
    repo_path = tmp_path / "repo"
    sha = _make_repo(
        repo_path,
        {"README.md": "hello\n", "CLAUDE.md": "secret\n"},
    )
    return repo_path, sha


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    """Per-test base directory for worktrees — avoids /tmp/syncade and
    keeps parallel test runs from colliding."""
    return tmp_path / "syncade-worktrees"
