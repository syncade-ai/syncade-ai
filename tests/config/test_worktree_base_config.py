"""``worktree_base`` config field (PR-v2-9): where per-run git worktrees are provisioned.

A single top-level ``SyncadeConfig`` value (Q5 — not a ``[worktree]`` block). Covers the schema
default (reproduces the runtime default), TOML load, and round-trip. The ``--worktree-base`` CLI
override lives in ``tests/cli/test_config_overrides.py``; that a run and ``--doctor`` honor it lives
in ``tests/cli/test_config_overrides.py`` and ``tests/doctor/test_doctor.py``."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from syncade.config import SyncadeConfig
from syncade.config_loader import load_config
from syncade.worktree import DEFAULT_WORKTREE_BASE


@pytest.mark.real_worktree_base
def test_default_reproduces_the_runtime_default():
    """Zero-config runs still provision worktrees under ``/tmp/syncade`` — byte-identical."""
    assert SyncadeConfig().worktree_base == DEFAULT_WORKTREE_BASE


def test_round_trips_losslessly():
    want = Path("/fast/disk/syncade")
    cfg = SyncadeConfig.model_validate({"worktree_base": "/fast/disk/syncade"})
    assert cfg.worktree_base == want
    assert SyncadeConfig.model_validate(cfg.model_dump()).worktree_base == want


def test_worktree_base_loads_from_toml(tmp_path):
    """The user path: ``worktree_base = "..."`` at the top level of the TOML reaches the config."""
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        'worktree_base = "/mnt/fast/syncade"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).worktree_base == Path("/mnt/fast/syncade")


def test_embedded_nul_is_rejected_at_config_load():
    """An embedded NUL in worktree_base must raise ValidationError at config load, not crash at
    the OS call site with an uncaught ValueError."""
    with pytest.raises(ValidationError, match="NUL"):
        SyncadeConfig.model_validate({"worktree_base": "/tmp/bad\x00path"})
