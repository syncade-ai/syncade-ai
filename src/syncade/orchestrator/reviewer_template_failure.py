"""Artifact builder for reviewer-template render failures."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from syncade.config import SyncadeConfig
from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.exit_codes import REVIEWER_FAILURE
from syncade.persistence import persist_round_manifest, persist_run_summary
from syncade.process import SubprocessError
from syncade.snapshot import Snapshot

from .results import RoundArtifacts, RoundResult


def _reviewer_template_failure_result(
    *,
    round_idx: int,
    snapshot: Snapshot,
    round_dir: Path,
    config: SyncadeConfig,
    started_at: datetime,
    resumed_under_drift: bool,
    error: Exception,
    filtered_diff_bytes: int | None = None,
    raw_diff_bytes: int | None = None,
) -> RoundResult:
    """Build a phase-failure result for reviewer-template load/render errors."""
    message = f"reviewer template render failed: {error}"
    dispatch_result = DispatchResult(
        results=[
            ReviewerRunResult(
                reviewer_name=reviewer.name,
                provider=reviewer.provider,
                model=reviewer.model,
                output=None,
                error=SubprocessError(message),
                duration_seconds=0.0,
                raw_subprocess_result=None,
            )
            for reviewer in config.reviewers
        ],
        total_duration_seconds=0.0,
    )
    persist_round_manifest(
        round_dir,
        snapshot,
        dispatch_result,
        REVIEWER_FAILURE,
        started_at,
        None,
        None,
        None,
        round_idx=round_idx,
        producer_result=None,
        producer_provider=config.producer.provider,
        producer_model=config.producer.model,
        check_results=[],
        filtered_diff_bytes=filtered_diff_bytes,
        raw_diff_bytes=raw_diff_bytes,
    )
    persist_run_summary(
        round_dir,
        snapshot,
        dispatch_result,
        REVIEWER_FAILURE,
        started_at,
        None,
        None,
        None,
        resumed_under_drift=resumed_under_drift,
        check_results=[],
    )
    artifacts = RoundArtifacts(
        round_idx=round_idx,
        round_dir=round_dir,
        manifest_path=round_dir / "manifest.json",
        summary_path=round_dir / "summary.md",
        findings_md_path=None,
        synthesizer_paths=None,
        test_run_paths=None,
        producer_paths=None,
    )
    return RoundResult(
        round_idx=round_idx,
        snapshot=snapshot,
        dispatch_result=dispatch_result,
        synth_result=None,
        test_result=None,
        test_skip_reason=None,
        test_worktree_error=None,
        producer_result=None,
        round_exit_code=REVIEWER_FAILURE,
        artifacts=artifacts,
        check_results=[],
        filtered_diff_bytes=filtered_diff_bytes,
        raw_diff_bytes=raw_diff_bytes,
    )
