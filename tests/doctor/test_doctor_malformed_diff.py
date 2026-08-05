"""Regression tests for doctor's malformed-diff handling (PR-h-02d blocker fix).

A malformed based/scoped diff (unidentifiable ``diff --git`` header when strip targets are
configured) means the real run exits 60 with ``diff_malformed`` before dispatching any
subprocess. Doctor must surface this as a cheap RED row so the ``$0`` spend-gating rule
skips the live auth + producer-commit legs — the same guarantee it provides for other
doomed runs (dirty-tree, default-branch, etc.).

Two failure modes from the prior implementation that these tests pin:
- ``_check_branch`` returned an OK "guard N/A" row (matching the known-empty case) instead
  of RED, so the live legs ran on a run that would refuse before dispatch.
- ``check_plan`` reported a diff-size summary instead of RED, so there was no cheap red to
  trigger the spend gate even when the branch row was fixed.
"""

from __future__ import annotations

import pytest

from syncade import doctor, doctor_preview
from syncade.config import SyncadeConfig
from tests.doctor._helpers import _repo_with_second_commit

_MALFORMED = ["diff --git a/foo b/foo b/bar"]  # ambiguous header → unidentifiable


@pytest.fixture()
def malformed_env(tmp_path, monkeypatch):
    """A 2-commit repo on ``main`` with ``unidentifiable_sections`` patched to return a
    non-empty list, simulating the ambiguous-path-header diff_malformed condition."""
    repo, base = _repo_with_second_commit(tmp_path / "m")
    monkeypatch.setattr(doctor_preview, "unidentifiable_sections", lambda _: _MALFORMED)
    return repo, base


class TestMalformedDiffBranchCheck:
    """``_check_branch`` must be RED for a malformed diff, not the 'guard N/A' OK row."""

    def test_branch_is_red_not_guard_na(self, malformed_env):
        repo, base = malformed_env
        chk = doctor.collect_checks(SyncadeConfig(), repo, base_ref=base, max_rounds=3)
        branch = next(c for c in chk if c.name == "branch")
        assert branch.status == "red", "malformed diff must be red, not the 'guard N/A' OK row"
        assert "malformed" in branch.detail

    def test_malformed_diff_skips_live_spend(self, malformed_env, monkeypatch):
        """Malformed diff is a cheap red; live legs (auth/producer-commit) must not spend."""
        repo, base = malformed_env

        def _boom(*a, **k):
            raise AssertionError("a live leg ran despite a malformed-diff cheap red")

        monkeypatch.setattr(doctor, "probe_credentials", _boom)
        monkeypatch.setattr(doctor, "run_selfcheck", _boom)
        chk = doctor.collect_checks(SyncadeConfig(), repo, base_ref=base, max_rounds=3)
        assert next(c for c in chk if c.name == "auth").status == "skip"
        assert next(c for c in chk if c.name == "producer-commit").status == "skip"


class TestMalformedDiffPlanCheck:
    """``check_plan`` must be RED for a malformed diff (matching the real run's exit 60)."""

    def test_plan_is_red(self, malformed_env):
        repo, base = malformed_env
        plan = doctor_preview.check_plan(
            repo, SyncadeConfig(), base_ref=base, scope=None, max_rounds=3
        )
        assert plan.status == "red", "malformed diff must red the plan check"
        assert "malformed" in plan.detail or "ambiguous" in plan.detail
        assert plan.fix
