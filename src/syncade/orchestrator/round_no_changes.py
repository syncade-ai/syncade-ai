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
    raw_diff_bytes = len((snapshot.diff_text or "").encode("utf-8"))
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
        filtered_diff_bytes=0,
        raw_diff_bytes=raw_diff_bytes,
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
        filtered_diff_bytes=0,
        raw_diff_bytes=raw_diff_bytes,
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
    return _persist_refusal(
        round_idx=round_idx,
        snapshot=snapshot,
        round_dir=round_dir,
        config=config,
        started_at=started_at,
        resumed_under_drift=resumed_under_drift,
        fail_closed_headers=unidentifiable,
        oversize_diff_bytes=None,
    )


def _persist_refusal(
    *,
    round_idx: int,
    snapshot: Snapshot,
    round_dir: Path,
    config: SyncadeConfig,
    started_at: datetime,
    resumed_under_drift: bool,
    fail_closed_headers: list[str] | None,
    oversize_diff_bytes: int | None,
    oversize_prompt_chars: int | None = None,
) -> RoundResult:
    """Shared tail for every before-dispatch refusal: exit 60, zero reviewers, artifacts.

    The three refusals differ only in WHY (which discriminator is set) — everything after
    that is identical, and duplicating it is how the three would drift apart.
    """
    if fail_closed_headers is not None:
        _refusal_reason = "diff_malformed"
    elif oversize_diff_bytes is not None:
        _refusal_reason = "diff_too_large"
    elif oversize_prompt_chars is not None:
        _refusal_reason = "prompt_too_large"
    else:
        _refusal_reason = None
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
        diff_filter_refusal_headers=fail_closed_headers,
        # Pass measured sizes when they are known (diff_too_large path).
        # The "only the path that measured it writes it" rule from PR-h-03.5 applies:
        # fail_closed_headers path has no measured bytes (we refuse before measuring),
        # so filtered_diff_bytes stays None there.
        filtered_diff_bytes=oversize_diff_bytes,
        raw_diff_bytes=(
            len((snapshot.diff_text or "").encode("utf-8"))
            if oversize_diff_bytes is not None
            else None
        ),
        oversize_prompt_chars=oversize_prompt_chars,
        refusal_reason=_refusal_reason,
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
        fail_closed_headers=fail_closed_headers,
        oversize_diff_bytes=oversize_diff_bytes,
        oversize_prompt_chars=oversize_prompt_chars,
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
        fail_closed_headers=fail_closed_headers,
        oversize_diff_bytes=oversize_diff_bytes,
        oversize_prompt_chars=oversize_prompt_chars,
        # Mirror the sizes passed to persist_round_manifest so persist_loop_manifest
        # (which reads from RoundResult, not from the on-disk manifest) also carries
        # them. Only populated on the diff_too_large path where they were measured.
        filtered_diff_bytes=oversize_diff_bytes,
        raw_diff_bytes=(
            len((snapshot.diff_text or "").encode("utf-8"))
            if oversize_diff_bytes is not None
            else None
        ),
    )


