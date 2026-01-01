"""Subdir-scoped fixtures for the :mod:`syncade.snapshot` test package.

Holds the ``single_commit_repo`` fixture (copied verbatim from the original
``tests/test_snapshot.py``) — shared across the split test files. Scoped to
this subdir so it does not bleed to other test modules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.snapshot._helpers import _commit, _init_repo


@pytest.fixture
def single_commit_repo(tmp_path: Path) -> tuple[Path, str]:
    """Repo with one initial commit on the default branch. Returns
    ``(repo_path, head_sha)``."""
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    sha = _commit(repo_path, {"README.md": "hello\n"}, "initial")
    return repo_path, sha
