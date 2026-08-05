"""Pre-dispatch early-exit results for the known-empty and fail-closed diff cases.

Split from :mod:`.round` to keep that module under the LOC cap.

D1/D3 (PR-h-02d): when ``snapshot.base_oid`` is set but the reviewer-facing diff
is empty (nothing changed, or all changed files were legitimate repo-context),
terminate before any subprocess is dispatched.

D2 (PR-h-02d): when ``filter_diff_for_reviewer`` dropped sections fail-closed
(headers that cannot be parsed or decoded), refuse the run (exit 60) rather than
treating "unreadable change" as "no change".
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from syncade.config import SyncadeConfig
from syncade.dispatcher import DispatchResult
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.logging import Logger
from syncade.persistence import persist_round_manifest, persist_run_summary
from syncade.snapshot import Snapshot

from .results import RoundArtifacts, RoundResult


def _no_changes_to_review_result(
    *,
    round_idx: int,
    snapshot: Snapshot,
    round_dir: Path,
    config: SyncadeConfig,
    started_at: datetime,
    resumed_under_drift: bool,
) -> RoundResult:
    """Terminal result for a run whose reviewer-facing diff is empty (D1/D3, PR-h-02d).

    ``snapshot.base_oid`` is set (a real base resolved) but the diff filtered to
    empty — either no files changed, or every section was legitimate repo-context
    stripped by the filter. Zero subprocesses are dispatched.

    The round exit code is ``SUCCESS``, NOT a private sentinel. An earlier cut used
    a sentinel ``1`` so the loop could distinguish this from a real SHIP, but ``1``
    is not in ``_EXIT_CODE_LABELS`` — so the durable artifacts rendered the round as
    ``Exit code: 1 (UNKNOWN)`` and ``ERROR (exit 1)`` while the top-level API
    correctly reported exit 0. The distinction is carried by
    ``termination_reason="no_changes_to_review"``, which is what the renderers key
    on; the exit code does not need to encode it twice.
    """
    empty_dispatch = DispatchResult(results=[], total_duration_seconds=0.0)
    persist_round_manifest(
        round_dir,
        snapshot,
        empty_dispatch,
        SUCCESS,
        started_at,
        None,
        None,
        None,
        round_idx=round_idx,
        producer_result=None,
        producer_provider=config.producer.provider,
        producer_model=config.producer.model,
        check_results=[],
    )
    persist_run_summary(
        round_dir,
        snapshot,
        empty_dispatch,
        SUCCESS,
        started_at,
        None,
        None,
        None,
        resumed_under_drift=resumed_under_drift,
        check_results=[],
        no_changes_to_review=True,
    )
    artifacts = RoundArtifacts(
        round_idx=round_idx,
        round_dir=round_dir,
        manifest_path=round_dir / "manifest.json",
        summary_path=round_dir / "summary.md",
    )
    return RoundResult(
        round_idx=round_idx,
        snapshot=snapshot,
        dispatch_result=empty_dispatch,
        synth_result=None,
        test_result=None,
        test_skip_reason=None,
        test_worktree_error=None,
        producer_result=None,
        round_exit_code=SUCCESS,
        artifacts=artifacts,
        check_results=[],
        no_changes_to_review=True,
    )


def _fail_closed_refusal_result(
    *,
    round_idx: int,
    snapshot: Snapshot,
    round_dir: Path,
    config: SyncadeConfig,
    started_at: datetime,
    resumed_under_drift: bool,
    unidentifiable: list[str],
    logger: Logger,
) -> RoundResult:
    """Refusal result for a diff with sections that cannot be identified (D2, PR-h-02d).

    ``filter_diff_for_reviewer`` dropped one or more sections fail-closed because their
    headers could not be parsed or decoded. Treating such a diff as "nothing to review"
    would ship a change BECAUSE we could not read it — the exact false-SHIP path
    PR-h-01 closed, reintroduced through a different door. Exit 60 (WORKTREE_ERROR)
    names the dropped headers and refuses before any subprocess is dispatched.
    """
    headers = "\n  ".join(unidentifiable)
    logger.event(
        f"refusing run: {len(unidentifiable)} diff section(s) had unidentifiable headers "
        f"(fail-closed per D2 — cannot determine whether they are repo-context or real change); "
        f"unidentifiable headers:\n  {headers}",
        error=True,
    )
    # Write a durable named artifact so the refusal cause is preserved after the
    # run completes. manifest.json also carries diff_filter_refusal_headers for
    # machine-readable access.
    refused_path = round_dir / "diff-refused.txt"
    refused_path.write_text(
        f"Syncade refused this run (exit 60, diff_malformed).\n\n"
        f"{len(unidentifiable)} diff section(s) could not be identified "
        f"(D2, PR-h-02d):\n\n"
        + "\n".join(f"  {h}" for h in unidentifiable)
        + "\n\nSee summary.md for next steps.\n",
        encoding="utf-8",
    )
    empty_dispatch = DispatchResult(results=[], total_duration_seconds=0.0)
    persist_round_manifest(
        round_dir,
        snapshot,
        empty_dispatch,
        WORKTREE_ERROR,
        started_at,
        None,
        None,
        None,
        round_idx=round_idx,
        producer_result=None,
        producer_provider=config.producer.provider,
        producer_model=config.producer.model,
        check_results=[],
        diff_filter_refusal_headers=unidentifiable,
    )
    persist_run_summary(
        round_dir,
        snapshot,
        empty_dispatch,
        WORKTREE_ERROR,
        started_at,
        None,
        None,
        None,
        resumed_under_drift=resumed_under_drift,
        check_results=[],
        fail_closed_headers=unidentifiable,
    )
    artifacts = RoundArtifacts(
        round_idx=round_idx,
        round_dir=round_dir,
        manifest_path=round_dir / "manifest.json",
        summary_path=round_dir / "summary.md",
    )
    return RoundResult(
        round_idx=round_idx,
        snapshot=snapshot,
        dispatch_result=empty_dispatch,
        synth_result=None,
        test_result=None,
        test_skip_reason=None,
        test_worktree_error=None,
        producer_result=None,
        round_exit_code=WORKTREE_ERROR,
        artifacts=artifacts,
        check_results=[],
        fail_closed_headers=unidentifiable,
    )
