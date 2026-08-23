"""Tests for :mod:`syncade.persistence` loop-summary (part 2 of 2).

GIANT-CLASS SPLIT: ``TestPersistLoopSummary`` is split across
``test_loop_summary.py`` (part 1) and this file (part 2) by test-method
group. Both halves carry a verbatim copy of the per-class helper
``_ship_round_result`` (it has no external dependency, so duplicating it
is cheaper than promoting it to ``_helpers``). Same class name in both
files is intentional — pytest collects them independently.

Moved verbatim from the former ``tests/test_persistence.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutput
from syncade.snapshot import Snapshot
from syncade.synthesis import SynthesizerOutput
from tests.persistence._helpers import _FIXED_STARTED_AT


class TestPersistLoopSummary:
    """PR-8: ``persist_loop_summary`` writes
    ``<run_dir>/loop-summary.md`` aggregating all rounds + final
    verdict + commit series + next-steps."""

    def _ship_round_result(self, round_idx: int = 0):
        """Build a minimal RoundResult that SHIPped."""
        from syncade.orchestrator import RoundArtifacts, RoundResult
        from syncade.synthesizer import SynthesizerResult

        round_dir = Path(f"round-{round_idx}")
        synth_output = SynthesizerOutput(
            consolidated_findings=[],
            synthesis_summary="clean",
        )
        synth_result = SynthesizerResult(output=synth_output, error=None, duration_seconds=5.0)
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=ReviewerOutput(
                        verdict="SHIP",
                        findings=[],
                        summary="ok",
                        priority_order=[],
                        coverage_gaps=[],
                        dismissed_concerns=[],
                    ),
                    error=None,
                    duration_seconds=2.0,
                ),
            ],
            total_duration_seconds=2.0,
        )
        snapshot = Snapshot(
            repo_root=Path("/tmp/test"),
            commit_sha="a" * 40,
            branch="main",
            base_ref=None,
            diff_text="",
            dirty_state="clean",
        )
        return RoundResult(
            round_idx=round_idx,
            snapshot=snapshot,
            dispatch_result=dispatch,
            synth_result=synth_result,
            test_result=None,
            test_skip_reason="test_command_unset",
            test_worktree_error=None,
            producer_result=None,
            round_exit_code=0,
            artifacts=RoundArtifacts(
                round_idx=round_idx,
                round_dir=round_dir,
                manifest_path=round_dir / "manifest.json",
                summary_path=round_dir / "summary.md",
            ),
        )

    def test_commit_series_includes_producer_subject_when_repo_root_supplied(self, tmp_path):
        """R1.T2: when repo_root is passed and the producer
        commit is reachable, the loop-summary commit series
        renders ``<sha> — round N producer ("<subject>")``.

        Build a real git repo with one commit; that commit's
        SHA + subject serve as the producer's commit. Pass
        repo_root in; assert the rendered subject is in the
        loop-summary."""
        import shutil
        import subprocess

        from syncade.adapters.producer import ProducerOutput
        from syncade.orchestrator import RoundArtifacts, RoundResult
        from syncade.persistence import persist_loop_summary
        from syncade.producer import ProducerResult

        if shutil.which("git") is None:
            pytest.skip("git not on PATH")

        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in (
            ["init", "-q"],
            ["config", "user.email", "t@e.com"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", *cmd], cwd=repo, check=True)
        (repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fix: handle null pointer in foo"],
            cwd=repo,
            check=True,
        )
        starting_sha = "a" * 40
        ending_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        r0 = self._ship_round_result(0)
        producer = ProducerResult(
            outcome="committed",
            starting_sha=starting_sha,
            ending_sha=ending_sha,
            duration_seconds=20.0,
            output=ProducerOutput(narrative_text="ok"),
            error=None,
        )
        r0_with_producer = RoundResult(
            round_idx=0,
            snapshot=r0.snapshot,
            dispatch_result=r0.dispatch_result,
            synth_result=r0.synth_result,
            test_result=r0.test_result,
            test_skip_reason=r0.test_skip_reason,
            test_worktree_error=r0.test_worktree_error,
            producer_result=producer,
            round_exit_code=30,
            artifacts=RoundArtifacts(
                round_idx=0,
                round_dir=Path("round-0"),
                manifest_path=Path("round-0/manifest.json"),
                summary_path=Path("round-0/summary.md"),
            ),
        )
        r1 = self._ship_round_result(1)

        run_dir = tmp_path / "run-with-subject"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=0,
            final_round=1,
            termination_reason="ship",
            rounds=[r0_with_producer, r1],
            max_rounds=2,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
            repo_root=repo,
        )
        text = path.read_text()
        # The commit-series line for round-0's producer should
        # include the commit subject in parens after "producer".
        assert 'round 0 producer ("fix: handle null pointer in foo")' in text

    def test_commit_series_falls_back_without_repo_root(self, tmp_path):
        """R1.T2 degradation: when repo_root is None, the subject
        lookup degrades gracefully — series renders the SHA + role
        without the parenthetical subject. This is the path the
        pre-R1.T2 implementation took unconditionally."""
        from syncade.adapters.producer import ProducerOutput
        from syncade.orchestrator import RoundArtifacts, RoundResult
        from syncade.persistence import persist_loop_summary
        from syncade.producer import ProducerResult

        r0 = self._ship_round_result(0)
        producer = ProducerResult(
            outcome="committed",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=20.0,
            output=ProducerOutput(narrative_text="ok"),
            error=None,
        )
        r0_with_producer = RoundResult(
            round_idx=0,
            snapshot=r0.snapshot,
            dispatch_result=r0.dispatch_result,
            synth_result=r0.synth_result,
            test_result=r0.test_result,
            test_skip_reason=r0.test_skip_reason,
            test_worktree_error=r0.test_worktree_error,
            producer_result=producer,
            round_exit_code=30,
            artifacts=RoundArtifacts(
                round_idx=0,
                round_dir=Path("round-0"),
                manifest_path=Path("round-0/manifest.json"),
                summary_path=Path("round-0/summary.md"),
            ),
        )
        r1 = self._ship_round_result(1)

        run_dir = tmp_path / "run-no-repo"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=0,
            final_round=1,
            termination_reason="ship",
            rounds=[r0_with_producer, r1],
            max_rounds=2,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
            repo_root=None,
        )
        text = path.read_text()
        # No parenthetical subject (no repo_root means no git log
        # lookup); plain "round 0 producer" line.
        assert "round 0 producer\n" in text
        assert 'round 0 producer ("' not in text

    def test_empty_commit_series_phrasing_branches_on_termination_reason(self, tmp_path):
        """R1.T3: when no producer commits landed, the empty-
        series wording branches on termination_reason so it
        matches what actually happened. Pre-R1.T3 was a static
        "round 0 shipped or no producer ran" that was jarring on
        failure paths."""
        from syncade.persistence import persist_loop_summary

        r0 = self._ship_round_result(0)
        run_dir = tmp_path / "run-cfg-err"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=50,
            final_round=0,
            termination_reason="config_error",
            rounds=[r0],
            max_rounds=2,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
        )
        text = path.read_text()
        # Config error path should NOT say "round 0 shipped".
        assert "round 0 shipped" not in text
        # Should explicitly name the config_error reason.
        assert "configuration is invalid" in text or "config" in text.lower()

    def test_empty_commit_series_ship_round_0(self, tmp_path):
        """R1.T3: when round 0 SHIPs (no producer ran because the
        loop terminated at round 0 success), the empty-series
        wording correctly says "round 0 shipped without needing a
        fix"."""
        from syncade.persistence import persist_loop_summary

        r0 = self._ship_round_result(0)
        run_dir = tmp_path / "run-ship-0"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=0,
            final_round=0,
            termination_reason="ship",
            rounds=[r0],
            max_rounds=3,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
        )
        text = path.read_text()
        assert "round 0 shipped without needing a fix" in text

    def test_loop_summary_carries_round_0_starting_sha_header(self, tmp_path):
        """PR-15: ``**Round 0 starting SHA:** \\`<short>\\` (full:
        \\`<long>\\`)`` appears in the headline block. The loop summary
        has multiple snapshot SHAs (one per round) so the headline
        gets the operator's pre-loop SHA — the question a re-reader is
        most likely to have. The commit-series section below lists
        the rest.

        Also pins the one-line lead-in immediately after the
        ``## Operator start and producer candidates`` header, since the
        headline SHA invites the operator to look there for the rest.
        """
        from syncade.persistence import persist_loop_summary

        r0 = self._ship_round_result(0)
        # _ship_round_result hardcodes commit_sha="a" * 40 — the
        # rendered short SHA is the first 12 chars.
        run_dir = tmp_path / "run-sha-header"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=0,
            final_round=0,
            termination_reason="ship",
            rounds=[r0],
            max_rounds=3,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
        )
        text = path.read_text()
        expected_short = "a" * 12
        expected_full = "a" * 40
        assert f"**Round 0 starting SHA:** `{expected_short}` (full: `{expected_full}`)" in text
        # Lead-in after the candidate-series header makes the link from
        # headline SHA → series explicit.
        series_idx = text.find("## Operator start and producer candidates")
        leadin_idx = text.find("Subsequent entries are producer candidate SHAs")
        assert series_idx != -1
        assert leadin_idx != -1
        assert series_idx < leadin_idx


