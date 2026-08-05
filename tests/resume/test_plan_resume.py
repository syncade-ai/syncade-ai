"""Tests for :func:`syncade.orchestrator.resume.plan_resume`.

Split out of ``test_find_resolve_plan.py`` when the round-1 producer's
added resume cases pushed that file past the 500-LOC gate. Same fixture
style: ``<runs_root>/<run-id>/`` directories built directly on
``tmp_path``, no real syncade subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.orchestrator.resume import (
    ResumeError,
    ResumePlan,
    plan_resume,
)
from syncade.persistence import RUN_INIT_FILENAME
from tests.resume._helpers import (
    _write_loop_manifest,
    _write_round_manifest,
    _write_run_init,
)


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
