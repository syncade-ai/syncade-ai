"""Tests for :mod:`syncade.orchestrator.resume` (PR-16 T3 + PR-22 T4).

Covers ``check_tree_drift`` (real git repos on ``tmp_path``) and the
producer-escalation resume path (``read_resume_decision`` + escalation
eligibility).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.orchestrator.resume import (
    ResumeError,
    TreeDriftError,
    check_tree_drift,
    find_resumable_runs,
    plan_resume,
)
from syncade.process import run_subprocess
from tests.resume._helpers import (
    _write_loop_manifest,
    _write_round_manifest,
    _write_run_init,
)


def _init_repo(repo_root: Path, *, branch: str = "main") -> str:
    """Initialize a real git repo at ``repo_root`` and return the
    initial commit SHA. Uses a stub commit so the SHA is meaningful
    for the tree-drift comparisons."""
    repo_root.mkdir(parents=True, exist_ok=True)
    # Use the existing run_subprocess for the same timeout discipline
    # the production code uses.
    run_subprocess(["git", "init", "-q", "-b", branch], cwd=repo_root, timeout=10.0)
    run_subprocess(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_root,
        timeout=10.0,
    )
    run_subprocess(["git", "config", "user.name", "Test"], cwd=repo_root, timeout=10.0)
    (repo_root / "seed.txt").write_text("seed\n")
    run_subprocess(["git", "add", "seed.txt"], cwd=repo_root, timeout=10.0)
    run_subprocess(
        ["git", "commit", "-q", "-m", "seed"],
        cwd=repo_root,
        timeout=10.0,
    )
    result = run_subprocess(["git", "rev-parse", "HEAD"], cwd=repo_root, timeout=10.0)
    return result.stdout.strip()


class TestCheckTreeDrift:
    def test_check_tree_drift_no_drift_returns_none(self, tmp_path: Path):
        """Happy path: branch + SHA both match → returns None."""
        repo_root = tmp_path / "repo"
        sha = _init_repo(repo_root)
        # Should not raise.
        result = check_tree_drift(repo_root, expected_sha=sha, expected_branch="main")
        assert result is None

    def test_check_tree_drift_branch_mismatch_raises(self, tmp_path: Path):
        """Operator switched branches between abort and resume → raises
        TreeDriftError(kind='branch'). Branch check fires FIRST (before
        SHA), per the brief's 'Refusing on SHA alone misses the case
        where the operator switched branches and the new branch shares
        a SHA with the run's recorded SHA' rationale."""
        repo_root = tmp_path / "repo"
        sha = _init_repo(repo_root)
        # Create a new branch and check it out — both have the same SHA.
        run_subprocess(
            ["git", "checkout", "-q", "-b", "feature/x"],
            cwd=repo_root,
            timeout=10.0,
        )
        with pytest.raises(TreeDriftError) as excinfo:
            check_tree_drift(repo_root, expected_sha=sha, expected_branch="main")
        assert excinfo.value.kind == "branch"
        assert excinfo.value.expected == "main"
        assert excinfo.value.actual == "feature/x"
        # The message names both branches so the operator can see the drift.
        assert "main" in str(excinfo.value)
        assert "feature/x" in str(excinfo.value)

    def test_check_tree_drift_sha_mismatch_raises(self, tmp_path: Path):
        """Operator made a new commit on the same branch → raises
        TreeDriftError(kind='sha')."""
        repo_root = tmp_path / "repo"
        original_sha = _init_repo(repo_root)
        # Make a new commit.
        (repo_root / "other.txt").write_text("other\n")
        run_subprocess(["git", "add", "other.txt"], cwd=repo_root, timeout=10.0)
        run_subprocess(
            ["git", "commit", "-q", "-m", "second"],
            cwd=repo_root,
            timeout=10.0,
        )
        new_sha = run_subprocess(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, timeout=10.0
        ).stdout.strip()
        assert new_sha != original_sha
        with pytest.raises(TreeDriftError) as excinfo:
            check_tree_drift(
                repo_root,
                expected_sha=original_sha,
                expected_branch="main",
            )
        assert excinfo.value.kind == "sha"
        assert excinfo.value.expected == original_sha
        assert excinfo.value.actual == new_sha
        # Both SHAs in the message.
        assert original_sha in str(excinfo.value)
        assert new_sha in str(excinfo.value)

    def test_check_tree_drift_branch_takes_precedence_over_sha(self, tmp_path: Path):
        """If both branch and SHA mismatch, the branch error is
        raised (not the SHA error). The brief is explicit about this
        ordering: 'Branch check first, then SHA check'."""
        repo_root = tmp_path / "repo"
        _init_repo(repo_root)
        run_subprocess(
            ["git", "checkout", "-q", "-b", "feature/x"],
            cwd=repo_root,
            timeout=10.0,
        )
        # Make a new commit on feature/x so SHA also differs.
        (repo_root / "other.txt").write_text("other\n")
        run_subprocess(["git", "add", "other.txt"], cwd=repo_root, timeout=10.0)
        run_subprocess(
            ["git", "commit", "-q", "-m", "feature commit"],
            cwd=repo_root,
            timeout=10.0,
        )
        with pytest.raises(TreeDriftError) as excinfo:
            check_tree_drift(
                repo_root,
                expected_sha="0" * 40,  # deliberately wrong
                expected_branch="main",
            )
        # Branch error wins.
        assert excinfo.value.kind == "branch"

    def test_check_tree_drift_detached_head_both_sides_ok(self, tmp_path: Path):
        """Original run on detached HEAD (expected_branch=None) and
        operator still on detached HEAD → no drift on the branch
        check. SHA check still applies."""
        repo_root = tmp_path / "repo"
        sha = _init_repo(repo_root)
        # Detach HEAD.
        run_subprocess(["git", "checkout", "-q", "--detach"], cwd=repo_root, timeout=10.0)
        # Should not raise.
        result = check_tree_drift(repo_root, expected_sha=sha, expected_branch=None)
        assert result is None

    def test_check_tree_drift_branch_to_detached_raises(self, tmp_path: Path):
        """Operator was on main, detached HEAD now → branch drift."""
        repo_root = tmp_path / "repo"
        sha = _init_repo(repo_root)
        run_subprocess(["git", "checkout", "-q", "--detach"], cwd=repo_root, timeout=10.0)
        with pytest.raises(TreeDriftError) as excinfo:
            check_tree_drift(repo_root, expected_sha=sha, expected_branch="main")
        assert excinfo.value.kind == "branch"
        assert excinfo.value.expected == "main"
        assert excinfo.value.actual == "(detached HEAD)"


# ---------------------------------------------------------------------------
# PR-22: producer escalation → resume-with-decision
# ---------------------------------------------------------------------------


class TestResumeEscalation:
    """PR-22 T4: a producer-escalation run (exit 10) is resume-eligible; the
    escalated round is the retry target; read_resume_decision reads
    decision.txt (and refuses when the escalated round recorded none)."""

    def test_exit_10_decision_needed_is_resumable(self, tmp_path: Path):
        runs_root = tmp_path / "runs"
        run_dir = runs_root / "2026-05-30T00-00-00"
        _write_run_init(run_dir)
        _write_round_manifest(run_dir, 0, round_exit_code=30, producer_outcome="escalated")
        _write_loop_manifest(run_dir, final_exit_code=10, termination_reason="decision_needed")
        assert run_dir.name in find_resumable_runs(runs_root)

    def test_plan_resume_retries_the_escalated_round(self, tmp_path: Path):
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        run_dir = runs_root / "2026-05-30T00-00-00"
        _write_run_init(run_dir, max_rounds=3)
        # round 0 committed; round 1 escalated → retry round 1 (NOT round 2).
        _write_round_manifest(
            run_dir,
            0,
            round_exit_code=30,
            producer_outcome="committed",
            producer_ending_sha="b" * 40,
        )
        _write_round_manifest(
            run_dir, 1, snapshot_sha="b" * 40, round_exit_code=30, producer_outcome="escalated"
        )
        _write_loop_manifest(
            run_dir, final_exit_code=10, final_round=1, termination_reason="decision_needed"
        )
        plan = plan_resume(tmp_path, run_dir)
        assert plan.resumed_round == 1
        assert plan.completed_rounds == [0]

    def test_read_resume_decision_returns_text(self, tmp_path: Path):
        from syncade.orchestrator.resume import read_resume_decision

        run_dir = tmp_path / "runs" / "r"
        _write_round_manifest(run_dir, 0, round_exit_code=30, producer_outcome="escalated")
        (run_dir / "decision.txt").write_text("Chose option A: omit empty checks.\n")
        assert read_resume_decision(run_dir, 0) == "Chose option A: omit empty checks."

    def test_read_resume_decision_refuses_without_decision(self, tmp_path: Path):
        from syncade.orchestrator.resume import read_resume_decision

        run_dir = tmp_path / "runs" / "r"
        _write_round_manifest(run_dir, 0, round_exit_code=30, producer_outcome="escalated")
        # No decision.txt → refuse with a helpful message.
        with pytest.raises(ResumeError, match="decision"):
            read_resume_decision(run_dir, 0)

    def test_read_resume_decision_none_for_non_escalated(self, tmp_path: Path):
        from syncade.orchestrator.resume import read_resume_decision

        run_dir = tmp_path / "runs" / "r"
        _write_round_manifest(run_dir, 0, round_exit_code=30, producer_outcome="stalled")
        assert read_resume_decision(run_dir, 0) is None