class TestRoundVerdictLabel:
    """_round_verdict_label handles all exit codes including exit 10."""

    def _round_result(self, exit_code: int):
        from syncade.orchestrator import RoundArtifacts, RoundResult
        from tests.persistence._helpers import _snapshot

        round_dir = Path("round-0")
        dispatch = DispatchResult(results=[], total_duration_seconds=0.0)
        return RoundResult(
            round_idx=0,
            snapshot=_snapshot(),
            dispatch_result=dispatch,
            synth_result=None,
            test_result=None,
            test_skip_reason=None,
            test_worktree_error=None,
            producer_result=None,
            round_exit_code=exit_code,
            artifacts=RoundArtifacts(
                round_idx=0,
                round_dir=round_dir,
                manifest_path=round_dir / "manifest.json",
                summary_path=round_dir / "summary.md",
            ),
        )

    def test_exit_0_is_ship(self):
        from syncade.persistence.loop_summary_text import _round_verdict_label

        assert _round_verdict_label(self._round_result(0)) == "SHIP"

    def test_exit_30_is_no_ship(self):
        from syncade.persistence.loop_summary_text import _round_verdict_label

        assert _round_verdict_label(self._round_result(30)) == "NO-SHIP"

    def test_exit_10_is_decision_needed_not_error(self):
        """exit 10 is a judgment checkpoint, not an error — must render
        'DECISION NEEDED', not 'ERROR (exit 10)'."""
        from syncade.persistence.loop_summary_text import _round_verdict_label

        label = _round_verdict_label(self._round_result(10))
        assert label == "DECISION NEEDED"
        assert "ERROR" not in label


class TestDecisionNeededLoopSummaryText:
    """The decision_needed next-steps in loop-summary.md must not make
    unconditional branch-advance claims."""

    def test_decision_needed_next_steps_does_not_say_without_advancing(self, tmp_path):
        """The 'decision_needed' entry in _LOOP_NEXT_STEPS must not say
        'WITHOUT advancing any branch' — that claim is false when an earlier
        round already committed."""
        from syncade.persistence.loop_summary_text import _LOOP_NEXT_STEPS

        text = _LOOP_NEXT_STEPS.get("decision_needed", "")
        assert "WITHOUT advancing any branch" not in text, (
            "_LOOP_NEXT_STEPS['decision_needed'] still contains the stale "
            "'WITHOUT advancing any branch' claim"
        )
