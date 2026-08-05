"""Per-round manifest.json persistence.

Writes ``<round_dir>/manifest.json`` — the round-level entry point for
tooling that wants to know what happened without reading every
reviewer's output. The schema includes reviewer, synthesizer, test,
producer, and check sections, with ``round_exit_code`` aligned to
``loop-manifest.json``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from syncade import __version__
from syncade.dispatcher import DispatchResult
from syncade.producer import ProducerResult
from syncade.snapshot import Snapshot
from syncade.synthesizer import SynthesizerResult
from syncade.test_runner import TestRunResult

from ._atomic import atomic_write_json
from .checks import _check_manifest_entry
from .producer import _producer_manifest_entry
from .reviewer import _reviewer_manifest_entry
from .synth import _synthesizer_manifest_entry
from .test_run import _test_run_manifest_entry


def persist_round_manifest(
    round_dir: Path,
    snapshot: Snapshot,
    dispatch_result: DispatchResult,
    exit_code: int,
    started_at: datetime,
    synth_result: SynthesizerResult | None = None,
    test_result: TestRunResult | None = None,
    test_skip_reason: str | None = None,
    *,
    round_idx: int = 0,
    producer_result: ProducerResult | None = None,
    producer_provider: str | None = None,
    producer_model: str | None = None,
    check_results: list[TestRunResult] | None = None,
    diff_filter_refusal_headers: list[str] | None = None,
) -> Path:
    """Write ``<round_dir>/manifest.json`` summarizing the round.

    The manifest is the round-level entry point for tooling that wants
    to know what happened without reading every reviewer's output.
    The loop and skill bridge read it to surface findings counts and round
    status to the user.

    Schema (matches PRD Appendix C usage):

    .. code-block:: json

       {
         "syncade_version": "0.X.0",
         "run_id": "2026-05-12T15-30-04",
         "round": 0,
         "started_at_utc": "2026-05-12T15:30:04Z",
         "snapshot": {
           "commit_sha": "...",
           "branch": "main",
           "base_ref": null,
           "base_oid": null,
           "diff_present": false
         },
         "reviewers": [
           {
             "name": "claude-reviewer",
             "provider": "anthropic",
             "verdict": "SHIP",
             "finding_count": 0,
             "duration_seconds": 12.4,
             "outcome": "success",
             "error_type": null
           }
         ],
         "synthesizer": {
           "outcome": "success",
           "stdout_path": "synthesizer.stdout",
           "stderr_path": "synthesizer.stderr",
           "parsed_path": "synthesizer.parsed.json",
           "error_path": null,
           "duration_seconds": 12.4,
           "dismissed_count": 1,
           "active_blocker_count": 0,
           "active_minor_count": 2,
           "active_nit_count": 1
         },
         "test_run": {
           "outcome": "passed",
           "exit_code": 0,
           "command": "pytest -q",
           "duration_seconds": 8.3,
           "stdout_path": "test-run.stdout",
           "stderr_path": "test-run.stderr",
           "exit_code_path": "test-run.exit-code.txt",
           "error_type": null
         },
         "test_skip_reason": null,
         "producer": null,
         "retried": 0,
         "round_exit_code": 0
       }

    The ``synthesizer`` section is ``null`` when the phase was
    skipped (any reviewer failed). On synthesizer success the
    consolidation counts populate. On synthesizer failure the
    counts are ``null`` and ``error_path`` points at the
    ``synthesizer.error.txt`` artifact.

    The ``test_run`` section is ``null`` when the leg was skipped
    (``[loop] test_command`` is not configured OR a prior phase
    failed/produced blockers). When the leg ran, it carries the
    ``outcome``, the operator-configured ``command`` echoed back
    verbatim, and pointers to the three artifact files. On
    ``subprocess_error``, ``error_type`` names the failure shape
    (``SubprocessTimeoutError``, ``SubprocessNotFoundError``, or
    other :class:`~syncade.process.SubprocessError` subclasses).

    ``started_at`` is the run-start instant captured once by the
    orchestrator and shared with :func:`persist_run_summary`, so the
    two files agree on when the run *began* — not when each file
    happened to be written (a long run finishes minutes after it
    starts).

    Returns the path of the written manifest.
    """
    if not round_dir.is_dir():
        raise FileNotFoundError(f"round_dir does not exist: {round_dir}")

    # Run id is the parent directory's name (orchestrator's layout
    # convention). Decoupled here rather than passed in, so a future
    # caller that reorganizes the run hierarchy doesn't have to remember
    # to update this argument list — they only have to keep the
    # parent.name convention.
    run_id = round_dir.parent.name

    manifest = {
        "syncade_version": __version__,
        "run_id": run_id,
        # ``round`` is the per-round index (0-indexed). Top-level loop
        # aggregation lives in <run-id>/loop-manifest.json.
        "round": round_idx,
        "started_at_utc": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot": {
            "commit_sha": snapshot.commit_sha,
            "branch": snapshot.branch,
            "base_ref": snapshot.base_ref,
            "base_oid": snapshot.base_oid,
            "diff_present": bool(snapshot.diff_text),
        },
        "reviewers": [_reviewer_manifest_entry(r) for r in dispatch_result.results],
        "synthesizer": _synthesizer_manifest_entry(synth_result),
        "test_run": _test_run_manifest_entry(test_result),
        # persist the test_skip_reason alongside test_run.
        # Tooling that wants to know WHY the test leg didn't fire
        # would otherwise have to infer from dispatch + synth
        # state. Surfacing the explicit reason in the
        # machine-readable manifest closes the loop: the CLI,
        # the future update loop, and any external tooling all
        # get the same signal the Logger emitted at run time.
        # ``None`` when test_result is not None (the leg ran).
        "test_skip_reason": test_skip_reason if test_result is None else None,
        # per-round producer section. ``None`` when this round
        # didn't run a producer (the round that SHIPped, or the
        # final round under max-rounds-reached). Populated when the
        # producer ran with outcome / starting_sha / ending_sha /
        # provider + model echo.
        "producer": _producer_manifest_entry(
            producer_result,
            producer_config_provider=producer_provider,
            producer_config_model=producer_model,
        ),
        # Total EXTRA subprocess attempts consumed this round riding out
        # transient provider errors (H5). A 429/5xx/dropped-socket blip in a
        # reviewer, the synthesizer, OR the producer subprocess is retried with
        # jittered backoff instead of aborting the loop at exit 40; surfacing the
        # count makes a flaky run visible in the artifact. Sums the reviewer-dispatch
        # retries, the synthesizer's own (SynthesizerResult.retries), and the
        # producer's (ProducerResult.retries, PR-v2-22).
        "retried": sum(r.retries for r in dispatch_result.results)
        + (synth_result.retries if synth_result is not None else 0)
        + (producer_result.retries if producer_result is not None else 0),
        # Use ``round_exit_code`` for cross-surface consistency with
        # ``loop-manifest.json``'s ``rounds[].round_exit_code``.
        "round_exit_code": exit_code,
    }

    # append the checks array ONLY when checks ran, so a zero-config
    # round's manifest stays byte-identical (no 'checks' key at all).
    if check_results:
        manifest["checks"] = [_check_manifest_entry(c) for c in check_results]

    # present ONLY on a fail-closed diff-filter refusal (D2, PR-h-02d).
    # Named "diff_filter_refusal_headers" so tooling can distinguish
    # "reviewer worktree failed" from "diff section(s) were unidentifiable".
    if diff_filter_refusal_headers is not None:
        manifest["diff_filter_refusal_headers"] = diff_filter_refusal_headers

    manifest_path = round_dir / "manifest.json"
    atomic_write_json(manifest_path, manifest, sort_keys=False)
    return manifest_path
