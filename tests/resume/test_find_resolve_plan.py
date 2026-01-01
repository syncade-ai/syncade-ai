"""Tests for :mod:`syncade.orchestrator.resume` (PR-16 T2 + T3).

Constructs ``<runs_root>/<run-id>/`` directory fixtures directly on
``tmp_path`` (no real syncade subprocess invocation) so the resume
helpers can be exercised in isolation. The orchestrator integration
tests (T4 → ``tests/test_orchestrator.py``) cover the end-to-end
resume path; this file covers the pure-function eligibility +
detection layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.orchestrator.resume import (
    LOOP_MANIFEST_FILENAME,
    ResumeError,
    ResumePlan,
    find_resumable_runs,
    plan_resume,
    resolve_resume_target,
)
from syncade.persistence import RUN_INIT_FILENAME
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
        import json

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


class TestPlanResume:
    def test_plan_resume_finds_first_incomplete_round(self, tmp_path: Path):
        """Round 0 complete (clean manifest), round 1 directory
        missing → resumed_round=1, completed_rounds=[0]."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, max_rounds=3)
        _write_round_manifest(
            run_dir,
            0,
            round_exit_code=30,
            producer_outcome="committed",
            producer_ending_sha="b" * 40,
        )
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 1
        assert plan.completed_rounds == [0]
        assert plan.max_rounds == 3
        # Expected SHA = round-0 producer's ending SHA (branch advanced).
        assert plan.expected_sha == "b" * 40

    def test_plan_resume_round_n_snapshot_from_prior_no_commit(self, tmp_path: Path):
        """Round 0 NO-SHIP, producer stalled (no commit) → resumed
        round 1 snapshots from round-0's snapshot SHA (the branch
        never advanced)."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, max_rounds=3)
        _write_round_manifest(
            run_dir,
            0,
            snapshot_sha="c" * 40,
            round_exit_code=30,
            producer_outcome="stalled",
        )
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 1
        # Expected SHA = round-0's snapshot SHA (branch unchanged).
        assert plan.expected_sha == "c" * 40

    def test_plan_resume_round_zero_when_no_round_dirs(self, tmp_path: Path):
        """No round directories exist → resumed_round=0, empty
        completed_rounds. Expected SHA = run-init's starting_sha."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, starting_sha="d" * 40, max_rounds=3)
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 0
        assert plan.completed_rounds == []
        assert plan.expected_sha == "d" * 40

    def test_plan_resume_drops_round_with_missing_manifest(self, tmp_path: Path):
        """round-0/ exists but has no manifest.json (typical
        interrupted-mid-round shape) → resumed_round=0, drop the
        directory."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, starting_sha="e" * 40, max_rounds=3)
        # Round dir exists but no manifest (e.g. only some reviewers
        # finished before Ctrl-C).
        (run_dir / "round-0").mkdir()
        (run_dir / "round-0" / "claude-reviewer.stdout").write_text("partial")
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 0
        assert plan.completed_rounds == []
        assert plan.expected_sha == "e" * 40

    def test_plan_resume_detects_phase_failure_in_round(self, tmp_path: Path):
        """A round's manifest exists but shows reviewer subprocess
        error → drop and retry this round (don't include in
        completed_rounds)."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, max_rounds=3)
        # Round 0 completed cleanly with committed producer.
        _write_round_manifest(
            run_dir,
            0,
            round_exit_code=30,
            producer_outcome="committed",
            producer_ending_sha="f" * 40,
        )
        # Round 1 had a reviewer subprocess error → drop.
        _write_round_manifest(
            run_dir,
            1,
            snapshot_sha="f" * 40,
            round_exit_code=40,
            reviewers_succeeded=False,
            synth_succeeded=None,  # synth skipped (reviewer failure)
        )
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 1
        assert plan.completed_rounds == [0]
        assert plan.expected_sha == "f" * 40

    def test_plan_resume_detects_producer_subprocess_error_as_phase_failure(self, tmp_path: Path):
        """Round 0's reviewers + synth succeeded but the producer
        subprocess errored → drop round 0 and retry. (Round-level
        granularity per the brief; phase-level skip-already-done is
        explicitly out of scope.)"""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, starting_sha="0" * 40, max_rounds=3)
        _write_round_manifest(
            run_dir,
            0,
            snapshot_sha="0" * 40,
            round_exit_code=30,
            producer_outcome="subprocess_error",
        )
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 0
        assert plan.completed_rounds == []
        assert plan.expected_sha == "0" * 40

    @pytest.mark.parametrize(
        ("block_name", "block_value"),
        [
            ("test_run", {"exit_code": 0}),
            ("test_run", {"outcome": "failed"}),
            ("producer", {"starting_sha": "a" * 40, "ending_sha": "b" * 40}),
            ("producer", {"outcome": "committed", "starting_sha": "a" * 40}),
            ("checks", ["not-a-check-entry"]),
            ("checks", [{"severity": "blocking"}]),
        ],
    )
    def test_plan_resume_raises_resume_error_for_malformed_phase_blocks(
        self,
        tmp_path: Path,
        block_name: str,
        block_value,
    ):
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, starting_sha="0" * 40, max_rounds=2)
        round_dir = _write_round_manifest(
            run_dir,
            0,
            snapshot_sha="0" * 40,
            round_exit_code=0,
        )
        manifest_path = round_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest[block_name] = block_value
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ResumeError, match=block_name):
            plan_resume(tmp_path, run_dir)

    def test_plan_resume_treats_test_failed_as_clean(self, tmp_path: Path):
        """test_run.outcome == 'failed' is a CLEAN signal (the test
        leg ran and exited non-zero) — that's a legitimate round
        result, not a phase failure. The loop terminator would
        exit 30 with a NO-SHIP findings.md. Resume should treat
        this as a completed round."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, max_rounds=3)
        _write_round_manifest(
            run_dir,
            0,
            round_exit_code=30,
            test_outcome="failed",
            producer_outcome="committed",
            producer_ending_sha="9" * 40,
        )
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 1
        assert plan.completed_rounds == [0]

    def test_plan_resume_detects_all_rounds_complete_but_loop_aborted(self, tmp_path: Path):
        """All N rounds completed cleanly per their per-round
        manifests, but the loop-manifest aborted with exit 40
        (e.g., loop-summary write failed). resumed_round = N (the
        round we never reached, equivalent to N+1 in the brief's
        wording where N is the highest completed)."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, max_rounds=3)
        _write_round_manifest(
            run_dir,
            0,
            round_exit_code=30,
            producer_outcome="committed",
            producer_ending_sha="1" * 40,
        )
        _write_round_manifest(
            run_dir,
            1,
            snapshot_sha="1" * 40,
            round_exit_code=30,
            producer_outcome="committed",
            producer_ending_sha="2" * 40,
        )
        _write_loop_manifest(run_dir, final_exit_code=40)
        # The walk finds NO incomplete round in {0, 1} but max_rounds=3
        # and completed_rounds=[0, 1] → resumed_round = 2.
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 2
        assert plan.completed_rounds == [0, 1]
        # Expected SHA = round-1 producer's ending SHA.
        assert plan.expected_sha == "2" * 40

    def test_plan_resume_all_rounds_complete_at_cap_raises_degenerate(self, tmp_path: Path):
        """All max_rounds rounds done cleanly AND the run is at the
        cap → degenerate. plan_resume refuses with an explanatory
        message; the operator's next move is to inspect manually,
        not silently resume."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, max_rounds=2)
        _write_round_manifest(
            run_dir,
            0,
            round_exit_code=30,
            producer_outcome="committed",
            producer_ending_sha="1" * 40,
        )
        _write_round_manifest(
            run_dir,
            1,
            snapshot_sha="1" * 40,
            round_exit_code=30,
            producer_outcome="committed",
            producer_ending_sha="2" * 40,
        )
        _write_loop_manifest(run_dir, final_exit_code=40)
        with pytest.raises(ResumeError, match="degenerate"):
            plan_resume(tmp_path, run_dir)

    def test_plan_resume_raises_when_run_init_missing(self, tmp_path: Path):
        run_dir = tmp_path / "no-init"
        run_dir.mkdir()
        with pytest.raises(ResumeError, match=RUN_INIT_FILENAME):
            plan_resume(tmp_path, run_dir)

    def test_plan_resume_returns_resume_plan_dataclass(self, tmp_path: Path):
        """Pin the dataclass shape so a future field rename doesn't
        silently break downstream callers."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(
            run_dir,
            starting_sha="0" * 40,
            operator_branch="feature/x",
            max_rounds=3,
            syncade_version="0.1.0",
            pr_doc_path="path/to/pr.md",
        )
        plan = plan_resume(tmp_path, run_dir)
        assert isinstance(plan, ResumePlan)
        assert plan.run_id == "2026-05-28T10-00-00"
        assert plan.run_dir == run_dir
        assert plan.operator_branch == "feature/x"
        assert plan.expected_branch == "feature/x"
        assert plan.syncade_version == "0.1.0"
        assert plan.config_snapshot_path == run_dir / RUN_INIT_FILENAME

    def test_plan_resume_refuses_resume_past_shipped_round(self, tmp_path: Path):
        """A SHIPped round (round_exit_code == 0 == SUCCESS) terminates the
        run. Such a run is only 'resumable' when the loop crashed after the
        round persisted but before the exit-0 loop-manifest was written.
        Resuming would re-review the un-advanced tree and could flip the
        legit SHIP to NO-SHIP and re-advance the branch — refuse, the
        operator's next move is a fresh run."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, max_rounds=3)
        # Round 0 SHIPped: reviewers + synth clean, no producer, exit 0.
        # No loop-manifest (interrupted after the SHIP round persisted).
        _write_round_manifest(run_dir, 0, round_exit_code=0)
        with pytest.raises(ResumeError, match="SHIP"):
            plan_resume(tmp_path, run_dir)

    def test_plan_resume_refuses_malformed_starting_sha(self, tmp_path: Path):
        """A malformed starting_sha in run-init.json is caught on read with
        a clear ResumeError naming the field — not surfaced later as a
        downstream TreeDriftError when the real HEAD fails to match it.
        Mirrors the live-git HEAD validation in resume_load."""
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-28T10-00-00"
        _write_run_init(run_dir, starting_sha="not-a-real-sha", max_rounds=3)
        with pytest.raises(ResumeError, match="starting_sha"):
            plan_resume(tmp_path, run_dir)
