"""Tests for :mod:`syncade.orchestrator.resume` (PR-16 T4).

The ``load_completed_round`` dehydrate/rehydrate round-trip pair plus
its corruption / missing-manifest contract pins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.orchestrator.resume import (
    ResumeError,
    load_completed_round,
)


class TestLoadCompletedRoundRoundTrip:
    """The dehydrate/rehydrate test pair the PR-16 brief asked for
    explicitly: 'load_completed_round is the riskiest helper in the
    PR. ... Write the dehydrate/rehydrate test pair FIRST, then
    implement.'

    Strategy: build a real ``RoundResult`` with synthetic data, run
    it through the PRODUCTION persistence writers (persist_reviewer_result,
    persist_synthesizer_result, persist_test_run_result,
    persist_producer_result, persist_round_manifest), then call
    load_completed_round on the resulting round_dir. The rehydrated
    RoundResult must agree with the original on every field that
    flows into downstream consumers (loop_summary, handoff, PR-14
    cross-round-context). Lossy fields (raw_subprocess_result on
    reviewer/synth, Snapshot.diff_text, Snapshot.dirty_state) are
    not pinned — they're documented as not recoverable.
    """

    def _make_realistic_round_result(self, round_dir: Path):
        """Build a NO-SHIP-with-producer-committed round (the most
        common rehydration case). Round 0: two reviewers SHIP, one
        active blocker from synth, producer committed."""
        from syncade.adapters.producer import ProducerOutput
        from syncade.dispatcher import DispatchResult, ReviewerRunResult
        from syncade.findings import Finding, ReviewerOutput
        from syncade.orchestrator import RoundArtifacts, RoundResult
        from syncade.process import SubprocessResult
        from syncade.producer import ProducerResult
        from syncade.snapshot import Snapshot
        from syncade.synthesis import (
            ConsolidatedFinding,
            FindingProvenance,
            SynthesizerOutput,
        )
        from syncade.synthesizer import SynthesizerResult
        from syncade.test_runner import TestRunResult
        from syncade.usage import Usage

        snapshot = Snapshot(
            repo_root=round_dir.parent.parent.parent.parent,
            commit_sha="a" * 40,
            branch="main",
            base_ref="HEAD~5",
            diff_text="--- a/foo.py\n+++ b/foo.py\n@@\n-old\n+new\n",
            dirty_state="clean",
        )
        reviewers = [
            ReviewerRunResult(
                reviewer_name="claude-reviewer",
                provider="anthropic",
                output=ReviewerOutput(
                    verdict="NO-SHIP",
                    findings=[
                        Finding(
                            severity="blocker",
                            file="src/foo.py",
                            spec_clause="G1",
                            finding="missing validation",
                        )
                    ],
                    summary="claude flagged blocker",
                    priority_order=[0],
                    coverage_gaps=[],
                    dismissed_concerns=[],
                ),
                error=None,
                duration_seconds=12.5,
                raw_subprocess_result=SubprocessResult(
                    returncode=0, stdout="{}", stderr="", duration_seconds=12.5
                ),
                usage=Usage("claude-opus-4-8", 1000, 200, cost_usd=0.05, cost_source="provider"),
            ),
            ReviewerRunResult(
                reviewer_name="codex-reviewer",
                provider="openai",
                output=ReviewerOutput(
                    verdict="NO-SHIP",
                    findings=[],
                    summary="codex flagged nothing",
                    priority_order=[],
                    coverage_gaps=["spec doesn't cover null inputs"],
                    dismissed_concerns=[],
                ),
                error=None,
                duration_seconds=15.0,
                raw_subprocess_result=SubprocessResult(
                    returncode=0, stdout="{}", stderr="", duration_seconds=15.0
                ),
            ),
        ]
        dispatch = DispatchResult(
            results=reviewers,
            total_duration_seconds=27.5,
        )
        synth_output = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="missing input validation in src/foo.py",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="claude-reviewer",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing validation",
                        )
                    ],
                    dismissed=False,
                )
            ],
            synthesis_summary="One blocker from claude alone",
        )
        synth_result = SynthesizerResult(
            output=synth_output,
            error=None,
            duration_seconds=7.2,
            raw_subprocess_result=SubprocessResult(
                returncode=0, stdout="{}", stderr="", duration_seconds=7.2
            ),
        )
        test_result = TestRunResult(
            exit_code=0,
            outcome="passed",
            duration_seconds=42.0,
            stdout="all good\n",
            stderr="",
            error=None,
            command="pytest -q",
        )
        producer_result = ProducerResult(
            outcome="committed",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=88.0,
            output=ProducerOutput(narrative_text="addressed the blocker"),
            error=None,
            raw_subprocess_result=SubprocessResult(
                returncode=0,
                stdout="committed\n",
                stderr="",
                duration_seconds=88.0,
            ),
        )
        artifacts = RoundArtifacts(
            round_idx=0,
            round_dir=round_dir,
            manifest_path=round_dir / "manifest.json",
            summary_path=round_dir / "summary.md",
            findings_md_path=round_dir / "findings.md",
        )
        return RoundResult(
            round_idx=0,
            snapshot=snapshot,
            dispatch_result=dispatch,
            synth_result=synth_result,
            test_result=test_result,
            test_skip_reason=None,
            test_worktree_error=None,
            producer_result=producer_result,
            round_exit_code=30,
            artifacts=artifacts,
        )

    def _persist_with_production_writers(self, round_dir: Path, round_result):
        """Use the actual production persistence layer to write the
        round to disk. This is the dehydration step the test pair
        pins."""
        from datetime import UTC, datetime

        from syncade.persistence import (
            persist_producer_result,
            persist_reviewer_result,
            persist_round_manifest,
            persist_synthesizer_result,
            persist_test_run_result,
        )

        started_at = datetime(2026, 5, 28, 17, 15, 23, tzinfo=UTC)
        for r in round_result.dispatch_result.results:
            persist_reviewer_result(round_dir, r, r.raw_subprocess_result)
        if round_result.synth_result is not None:
            persist_synthesizer_result(round_dir, round_result.synth_result)
        if round_result.test_result is not None:
            persist_test_run_result(round_dir, round_result.test_result)
        if round_result.producer_result is not None:
            persist_producer_result(round_dir, round_result.producer_result)
        persist_round_manifest(
            round_dir,
            round_result.snapshot,
            round_result.dispatch_result,
            round_result.round_exit_code,
            started_at,
            round_result.synth_result,
            round_result.test_result,
            round_result.test_skip_reason,
            round_idx=round_result.round_idx,
            producer_result=round_result.producer_result,
            producer_provider="anthropic",
            producer_model="claude-sonnet-4-6",
        )

    def test_round_trip_no_ship_with_producer_committed(self, tmp_path: Path):
        """Dehydrate → rehydrate a realistic NO-SHIP-with-producer-
        committed round. Every downstream-consumed field must
        round-trip equal."""
        # The run-dir layout matches the production convention so
        # load_completed_round's repo_root inference works.
        run_dir = tmp_path / ".syncade" / "runs" / "2026-05-28T10-00-00"
        round_dir = run_dir / "round-0"
        round_dir.mkdir(parents=True)
        original = self._make_realistic_round_result(round_dir)

        # Dehydrate via production writers.
        self._persist_with_production_writers(round_dir, original)

        # Rehydrate.
        rehydrated = load_completed_round(round_dir)
        assert rehydrated is not None

        # ---- Per-field round-trip pins ----
        # Snapshot: commit_sha + branch + base_ref must match.
        assert rehydrated.snapshot.commit_sha == original.snapshot.commit_sha
        assert rehydrated.snapshot.branch == original.snapshot.branch
        assert rehydrated.snapshot.base_ref == original.snapshot.base_ref
        manifest = json.loads((round_dir / "manifest.json").read_text(encoding="utf-8"))
        assert bool(rehydrated.snapshot.diff_text) == manifest["snapshot"]["diff_present"]

        # Round index + exit code.
        assert rehydrated.round_idx == original.round_idx
        assert rehydrated.round_exit_code == original.round_exit_code
        assert rehydrated.test_skip_reason == original.test_skip_reason

        # Dispatch result.
        assert len(rehydrated.dispatch_result.results) == len(original.dispatch_result.results)
        for orig_r, rehy_r in zip(
            original.dispatch_result.results,
            rehydrated.dispatch_result.results,
            strict=False,
        ):
            assert rehy_r.reviewer_name == orig_r.reviewer_name
            assert rehy_r.provider == orig_r.provider
            assert rehy_r.duration_seconds == orig_r.duration_seconds
            # ReviewerOutput round-trips fully.
            assert rehy_r.output is not None
            assert rehy_r.output.verdict == orig_r.output.verdict
            assert rehy_r.output.summary == orig_r.output.summary
            assert len(rehy_r.output.findings) == len(orig_r.output.findings)
            assert rehy_r.output.coverage_gaps == orig_r.output.coverage_gaps
            # raw_subprocess_result is documented as None on rehydration.
            assert rehy_r.raw_subprocess_result is None

        # Finding #2: persisted usage rehydrates, so a resumed run's loop-manifest +
        # metrics keep the prior round's spend (total_tokens survives; the
        # input/output split does not — see usage_from_fields).
        claude_r = rehydrated.dispatch_result.results[0]
        assert claude_r.usage is not None
        assert claude_r.usage.total_tokens == 1200  # 1000 + 200
        assert claude_r.usage.cost_usd == 0.05 and claude_r.usage.cost_source == "provider"

        assert rehydrated.dispatch_result.all_succeeded == original.dispatch_result.all_succeeded
        assert (
            rehydrated.dispatch_result.total_duration_seconds
            == original.dispatch_result.total_duration_seconds
        )

        # Synth result.
        assert rehydrated.synth_result is not None
        assert rehydrated.synth_result.output is not None
        assert rehydrated.synth_result.error is None
        assert rehydrated.synth_result.duration_seconds == original.synth_result.duration_seconds
        assert rehydrated.synth_result.output.synthesis_summary == (
            original.synth_result.output.synthesis_summary
        )
        assert len(rehydrated.synth_result.output.consolidated_findings) == len(
            original.synth_result.output.consolidated_findings
        )
        orig_finding = original.synth_result.output.consolidated_findings[0]
        rehy_finding = rehydrated.synth_result.output.consolidated_findings[0]
        assert rehy_finding.description == orig_finding.description
        assert rehy_finding.severity == orig_finding.severity
        assert rehy_finding.file == orig_finding.file
        assert rehy_finding.dismissed == orig_finding.dismissed
        assert len(rehy_finding.provenance) == len(orig_finding.provenance)

        # Test result.
        assert rehydrated.test_result is not None
        assert rehydrated.test_result.outcome == original.test_result.outcome
        assert rehydrated.test_result.exit_code == original.test_result.exit_code
        assert rehydrated.test_result.command == original.test_result.command
        assert rehydrated.test_result.duration_seconds == original.test_result.duration_seconds
        # stdout/stderr round-trip via the .stdout / .stderr files.
        assert rehydrated.test_result.stdout == original.test_result.stdout
        assert rehydrated.test_result.stderr == original.test_result.stderr

        # Producer result.
        assert rehydrated.producer_result is not None
        assert rehydrated.producer_result.outcome == original.producer_result.outcome
        assert rehydrated.producer_result.starting_sha == original.producer_result.starting_sha
        assert rehydrated.producer_result.ending_sha == original.producer_result.ending_sha
        assert (
            rehydrated.producer_result.duration_seconds == original.producer_result.duration_seconds
        )
        # narrative_text round-trips via producer.stdout (the persistence
        # writer puts narrative_text into producer.stdout, which our
        # rehydration reads back).
        assert (
            rehydrated.producer_result.output.narrative_text
            == original.producer_result.output.narrative_text
        )

        # Artifacts pin only the path shape (not on-disk contents).
        assert rehydrated.artifacts.round_idx == original.round_idx
        assert rehydrated.artifacts.round_dir == round_dir
        assert rehydrated.artifacts.manifest_path == round_dir / "manifest.json"

    def test_round_trip_no_producer_no_test_round(self, tmp_path: Path):
        """A simpler round shape: no test leg, no producer (e.g.
        SHIP at round 0 — but resume would never see this since
        SHIP terminates the loop. Still useful as a contract pin
        for the optional-blocks paths.)"""
        from syncade.dispatcher import DispatchResult, ReviewerRunResult
        from syncade.findings import ReviewerOutput
        from syncade.orchestrator import RoundArtifacts, RoundResult
        from syncade.snapshot import Snapshot
        from syncade.synthesis import SynthesizerOutput
        from syncade.synthesizer import SynthesizerResult

        run_dir = tmp_path / ".syncade" / "runs" / "2026-05-28T11-00-00"
        round_dir = run_dir / "round-0"
        round_dir.mkdir(parents=True)
        snapshot = Snapshot(
            repo_root=tmp_path,
            commit_sha="c" * 40,
            branch="main",
            base_ref=None,
            diff_text="",
            dirty_state="clean",
        )
        reviewers = [
            ReviewerRunResult(
                reviewer_name="rv1",
                provider="anthropic",
                output=ReviewerOutput(
                    verdict="SHIP",
                    findings=[],
                    summary="clean",
                    priority_order=[],
                    coverage_gaps=[],
                    dismissed_concerns=[],
                ),
                error=None,
                duration_seconds=5.0,
            )
        ]
        dispatch = DispatchResult(results=reviewers, total_duration_seconds=5.0)
        synth_result = SynthesizerResult(
            output=SynthesizerOutput(
                consolidated_findings=[],
                synthesis_summary="clean",
            ),
            error=None,
            duration_seconds=2.0,
        )
        original = RoundResult(
            round_idx=0,
            snapshot=snapshot,
            dispatch_result=dispatch,
            synth_result=synth_result,
            test_result=None,
            test_skip_reason="test_command_unset",
            test_worktree_error=None,
            producer_result=None,
            round_exit_code=0,
            artifacts=RoundArtifacts(
                round_idx=0,
                round_dir=round_dir,
                manifest_path=round_dir / "manifest.json",
                summary_path=round_dir / "summary.md",
            ),
        )
        self._persist_with_production_writers(round_dir, original)

        rehydrated = load_completed_round(round_dir)
        assert rehydrated is not None
        assert rehydrated.test_result is None
        assert rehydrated.producer_result is None
        # test_skip_reason round-trips because the round manifest
        # writes it when test_result is None.
        assert rehydrated.test_skip_reason == original.test_skip_reason
        assert rehydrated.round_exit_code == 0

    def test_load_completed_round_returns_none_when_manifest_missing(self, tmp_path: Path):
        """Missing manifest = incomplete round = None. Distinct from
        'manifest present but malformed' which raises."""
        round_dir = tmp_path / "round-0"
        round_dir.mkdir()
        assert load_completed_round(round_dir) is None

    def test_load_completed_round_raises_on_malformed_manifest(self, tmp_path: Path):
        """Manifest present but unparseable = caller bug or corrupt
        state. Surface explicitly, don't return None (which would
        silently trigger a drop+retry that loses good state)."""
        round_dir = tmp_path / "round-0"
        round_dir.mkdir()
        (round_dir / "manifest.json").write_text("not json {")
        with pytest.raises(ResumeError, match="malformed"):
            load_completed_round(round_dir)

    def test_load_completed_round_raises_when_parsed_json_missing(self, tmp_path: Path):
        """Manifest says reviewer succeeded but no .parsed.json on
        disk → corrupt state, raise."""
        round_dir = tmp_path / ".syncade" / "runs" / "2026-05-28T10-00-00" / "round-0"
        round_dir.mkdir(parents=True)
        (round_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "syncade_version": "0.1.0",
                    "run_id": "2026-05-28T10-00-00",
                    "round": 0,
                    "started_at_utc": "2026-05-28T10:00:00Z",
                    "snapshot": {
                        "commit_sha": "a" * 40,
                        "branch": "main",
                        "base_ref": None,
                        "diff_present": False,
                    },
                    "reviewers": [
                        {
                            "name": "claude-reviewer",
                            "provider": "anthropic",
                            "verdict": "SHIP",
                            "finding_count": 0,
                            "duration_seconds": 5.0,
                            "outcome": "success",
                            "error_type": None,
                        }
                    ],
                    "synthesizer": None,
                    "test_run": None,
                    "test_skip_reason": "reviewer_failed",
                    "producer": None,
                    "round_exit_code": 0,
                }
            )
        )
        # No claude-reviewer.parsed.json on disk.
        with pytest.raises(ResumeError, match="parsed.json"):
            load_completed_round(round_dir)

    def test_load_completed_round_wraps_malformed_reviewer_block(self, tmp_path: Path):
        round_dir = tmp_path / ".syncade" / "runs" / "2026-05-28T10-00-00" / "round-0"
        round_dir.mkdir(parents=True)
        (round_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "syncade_version": "0.1.0",
                    "run_id": "2026-05-28T10-00-00",
                    "round": 0,
                    "started_at_utc": "2026-05-28T10:00:00Z",
                    "snapshot": {
                        "commit_sha": "a" * 40,
                        "branch": "main",
                        "base_ref": None,
                        "diff_present": False,
                    },
                    "reviewers": [{"outcome": "success"}],
                    "synthesizer": None,
                    "test_run": None,
                    "test_skip_reason": None,
                    "producer": None,
                    "round_exit_code": 0,
                }
            )
        )

        with pytest.raises(ResumeError, match="reviewers"):
            load_completed_round(round_dir)

    def test_load_completed_round_rejects_non_string_reviewer_name(self, tmp_path: Path):
        round_dir = tmp_path / ".syncade" / "runs" / "2026-05-28T10-00-00" / "round-0"
        round_dir.mkdir(parents=True)
        (round_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "syncade_version": "0.1.0",
                    "run_id": "2026-05-28T10-00-00",
                    "round": 0,
                    "started_at_utc": "2026-05-28T10:00:00Z",
                    "snapshot": {
                        "commit_sha": "a" * 40,
                        "branch": "main",
                        "base_ref": None,
                        "diff_present": False,
                    },
                    "reviewers": [
                        {
                            "name": 123,
                            "provider": "anthropic",
                            "verdict": "SHIP",
                            "finding_count": 0,
                            "duration_seconds": 5.0,
                            "outcome": "success",
                            "error_type": None,
                        }
                    ],
                    "synthesizer": None,
                    "test_run": None,
                    "test_skip_reason": None,
                    "producer": None,
                    "round_exit_code": 0,
                }
            )
        )
        (round_dir / "123.parsed.json").write_text(
            json.dumps(
                {
                    "verdict": "SHIP",
                    "findings": [],
                    "summary": "clean",
                    "priority_order": [],
                    "coverage_gaps": [],
                    "dismissed_concerns": [],
                }
            )
        )

        with pytest.raises(ResumeError, match="reviewers.*name"):
            load_completed_round(round_dir)

    def test_load_completed_round_wraps_malformed_test_block(self, tmp_path: Path):
        round_dir = tmp_path / ".syncade" / "runs" / "2026-05-28T10-00-00" / "round-0"
        round_dir.mkdir(parents=True)
        (round_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "syncade_version": "0.1.0",
                    "run_id": "2026-05-28T10-00-00",
                    "round": 0,
                    "started_at_utc": "2026-05-28T10:00:00Z",
                    "snapshot": {
                        "commit_sha": "a" * 40,
                        "branch": "main",
                        "base_ref": None,
                        "diff_present": False,
                    },
                    "reviewers": [],
                    "synthesizer": None,
                    "test_run": {"exit_code": 0, "command": "pytest -q"},
                    "test_skip_reason": None,
                    "producer": None,
                    "round_exit_code": 0,
                }
            )
        )
        with pytest.raises(ResumeError, match="test_run"):
            load_completed_round(round_dir)


def test_resume_producer_retries_round_trip(tmp_path: Path):
    """Regression: load_completed_round must rehydrate producer.retries from
    manifest.json's producer.retried field. Previously the field was dropped,
    causing a resumed run to regenerate loop-manifest.json with producer.retried: 0
    even when the round manifest correctly recorded retries."""
    from datetime import UTC, datetime

    from syncade.adapters.producer import ProducerOutput
    from syncade.dispatcher import DispatchResult
    from syncade.orchestrator import RoundArtifacts, RoundResult
    from syncade.persistence import (
        persist_producer_result,
        persist_round_manifest,
        persist_synthesizer_result,
    )
    from syncade.producer_result import ProducerResult
    from syncade.snapshot import Snapshot
    from syncade.synthesis import SynthesizerOutput
    from syncade.synthesizer import SynthesizerResult

    run_dir = tmp_path / ".syncade" / "runs" / "2026-07-19T10-00-00"
    round_dir = run_dir / "round-0"
    round_dir.mkdir(parents=True)

    snapshot = Snapshot(
        repo_root=run_dir.parent.parent.parent.parent,
        commit_sha="a" * 40,
        branch="fix-branch",
        base_ref=None,
        diff_text="",
        dirty_state="clean",
    )
    dispatch = DispatchResult(results=[], total_duration_seconds=0.0)
    synth_output = SynthesizerOutput(
        consolidated_findings=[],
        synthesis_summary="one blocker",
    )
    synth_result = SynthesizerResult(
        output=synth_output,
        error=None,
        duration_seconds=5.0,
        raw_subprocess_result=None,
        usage=None,
        provider="openai",
        model="gpt-5.5",
    )
    producer_result = ProducerResult(
        outcome="committed",
        starting_sha="a" * 40,
        ending_sha="b" * 40,
        duration_seconds=30.0,
        output=ProducerOutput(narrative_text="fixed"),
        error=None,
        retries=2,
    )
    artifacts = RoundArtifacts(
        round_idx=0,
        round_dir=round_dir,
        manifest_path=round_dir / "manifest.json",
        summary_path=round_dir / "summary.md",
        findings_md_path=None,
    )
    round_result = RoundResult(
        round_idx=0,
        snapshot=snapshot,
        dispatch_result=dispatch,
        synth_result=synth_result,
        test_result=None,
        test_skip_reason=None,
        test_worktree_error=None,
        producer_result=producer_result,
        round_exit_code=30,
        artifacts=artifacts,
    )

    persist_producer_result(round_dir, round_result.producer_result)
    persist_synthesizer_result(round_dir, round_result.synth_result)
    persist_round_manifest(
        round_dir,
        round_result.snapshot,
        round_result.dispatch_result,
        round_result.round_exit_code,
        datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        round_result.synth_result,
        round_result.test_result,
        round_result.test_skip_reason,
        round_idx=round_result.round_idx,
        producer_result=round_result.producer_result,
        producer_provider="anthropic",
        producer_model="claude-sonnet-4-6",
    )

    rehydrated = load_completed_round(round_dir)
    assert rehydrated is not None
    assert rehydrated.producer_result is not None
    assert rehydrated.producer_result.retries == 2, (
        "resume rehydration must preserve producer.retries from manifest.json"
    )

    # Also verify no-retry round-trips as 0 (absence in manifest → 0).
    round_dir_0 = run_dir / "round-1"
    round_dir_0.mkdir()
    producer_no_retry = ProducerResult(
        outcome="committed",
        starting_sha="a" * 40,
        ending_sha="b" * 40,
        duration_seconds=10.0,
        output=ProducerOutput(narrative_text="clean"),
        error=None,
        retries=0,
    )
    persist_producer_result(round_dir_0, producer_no_retry)
    persist_synthesizer_result(round_dir_0, round_result.synth_result)
    persist_round_manifest(
        round_dir_0,
        round_result.snapshot,
        round_result.dispatch_result,
        0,
        datetime(2026, 7, 19, 10, 1, 0, tzinfo=UTC),
        round_result.synth_result,
        round_result.test_result,
        None,
        round_idx=1,
        producer_result=producer_no_retry,
        producer_provider="anthropic",
        producer_model="claude-sonnet-4-6",
    )
    rehydrated_0 = load_completed_round(round_dir_0)
    assert rehydrated_0 is not None
    assert rehydrated_0.producer_result is not None
    assert rehydrated_0.producer_result.retries == 0
