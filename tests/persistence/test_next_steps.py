"""Tests for :mod:`syncade.persistence` — Next-steps guidance splits.

Moved verbatim from the former ``tests/test_persistence.py``.
"""

from __future__ import annotations

from pathlib import Path

from syncade.adapters.base import ReviewerInvocationError
from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence import persist_run_summary
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _errored_test_result,
    _failed_test_result,
    _make_round_dir,
    _ship,
    _snapshot,
    _subprocess_result,
    _synth_output_empty,
    _synth_output_with_findings,
    _synth_result,
)


class TestNextStepsUpdated:
    """PR-7: Next-steps guidance points at findings.md (exit 0 / 30)
    and at synthesizer artifacts (exit 70 / 40 with synth failure)."""

    def _ship_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_exit_zero_next_steps_points_at_findings_md(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
        ).read_text()
        # Extract just the Next steps block
        ns_idx = text.find("## Next steps")
        assert ns_idx != -1
        ns_block = text[ns_idx:]
        assert "findings.md" in ns_block
        assert "summary.md" in ns_block

    def test_exit_30_next_steps_points_at_findings_md_first(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
        ).read_text()
        ns_block = text[text.find("## Next steps") :]
        # findings.md is the FIRST artifact mentioned in exit-30 guidance
        # because it lists the active blockers
        first_md = ns_block.find("findings.md")
        first_summary = ns_block.find("summary.md")
        assert first_md != -1
        # summary.md appears too, but findings.md comes first
        if first_summary != -1:
            assert first_md < first_summary

    def test_exit_70_reviewer_phase_omits_synth_files(self, tmp_path: Path):
        """R2.4: exit 70 now splits by phase. When synth was skipped
        (synth_result=None → reviewer phase parse-failed), the
        Next-steps block should NOT point at synthesizer.* files —
        they don't exist on this path. The previous one-block text
        mentioned both phases, sending the operator on a wild goose
        chase for non-existent files."""
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=70,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,  # synth skipped — reviewer parse failed
        ).read_text()
        ns_block = text[text.find("## Next steps") :]
        # Reviewer-variant content
        assert "<reviewer-name>.stdout" in ns_block
        assert "<reviewer-name>.error.txt" in ns_block
        # Synth files MUST NOT appear (they don't exist on this path)
        assert "synthesizer.stdout" not in ns_block
        assert "synthesizer.error.txt" not in ns_block
        # And the variant explicitly notes the synth was skipped
        # (text wraps across lines so check both halves)
        assert "synthesizer phase was" in ns_block
        assert "skipped" in ns_block.lower()

    def test_exit_70_synth_phase_omits_reviewer_files(self, tmp_path: Path):
        """R2.4: when synth ran and parse-failed
        (synth_result.error is a SynthesizerOutputError), the
        Next-steps block should point at synthesizer.* FIRST and
        NOT at <reviewer-name>.* generically (those reviewers all
        succeeded). Adds the new common-shapes list (ghost
        reviewer, out-of-range original_index from P0.2's cross-
        input validation)."""
        from syncade.synthesis import SynthesizerOutputError

        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=None, error=SynthesizerOutputError("unparseable"))
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=70,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
        ).read_text()
        ns_block = text[text.find("## Next steps") :]
        # Synth-variant lead-in
        lower = ns_block.lower()
        assert "synthesizer's output didn't parse" in lower
        # Synth artifacts named
        assert "synthesizer.stdout" in ns_block
        assert "synthesizer.error.txt" in ns_block
        # R2.4: common-shapes list expanded to include the P0.2
        # provenance-validation failure modes
        assert "ghost reviewer" in lower
        assert "out-of-range" in lower
        # Should NOT point at "<reviewer-name>.stdout" — those
        # reviewers all succeeded on this path
        assert "<reviewer-name>.stdout" not in ns_block
        assert "<reviewer-name>.error.txt" not in ns_block


