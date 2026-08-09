"""Shared fixtures for the ``syncade --doctor`` tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from syncade import doctor, doctor_env
from syncade.exit_codes import SUCCESS
from syncade.worktree import DEFAULT_WORKTREE_BASE
from tests.doctor._helpers import (
    _init_git_repo_returning,
    _ok_auth,
    _which_all_present,
)


@pytest.fixture
def healthy_env(monkeypatch, tmp_path):
    """A known-green baseline for ALL of doctor's probes AND a real repo on a feature branch
    (so the branch preview is green). Red tests patch one probe or point at another repo.
    Returns the repo path."""
    monkeypatch.setattr(doctor_env, "which", _which_all_present)
    monkeypatch.setattr(
        doctor_env, "_probe_cli_launch", lambda binary: None
    )  # fake successful exec
    # The worktree probe now reads config.worktree_base (PR-v2-9). Redirect ONLY the default base to
    # a hermetic tmp dir; a test that points config.worktree_base elsewhere still probes THAT base.
    _real_worktree_check = doctor_env._check_worktree_root
    _safe_base = tmp_path / "syncade"
    monkeypatch.setattr(
        doctor_env,
        "_check_worktree_root",
        lambda base: _real_worktree_check(_safe_base if base == DEFAULT_WORKTREE_BASE else base),
    )
    monkeypatch.setattr(doctor_env, "disk_usage", lambda p: SimpleNamespace(free=10 * 1024**3))
    monkeypatch.setattr(doctor, "preflight", lambda *a, **k: [])
    monkeypatch.setattr(doctor, "probe_credentials", lambda *a, **k: [_ok_auth()])
    monkeypatch.setattr(doctor, "run_selfcheck", lambda *a, **k: SUCCESS)
    return _init_git_repo_returning(tmp_path / "repo")