def _diff_too_large_result(
    *,
    round_idx: int,
    snapshot: Snapshot,
    round_dir: Path,
    config: SyncadeConfig,
    started_at: datetime,
    resumed_under_drift: bool,
    diff_bytes: int,
    logger: Logger,
) -> RoundResult:
    """Refusal for a reviewer-facing diff over ``[loop] max_diff_bytes`` (PR-h-02e D2).

    Refuses rather than truncating: a verdict on a deliberately partial diff is a verdict
    on the wrong code, which is the failure PR-h-02 exists to prevent. Refuses rather than
    warning-and-proceeding: that is today's behaviour with extra text.

    The point of refusing HERE is that the alternative is not "no cap" but an opaque
    provider error after the operator has already paid for the run — measured, `codex exec`
    returns `input_too_large` naming only a character count, and before PR-h-field-01 item 1 the
    same input produced a bare `[Errno 7] Argument list too long` with no cause at all.
    """
    cap = config.loop.max_diff_bytes
    over = diff_bytes - cap
    logger.event(
        f"refusing run: the reviewer-facing diff is {diff_bytes:,} bytes, over the "
        f"[loop] max_diff_bytes ceiling of {cap:,} by {over:,}. Narrow --base, split the "
        f"PR, or raise the ceiling. Refused before dispatching any reviewer, so nothing "
        f"was spent.",
        error=True,
    )
    (round_dir / "diff-refused.txt").write_text(
        f"Syncade refused this run (exit 60, diff_too_large).\n\n"
        f"Reviewer-facing diff: {diff_bytes:,} bytes\n"
        f"[loop] max_diff_bytes: {cap:,} bytes\n"
        f"Over by:              {over:,} bytes\n\n"
        f"This is measured AFTER repo-context stripping and binary elision — it is what a "
        f"reviewer would actually be handed.\n\n"
        f"Next steps: narrow --base to a smaller range, split the PR, or raise "
        f"[loop] max_diff_bytes in .syncade/config.toml.\n",
        encoding="utf-8",
    )
    return _persist_refusal(
        round_idx=round_idx,
        snapshot=snapshot,
        round_dir=round_dir,
        config=config,
        started_at=started_at,
        resumed_under_drift=resumed_under_drift,
        fail_closed_headers=None,
        oversize_diff_bytes=diff_bytes,
    )


# Provider character ceilings, measured live (PR-h-field-01 item 1). The codex ceiling
# (1,048,576 chars) is the tighter of the two known limits, so it governs cross-provider
# checks. A prompt below this is safe for both providers currently in use.
_CODEX_CHAR_CEILING = 1_048_576


def _assembled_prompt_too_large_result(
    *,
    round_idx: int,
    snapshot: Snapshot,
    round_dir: Path,
    config: SyncadeConfig,
    started_at: datetime,
    resumed_under_drift: bool,
    reviewer_name: str,
    prompt_chars: int,
    logger: Logger,
) -> RoundResult:
    """Refusal when an assembled reviewer prompt exceeds the provider's character ceiling.

    The diff-size cap ([loop] max_diff_bytes) gates on the reviewer-facing diff, but a
    large reviewer template or prior-round context can push the full assembled prompt over
    the provider limit while the diff alone stays under the cap. This refuses before
    dispatch so the operator gets syncade's actionable message rather than the provider's
    opaque `input_too_large` error after paying for the round.
    """
    over = prompt_chars - _CODEX_CHAR_CEILING
    logger.event(
        f"refusing run: the assembled prompt for reviewer {reviewer_name!r} is "
        f"{prompt_chars:,} chars, over the provider ceiling of {_CODEX_CHAR_CEILING:,} "
        f"by {over:,}. Trim the reviewer template or reduce prior-round context. "
        f"Refused before dispatching any reviewer, so nothing was spent.",
        error=True,
    )
    (round_dir / "diff-refused.txt").write_text(
        f"Syncade refused this run (exit 60, prompt_too_large).\n\n"
        f"Assembled prompt for {reviewer_name!r}: {prompt_chars:,} chars\n"
        f"Provider ceiling (codex):               {_CODEX_CHAR_CEILING:,} chars\n"
        f"Over by:                                {over:,} chars\n\n"
        f"The assembled prompt includes the diff, the reviewer template, and any "
        f"prior-round context. Reduce prompt size by narrowing --base, trimming the "
        f"reviewer template, or lowering [loop] max_diff_bytes so the diff contributes "
        f"fewer characters.\n",
        encoding="utf-8",
    )
    return _persist_refusal(
        round_idx=round_idx,
        snapshot=snapshot,
        round_dir=round_dir,
        config=config,
        started_at=started_at,
        resumed_under_drift=resumed_under_drift,
        fail_closed_headers=None,
        oversize_diff_bytes=None,
        oversize_prompt_chars=prompt_chars,
    )