class TestExit40NextStepsSplitByPhase:
    """QA fix #10 (P1.5): exit 40 has two meaningful subcases —
    reviewer-failed (synth skipped) vs synth-failed (every reviewer
    succeeded). The Next-steps content should route the operator
    to the right ``.error.txt`` first.
    """

    def _ship_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def _failed_reviewer_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=None,
                    error=ReviewerInvocationError(
                        "auth failed",
                        returncode=1,
                        stdout="",
                        stderr="",
                    ),
                    duration_seconds=0.5,
                    raw_subprocess_result=_subprocess_result(rc=1),
                ),
            ],
            total_duration_seconds=0.5,
        )

    def test_exit_40_reviewer_phase_failure_points_at_reviewer_files_first(self, tmp_path: Path):
        """When the reviewer phase failed (synth_result is None),
        the first instruction names per-reviewer artifacts. The
        synth-skipped phase doesn't get linked because
        ``synthesizer.*`` doesn't exist on this path."""
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._failed_reviewer_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,  # synth skipped — reviewer phase failed
        ).read_text()
        ns_block = text[text.find("## Next steps") :]
        # First-mention check: ".error.txt" and ".stderr" appear in
        # the reviewer-failure variant; the synth variant doesn't
        # name those generically.
        assert "reviewer" in ns_block.lower()
        assert ".error.txt" in ns_block
        assert ".stderr" in ns_block
        # The synth variant's lead-in phrasing must NOT appear here
        # — that's the wrong-phase signal.
        assert "synthesizer subprocess failed" not in ns_block.lower()
        # And the variant explicitly mentions the synth-was-skipped
        # state so the operator doesn't go looking for synth files.
        assert "synthesizer phase was skipped" in ns_block.lower()

    def test_exit_40_synth_phase_failure_points_at_synthesizer_files_first(self, tmp_path: Path):
        """When every reviewer succeeded and the synth subprocess
        failed, the first instruction names ``synthesizer.error.txt`` /
        ``synthesizer.stderr`` — not per-reviewer files (those are
        clean on this path)."""
        from syncade.synthesis import SynthesizerOutputError

        # Simulate a synth subprocess failure (not a parse failure).
        # ReviewerInvocationError is the codex-subprocess-died
        # bucket per PR-7's decision table.
        synth_failure = _synth_result(
            error=ReviewerInvocationError(
                "codex auth failed",
                returncode=1,
                stdout="",
                stderr="codex: 401 unauthorized",
            ),
            output=None,
        )
        # Ensure we aren't accidentally exercising parse-failure
        # path (SynthesizerOutputError) — that maps to 70, not 40.
        assert not isinstance(synth_failure.error, SynthesizerOutputError)

        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),  # both reviewers SHIPPED clean
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth_failure,
        ).read_text()
        ns_block = text[text.find("## Next steps") :]
        # The synth-variant lead-in phrase appears.
        assert "synthesizer subprocess failed" in ns_block.lower()
        # First-mention check: synthesizer.error.txt appears BEFORE
        # any per-reviewer .error.txt would.
        synth_idx = ns_block.find("synthesizer.error.txt")
        assert synth_idx != -1
        # The variant explicitly notes that per-reviewer .error.txt
        # files do not exist for this exit code. The text wraps
        # across multiple lines so check the two halves both appear.
        assert "per-reviewer `.error.txt`" in ns_block
        assert "do not exist" in ns_block
        # And the reviewer-variant's lead-in phrasing must NOT
        # appear here — that's the wrong-phase signal.
        assert "reviewer subprocess failed" not in ns_block.lower()


