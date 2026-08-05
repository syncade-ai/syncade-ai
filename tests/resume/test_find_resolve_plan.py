"""Tests for :mod:`syncade.orchestrator.resume` (PR-16 T2 + T3).

Constructs ``<runs_root>/<run-id>/`` directory fixtures directly on
``tmp_path`` (no real syncade subprocess invocation) so the resume
helpers can be exercised in isolation. The orchestrator integration
tests (T4 → ``tests/test_orchestrator.py``) cover the end-to-end
resume path; this file covers the pure-function eligibility +
detection layer. ``plan_resume`` itself lives in
``test_plan_resume.py`` (split out at the 500-LOC gate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.orchestrator.resume import (
    LOOP_MANIFEST_FILENAME,
    ResumeError,
    find_resumable_runs,
    plan_resume,
    resolve_resume_target,
)
from tests.resume._helpers import (
    _write_loop_manifest,
    _write_round_manifest,
    _write_run_init,
)


class TestFindResumableRuns:
    def test_returns_empty_when_runs_root_does_not_exist(self, tmp_path: Path):
        assert find_resumable_runs(tmp_path / "does-not-exist") == []

    def test_returns_empty_when_no_runs(self, tmp_path: Path):
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        assert find_resumable_runs(runs_root) == []

    def test_find_resumable_runs_skips_completed_normally(self, tmp_path: Path):
        """final_exit_code in (0, 20, 30) → NOT eligible. The
        operator's next move on a clean completion is a fresh run."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        for exit_code in (0, 20, 30):
            run_dir = runs_root / f"2026-05-28T10-00-0{exit_code:02d}"
            _write_run_init(run_dir)
            _write_loop_manifest(run_dir, final_exit_code=exit_code)
        assert find_resumable_runs(runs_root) == []

    def test_find_resumable_runs_includes_aborted_environment(self, tmp_path: Path):
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        ids = []
        for exit_code in (40, 60, 70):
            run_id = f"2026-05-28T10-00-{exit_code:02d}"
            ids.append(run_id)
            run_dir = runs_root / run_id
            _write_run_init(run_dir)
            _write_loop_manifest(run_dir, final_exit_code=exit_code)
        result = find_resumable_runs(runs_root)
        assert sorted(result) == sorted(ids)

    def test_find_resumable_runs_includes_interrupted(self, tmp_path: Path):
        """run-init.json present but loop-manifest.json missing →
        ELIGIBLE (operator Ctrl-C / crash / SIGTERM before the
        terminator wrote)."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_id = "2026-05-28T10-00-00"
        _write_run_init(runs_root / run_id)
        # Deliberately no loop-manifest.json.
        assert find_resumable_runs(runs_root) == [run_id]

    def test_find_resumable_runs_skips_non_syncade_directories(self, tmp_path: Path):
        """A directory without run-init.json is not a syncade run
        (or was created by a pre-PR-16 syncade) — skip."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        # A pre-PR-16-shaped dir: round-0/ but no run-init.json.
        (runs_root / "2026-05-27T09-00-00" / "round-0").mkdir(parents=True)
        # A resumable dir.
        _write_run_init(runs_root / "2026-05-28T09-00-00")
        assert find_resumable_runs(runs_root) == ["2026-05-28T09-00-00"]

    def test_find_resumable_runs_returns_newest_first(self, tmp_path: Path):
        """run-id is a UTC timestamp; lexical descending sort
        matches chronological newest-first."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        ids = [
            "2026-05-28T09-00-00",
            "2026-05-28T11-00-00",
            "2026-05-28T10-00-00",
        ]
        for rid in ids:
            _write_run_init(runs_root / rid)
        assert find_resumable_runs(runs_root) == [
            "2026-05-28T11-00-00",
            "2026-05-28T10-00-00",
            "2026-05-28T09-00-00",
        ]

    def test_find_resumable_runs_includes_malformed_loop_manifest(self, tmp_path: Path):
        """A malformed loop-manifest.json doesn't raise or hide the run.

        It is returned so resume/latest can surface the specific corruption
        instead of silently skipping potentially recoverable state.
        """
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        bad = runs_root / "2026-05-28T10-00-00"
        _write_run_init(bad)
        (bad / LOOP_MANIFEST_FILENAME).write_text("not json {", encoding="utf-8")
        # A clean eligible run too — confirms newest-first ordering still holds.
        _write_run_init(runs_root / "2026-05-28T11-00-00")
        result = find_resumable_runs(runs_root)
        assert result == ["2026-05-28T11-00-00", "2026-05-28T10-00-00"]

    def test_find_resumable_runs_excludes_blockers_all_deactivated(self, tmp_path: Path):
        """Exit-10 runs whose termination_reason is blockers_all_deactivated must
        not appear in the resumable list. They are not resumable (plan_resume also
        refuses them), and including them shadows an older decision_needed run when
        the operator does --resume latest.

        decision_needed (exit 10) IS included; blockers_all_deactivated (exit 10)
        is NOT.
        """
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        deactivated_id = "2026-05-28T12-00-00"
        deactivated_dir = runs_root / deactivated_id
        _write_run_init(deactivated_dir)
        _write_loop_manifest(
            deactivated_dir,
            final_exit_code=10,
            termination_reason="blockers_all_deactivated",
        )

        decision_id = "2026-05-28T10-00-00"
        decision_dir = runs_root / decision_id
        _write_run_init(decision_dir)
        _write_loop_manifest(
            decision_dir,
            final_exit_code=10,
            termination_reason="decision_needed",
        )

        result = find_resumable_runs(runs_root)
        assert deactivated_id not in result, (
            "blockers_all_deactivated exit-10 runs must be excluded from resumable list"
        )
        assert decision_id in result, "decision_needed exit-10 runs must remain eligible to resume"

    def test_find_resumable_runs_excludes_deactivated_when_manifest_missing(self, tmp_path: Path):
        """A run with no loop-manifest.json but a decision-needed.md containing
        the blockers_all_deactivated marker must NOT be returned as eligible.

        This closes the partial-finalization window: decision-needed.md is written
        before loop-manifest.json in the blockers_all_deactivated path, so a crash
        between the two writes previously left the run appearing as an interrupted
        (eligible) run."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        # Run with no manifest but the non-resumable decision-needed shape.
        deactivated_id = "2026-05-28T12-00-00"
        deactivated_dir = runs_root / deactivated_id
        _write_run_init(deactivated_dir)
        # Write decision-needed.md with the blockers_all_deactivated marker.
        (deactivated_dir / "decision-needed.md").write_text(
            "# Decision needed\n\n## What each reviewer actually said\n\nsome content\n"
        )
        # No loop-manifest.json — simulating the partial-finalization window.

        # A normal interrupted run (no manifest, no decision-needed.md) is still eligible.
        interrupted_id = "2026-05-28T10-00-00"
        _write_run_init(runs_root / interrupted_id)

        result = find_resumable_runs(runs_root)
        assert deactivated_id not in result, (
            "deactivated-blockers run must be excluded even when manifest is missing"
        )
        assert interrupted_id in result, "ordinary interrupted run must remain eligible"

    def test_plan_resume_refuses_deactivated_when_manifest_missing(self, tmp_path: Path):
        """plan_resume must refuse a blockers_all_deactivated run even when
        loop-manifest.json is absent (partial-finalization window)."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        run_id = "2026-05-28T12-00-00"
        run_dir = runs_root / run_id
        _write_run_init(run_dir, starting_sha="a" * 40)
        _write_round_manifest(
            run_dir,
            round_idx=0,
            snapshot_sha="a" * 40,
            round_exit_code=10,
        )
        # Write the non-resumable decision-needed.md shape.
        (run_dir / "decision-needed.md").write_text(
            "# Decision needed\n\n## What each reviewer actually said\n\nsome content\n"
        )
        # No loop-manifest.json.

        with pytest.raises(ResumeError, match="deactivated all of them"):
            plan_resume(tmp_path, run_dir)


# ---------------------------------------------------------------------------
# resolve_resume_target
# ---------------------------------------------------------------------------


class TestResolveResumeTarget:
    def test_resolve_resume_target_latest_picks_most_recent(self, tmp_path: Path):
        """'latest' returns the newest eligible run-id."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        _write_run_init(runs_root / "2026-05-28T09-00-00")
        _write_run_init(runs_root / "2026-05-28T11-00-00")
        _write_run_init(runs_root / "2026-05-28T10-00-00")
        assert resolve_resume_target(runs_root, "latest") == "2026-05-28T11-00-00"

    def test_resolve_resume_target_latest_no_eligible_raises(self, tmp_path: Path):
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        with pytest.raises(ResumeError, match="no eligible runs to resume"):
            resolve_resume_target(runs_root, "latest")

    def test_resolve_resume_target_latest_filters_by_current_branch(self, tmp_path: Path):
        """'latest' on a specific branch picks the newest eligible
        run that was started on THAT branch — runs from other
        branches are skipped even if they're newer."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        # Newer run on a different branch.
        _write_run_init(runs_root / "2026-05-28T12-00-00", operator_branch="feature/x")
        # Older run on main.
        _write_run_init(runs_root / "2026-05-28T10-00-00", operator_branch="main")
        # 'latest' on main picks the older main run.
        assert (
            resolve_resume_target(runs_root, "latest", current_branch="main")
            == "2026-05-28T10-00-00"
        )

    def test_resolve_resume_target_specific_id_returns_id(self, tmp_path: Path):
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        _write_run_init(runs_root / "2026-05-28T10-00-00")
        assert resolve_resume_target(runs_root, "2026-05-28T10-00-00") == "2026-05-28T10-00-00"

    def test_resolve_resume_target_unknown_id_raises(self, tmp_path: Path):
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        with pytest.raises(ResumeError, match="not found"):
            resolve_resume_target(runs_root, "2026-05-28T10-00-00")

    def test_resolve_resume_target_completed_normally_raises_with_helpful_msg(self, tmp_path: Path):
        """A run that completed normally (exit 0/20/30) gets a
        targeted error message naming the exit code and pointing the
        operator at a fresh run."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir)
        _write_loop_manifest(run_dir, final_exit_code=0)
        with pytest.raises(ResumeError, match="completed normally"):
            resolve_resume_target(runs_root, "2026-05-28T10-00-00")

    def test_resolve_resume_target_missing_run_init_raises(self, tmp_path: Path):
        """A directory without run-init.json (pre-PR-16 layout or
        corruption) is rejected with a specific message."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        (runs_root / "2026-05-28T10-00-00").mkdir()
        with pytest.raises(ResumeError, match="not a syncade run"):
            resolve_resume_target(runs_root, "2026-05-28T10-00-00")

    def test_resolve_resume_target_float_exit_code_refused_matches_find(self, tmp_path: Path):
        """A non-int ``final_exit_code`` (e.g. a hand-edited/foreign-written
        ``40.0``) must be REFUSED here, exactly as ``find_resumable_runs``
        excludes it (``isinstance(..., int)``)."""

        from syncade.orchestrator.resume import find_resumable_runs

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir)
        (run_dir / "loop-manifest.json").write_text(
            json.dumps({"final_exit_code": 40.0}), encoding="utf-8"
        )
        # find_resumable_runs (the single source of truth) excludes the float.
        assert find_resumable_runs(runs_root) == []
        # resolve_resume_target must agree — refuse, not resume.
        with pytest.raises(ResumeError, match="completed normally"):
            resolve_resume_target(runs_root, "2026-05-28T10-00-00")


# ---------------------------------------------------------------------------
# plan_resume
# ---------------------------------------------------------------------------
