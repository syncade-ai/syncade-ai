"""Tests for :mod:`syncade.persistence` loop-summary writers (part 1 of 2).

Split verbatim from the former ``tests/test_persistence.py``
``TestPersistLoopSummary`` (a 524-LOC giant class). This half carries
the per-class helper ``_ship_round_result`` (a verbatim DUPLICATE also
lives in ``test_loop_summary_more.py`` — both halves need it) plus the
SHIP / max-rounds / check-driven loop-summary tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutput
from syncade.snapshot import Snapshot
from syncade.synthesis import SynthesizerOutput
from syncade.test_runner import TestRunResult
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

    def test_budget_section_uses_fresh_tally_not_rehydrated_rounds(self, tmp_path):
        """PR-v2-11 Finding 4: on a resumed budget abort, the Budget section reports the
        ENFORCEMENT tally (fresh resumed spend, threaded as ``budget_usages``), NOT a re-sum
        over ``rounds`` — which on ``--resume`` includes rehydrated original rounds and would
        report original+resume, disagreeing with the budget that actually tripped."""
        import dataclasses

        from syncade.persistence import persist_loop_summary
        from syncade.usage import Usage

        # A round whose reviewer usage sums to $9.00 — stands in for a rehydrated original round.
        r0 = self._ship_round_result(0)
        big = dataclasses.replace(
            r0.dispatch_result.results[0],
            usage=Usage(
                model="m",
                input_tokens=1000,
                output_tokens=0,
                cost_usd=9.00,
                cost_source="estimated",
                auth_mode="subscription",
            ),
        )
        r0 = dataclasses.replace(
            r0,
            dispatch_result=dataclasses.replace(r0.dispatch_result, results=[big]),
            round_exit_code=30,
        )
        # The FRESH resumed-run tally that actually tripped the $0.50 budget: $0.60.
        fresh = [
            Usage(
                model="m",
                input_tokens=100,
                output_tokens=0,
                cost_usd=0.60,
                cost_source="estimated",
                auth_mode="subscription",
            )
        ]

        run_dir = tmp_path / "run-budget-resume"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=25,
            final_round=0,
            termination_reason="budget_exceeded",
            rounds=[r0],
            max_rounds=3,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 7, 18, 15, 0, 0, tzinfo=UTC),
            budget_usd=0.50,
            budget_usages=fresh,
        )
        text = path.read_text()
        assert "## Budget" in text
        assert "$0.6000" in text  # the fresh resumed tally that tripped
        assert "9.0000" not in text  # NOT the rehydrated original round's $9.00

    def test_budget_section_names_which_ceiling_crossed(self):
        """PR-v2-11 Finding 5: with BOTH budgets configured, the Budget section names the
        ceiling that actually tripped (over_budget's authoritative answer) and marks it —
        never a generic/cost-centric message on a token-only crossing, and vice versa."""
        from syncade.persistence.loop_summary_text import _budget_section
        from syncade.usage import Usage

        usages = [
            Usage(model="m", input_tokens=500, output_tokens=0, cost_usd=0.10, auth_mode="api")
        ]
        tok = "\n".join(
            _budget_section(
                usages, budget_tokens=100, budget_usd=5.0, budget_ceiling="budget_tokens"
            )
        )
        assert "TOKEN tally crossed" in tok and "COST tally crossed" not in tok
        assert "reported no usage)  ← CROSSED" in tok  # the token line is marked
        assert "LOWER-BOUND tally)  ← CROSSED" not in tok  # the dollar line is NOT

        usd = "\n".join(
            _budget_section(usages, budget_tokens=100, budget_usd=5.0, budget_ceiling="budget_usd")
        )
        assert "COST tally crossed" in usd and "TOKEN tally crossed" not in usd
        assert "LOWER-BOUND tally)  ← CROSSED" in usd
        assert "reported no usage)  ← CROSSED" not in usd

    def test_check_subprocess_error_label_and_next_steps(self, tmp_path):
        """PR-22 blocker 1: a blocking-check subprocess error terminates with
        termination_reason=check_subprocess_error; loop-summary.md labels it as
        a check failure and points next-steps at the check — NOT a reviewer
        failure (the pre-PR-22 mislabel)."""
        from syncade.persistence import persist_loop_summary

        run_dir = tmp_path / "run-cse"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=40,
            final_round=0,
            termination_reason="check_subprocess_error",
            rounds=[self._ship_round_result(0)],
            max_rounds=1,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
        )
        text = path.read_text()
        assert "check" in text.lower()
        # Must NOT be mislabeled as a reviewer failure.
        assert "reviewer failure" not in text.lower()
        ns = text[text.find("## Next steps") :]
        assert "mechanical check" in ns.lower()

    def test_check_driven_no_ship_loop_summary_points_at_check(self, tmp_path):
        """PR-22 dogfood (codex blocker): when the final round's NO-SHIP was a
        synth-clean BLOCKING mechanical-check failure (not synth blockers),
        loop-summary.md next-steps point at the check — NOT the generic
        max_rounds_reached 'read active blockers' text."""
        import dataclasses

        from syncade.persistence import persist_loop_summary

        round0 = dataclasses.replace(
            self._ship_round_result(0),  # synth-clean round
            round_exit_code=30,
            check_results=[
                TestRunResult(
                    name="file-length",
                    severity="blocking",
                    exit_code=1,
                    outcome="failed",
                    duration_seconds=0.5,
                    stdout="x.py: 600 > 500\n",
                    stderr="",
                )
            ],
        )
        run_dir = tmp_path / "run-cd"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=30,
            final_round=0,
            termination_reason="max_rounds_reached",
            rounds=[round0],
            max_rounds=1,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
        )
        ns = path.read_text()[path.read_text().find("## Next steps") :]
        assert "mechanical check" in ns.lower()
        assert "active blockers" not in ns  # not the misleading synth-blocker text

    def test_ship_at_round_0(self, tmp_path):
        """Single-round SHIP: loop summary names SHIP + ship reason +
        commit series shows only the starting SHA."""
        from syncade.persistence import persist_loop_summary

        run_dir = tmp_path / "run-1"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=0,
            final_round=0,
            termination_reason="ship",
            rounds=[self._ship_round_result(0)],
            max_rounds=1,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
        )
        text = path.read_text()
        assert path.name == "loop-summary.md"
        assert "**Final verdict:** SHIP" in text
        assert "ship (round 0)" in text
        assert "Rounds executed:** 1 of 1" in text
        # No producer commits in the series (round 0 shipped)
        assert "no producer commits" in text.lower() or "round 0 shipped" in text.lower()

    def test_ship_round_1_includes_commit_series(self, tmp_path):
        """Multi-round SHIP at round 1: the commit series shows
        both the starting SHA AND round 0's producer commit."""
        from syncade.adapters.producer import ProducerOutput
        from syncade.orchestrator import RoundArtifacts, RoundResult
        from syncade.persistence import persist_loop_summary
        from syncade.producer import ProducerResult

        # Round 0: NO-SHIP, producer committed
        r0 = self._ship_round_result(0)
        # Build round 0 with a producer that committed.
        producer = ProducerResult(
            outcome="committed",
            starting_sha="a" * 40,
            ending_sha="c" * 40,
            duration_seconds=20.0,
            output=ProducerOutput(narrative_text="fixed it"),
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
        # Round 1: SHIP
        r1 = self._ship_round_result(1)

        run_dir = tmp_path / "run-2"
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
        )
        text = path.read_text()
        # Both round sections appear
        assert "## Round 0" in text
        assert "## Round 1" in text
        # Round 0's producer committed
        assert "committed `cccccccccccc`" in text
        # The commit series shows both the starting SHA and the
        # round-0 producer's commit
        assert "round 0 starting SHA" in text
        assert "round 0 producer" in text
        # Rounds executed
        assert "Rounds executed:** 2 of 2" in text

    def test_max_rounds_reached_termination(self, tmp_path):
        """Multi-round max-rounds termination → exit 20 +
        max_rounds_reached label."""
        from syncade.orchestrator import RoundResult
        from syncade.persistence import persist_loop_summary

        # Two rounds, both NO-SHIP
        r0 = self._ship_round_result(0)
        r0_no_ship = RoundResult(
            round_idx=0,
            snapshot=r0.snapshot,
            dispatch_result=r0.dispatch_result,
            synth_result=r0.synth_result,
            test_result=r0.test_result,
            test_skip_reason=r0.test_skip_reason,
            test_worktree_error=None,
            producer_result=None,
            round_exit_code=30,
            artifacts=r0.artifacts,
        )
        r1_no_ship = self._ship_round_result(1)
        r1_no_ship_v2 = RoundResult(
            round_idx=1,
            snapshot=r1_no_ship.snapshot,
            dispatch_result=r1_no_ship.dispatch_result,
            synth_result=r1_no_ship.synth_result,
            test_result=r1_no_ship.test_result,
            test_skip_reason=r1_no_ship.test_skip_reason,
            test_worktree_error=None,
            producer_result=None,
            round_exit_code=30,
            artifacts=r1_no_ship.artifacts,
        )

        run_dir = tmp_path / "run-mr"
        run_dir.mkdir()
        path = persist_loop_summary(
            run_dir,
            final_exit_code=20,
            final_round=1,
            termination_reason="max_rounds_reached",
            rounds=[r0_no_ship, r1_no_ship_v2],
            max_rounds=2,
            started_at=_FIXED_STARTED_AT,
            completed_at=datetime(2026, 5, 12, 15, 32, 10, tzinfo=UTC),
        )
        text = path.read_text()
        assert "**Final verdict:** NO-SHIP" in text
        assert "**Termination reason:** max rounds reached" in text
        assert "**Final exit code:** 20" in text
        assert "consider increasing" in text.lower() or "bumping" in text.lower()