class TestNextStepsTestLegSplits:
    """PR-7.5: exit 30 splits between synth-blocker and test-failed;
    exit 40 splits between reviewer-failed / synth-failed /
    test-subprocess-errored. The skip rules for the latter two were
    in PR-7 fix #10; this class adds the test-failed branches."""

    def _ship_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_exit_30_with_test_failed_routes_to_test_failed_variant(self, tmp_path: Path):
        """Synth-clean + test-failed → exit 30 from the test leg.
        Next-steps points at ``test-run.stdout`` FIRST, not
        findings.md (the synth is clean)."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=_failed_test_result(),
        ).read_text()
        ns = text[text.find("## Next steps") :]
        # Test-failed variant signals
        assert "test re-run leg reported failures" in ns.lower()
        assert "test-run.stdout" in ns
        # The synth-blocker variant's lead-in phrase must NOT fire
        # here — the test-failed variant has its own distinct
        # "test re-run leg reported failures" lead-in.
        assert "lists the active blockers" not in ns.lower()

    def test_exit_30_with_synth_blocker_routes_to_synth_blocker_variant(self, tmp_path: Path):
        """Synth surfaced an active blocker, no test leg → original
        PR-7 exit-30 next-steps points at findings.md FIRST."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=None,
        ).read_text()
        ns = text[text.find("## Next steps") :]
        # Synth-blocker variant signals
        assert "findings.md" in ns.lower()
        assert "active blockers" in ns.lower()
        # The test-failed variant must NOT fire here
        assert "the independent test re-run leg reported failures" not in ns.lower()

    def test_exit_40_with_test_subprocess_error_routes_to_test_variant(self, tmp_path: Path):
        """Synth-clean + test-subprocess-error → exit 40 from the
        test leg's subprocess. Next-steps names ``test-run.stderr``
        + the test_run.error_type in manifest.json, not synth or
        reviewer artifacts (those succeeded)."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=_errored_test_result(),
        ).read_text()
        ns = text[text.find("## Next steps") :]
        assert "test re-run leg" in ns.lower()
        assert "test-run.stderr" in ns
        assert "test_run.error_type" in ns
        # Synth- and reviewer-variant signals must not appear
        assert "synthesizer subprocess failed" not in ns.lower()
        assert "a reviewer subprocess failed" not in ns.lower()


class TestProducerNextStepsCandidateLocation:
    """Per-round next-steps for committed+non-imported outcomes must derive
    location from candidate_import.recovery_ref, not assume preserved standalone."""

    def test_committed_error_without_recovery_ref_says_preserved_standalone(self):
        """When trusted import returned error WITHOUT a recovery_ref, the candidate
        is only in the preserved standalone repository — the pre-existing behavior."""
        from syncade.adapters.producer import ProducerOutput
        from syncade.persistence.run_summary_next_steps import _resolve_next_steps_with_producer
        from syncade.producer import ProducerResult
        from syncade.producer_import import CandidateImportResult

        producer = ProducerResult(
            outcome="committed",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=5.0,
            output=ProducerOutput(narrative_text="fix applied"),
            error=None,
            candidate_import=CandidateImportResult(
                status="error",
                recovery_ref=None,
                error="trusted import failed",
            ),
        )
        text = _resolve_next_steps_with_producer(exit_code=60, producer_result=producer)
        assert "preserved standalone" in text
        assert "NOT the only copy" not in text

    def test_committed_error_with_recovery_ref_says_anchored_not_preserved_standalone(self):
        """When trusted import returned error WITH a recovery_ref, the candidate IS
        in the operator repository; the standalone workspace was deleted.

        Before the fix, the per-round next-steps said 'preserved standalone repository'
        regardless of recovery_ref, pointing the operator to a deleted workspace.
        """
        from syncade.adapters.producer import ProducerOutput
        from syncade.persistence.run_summary_next_steps import _resolve_next_steps_with_producer
        from syncade.producer import ProducerResult
        from syncade.producer_import import CandidateImportResult

        recovery = "refs/syncade/recovery/run-1/round-0/" + "b" * 40
        producer = ProducerResult(
            outcome="committed",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=5.0,
            output=ProducerOutput(narrative_text="fix applied"),
            error=None,
            candidate_import=CandidateImportResult(
                status="error",
                recovery_ref=recovery,
                error="quarantine cleanup failed",
            ),
        )
        text = _resolve_next_steps_with_producer(exit_code=60, producer_result=producer)
        assert "preserved standalone" not in text, (
            "must not send operator to a workspace that was deleted"
        )
        assert recovery in text, "must name the ref the operator can actually read"
        assert "NOT the only copy" in text or "not preserved" in text
