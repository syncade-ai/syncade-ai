"""Resume-test staging helper for the orchestrator test subdir.

Holds ``_prepare_aborted_run`` — used only by ``test_resume.py`` — split out
of ``_helpers.py`` so neither helper module exceeds the LOC cap. Called (not
injected); the leading underscore keeps pytest from collecting it as a test
module. Moved verbatim from the former ``tests/test_orchestrator.py`` via
``tests/orchestrator/_helpers.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _prepare_aborted_run(
    repo_root: Path,
    pr_doc_path: Path,
    *,
    run_id: str = "2026-05-28T09-00-00",
    completed_round_count: int = 1,
    max_rounds: int = 3,
    aborted_exit_code: int = 40,
    aborted_round_partial: bool = True,
    budget_tokens: int | None = None,
    budget_usd: float | None = None,
) -> tuple[Path, str]:
    """Stage an aborted-run fixture: write run-init.json + N completed
    round directories with realistic manifests + (optionally) a
    partial directory for the round-to-resume.

    Returns ``(run_dir, expected_round_n_starting_sha)`` so the
    test can pass the right SHA into ResumePlan / pin against the
    rehydrated state.
    """
    from datetime import UTC, datetime

    from syncade.adapters.producer import ProducerOutput
    from syncade.config import SyncadeConfig
    from syncade.dispatcher import DispatchResult, ReviewerRunResult
    from syncade.findings import ReviewerOutput
    from syncade.persistence import (
        persist_producer_result,
        persist_reviewer_result,
        persist_round_manifest,
        persist_run_init,
        persist_synthesizer_result,
    )
    from syncade.producer import ProducerResult
    from syncade.snapshot import Snapshot
    from syncade.synthesis import (
        ConsolidatedFinding,
        FindingProvenance,
        SynthesizerOutput,
    )
    from syncade.synthesizer import SynthesizerResult

    run_dir = repo_root / ".syncade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    repo_head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Record the ACTUAL current branch, not a hardcoded "main". The repo fixture leaves HEAD
    # on a feature branch (so the PR-v2-26 default-branch guard doesn't trip loop-mode
    # tests), and a run-init that claimed "main" would then read as branch drift at resume.
    repo_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    started_at = datetime(2026, 5, 28, 9, 0, 0, tzinfo=UTC)
    loop_config: dict = {"max_rounds": max_rounds}
    if budget_tokens is not None:
        loop_config["budget_tokens"] = budget_tokens
    if budget_usd is not None:
        loop_config["budget_usd"] = budget_usd
    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop=loop_config,
    )
    persist_run_init(
        run_dir,
        syncade_version="0.1.0",
        started_at=started_at,
        pr_doc_path=pr_doc_path,
        base_ref=None,
        starting_sha=repo_head_sha,
        operator_branch=repo_branch,
        max_rounds=max_rounds,
        config=config,
    )

    # The "current SHA" for round 0 is the operator's tree HEAD. For
    # round N>0, we'd need real commits — but the fixture only needs
    # the SHA strings to match what the manifest writes. We use the
    # real HEAD for round 0 and fabricated SHAs for round N>0 commits
    # (the rehydrated rounds don't have to point to real git objects;
    # they're only read for downstream rendering).
    current_sha = repo_head_sha
    for round_idx in range(completed_round_count):
        round_dir = run_dir / f"round-{round_idx}"
        round_dir.mkdir()
        snapshot = Snapshot(
            repo_root=repo_root,
            commit_sha=current_sha,
            branch=repo_branch,
            base_ref=None,
            diff_text="",
            dirty_state="clean",
        )
        reviewers = [
            ReviewerRunResult(
                reviewer_name="rv1",
                provider="fake1",
                output=ReviewerOutput(
                    verdict="NO-SHIP",
                    findings=[],
                    summary=f"round {round_idx} rv1 summary",
                    priority_order=[],
                    coverage_gaps=[],
                    dismissed_concerns=[],
                ),
                error=None,
                duration_seconds=1.0,
            ),
            ReviewerRunResult(
                reviewer_name="rv2",
                provider="fake2",
                output=ReviewerOutput(
                    verdict="NO-SHIP",
                    findings=[],
                    summary=f"round {round_idx} rv2 summary",
                    priority_order=[],
                    coverage_gaps=[],
                    dismissed_concerns=[],
                ),
                error=None,
                duration_seconds=1.0,
            ),
        ]
        dispatch = DispatchResult(results=reviewers, total_duration_seconds=2.0)
        for r in reviewers:
            persist_reviewer_result(round_dir, r, None)
        synth_output = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description=f"round {round_idx} blocker",
                    file="src/x.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="rv1",
                            original_severity="blocker",
                            original_index=0,
                            original_description=f"r{round_idx} rv1 desc",
                        ),
                    ],
                    dismissed=False,
                )
            ],
            synthesis_summary=f"round {round_idx} synth",
        )
        synth_result = SynthesizerResult(output=synth_output, error=None, duration_seconds=1.0)
        persist_synthesizer_result(round_dir, synth_result)
        # Producer committed → fabricated next SHA.
        next_sha = f"{round_idx + 1:02x}" + "f" * 38
        producer_result = ProducerResult(
            outcome="committed",
            starting_sha=current_sha,
            ending_sha=next_sha,
            duration_seconds=5.0,
            output=ProducerOutput(narrative_text=f"round {round_idx} producer"),
            error=None,
        )
        persist_producer_result(round_dir, producer_result)
        persist_round_manifest(
            round_dir,
            snapshot,
            dispatch,
            30,
            started_at,
            synth_result,
            None,
            None,
            round_idx=round_idx,
            producer_result=producer_result,
            producer_provider="anthropic",
            producer_model="claude-sonnet-4-6",
        )
        current_sha = next_sha

    # The round to be resumed: optionally drop a partial directory
    # to simulate "loop ctrl-c'd between persistence and loop
    # terminator".
    if aborted_round_partial:
        partial_round_dir = run_dir / f"round-{completed_round_count}"
        partial_round_dir.mkdir()
        (partial_round_dir / "rv1.stdout").write_text("partial output")

    # Optionally write a loop-manifest with the aborted exit code. 25 is the PR-v2-11 budget
    # stop (resume-eligible like the environment-failure codes).
    if aborted_exit_code in (25, 40, 60, 70):
        # We write a minimal loop-manifest directly (rather than via persist_loop_manifest,
        # which requires a full rounds[] list) so the fixture controls the schema. The
        # termination_reason label is not read by the resume path — eligibility keys on
        # final_exit_code — so a single placeholder is fine across the aborted codes.
        loop_manifest_path = run_dir / "loop-manifest.json"
        loop_manifest_path.write_text(
            json.dumps(
                {
                    "syncade_version": "0.1.0",
                    "run_id": run_id,
                    "started_at_utc": "2026-05-28T09:00:00Z",
                    "max_rounds": max_rounds,
                    "final_exit_code": aborted_exit_code,
                    "final_round": completed_round_count - 1,
                    "termination_reason": "reviewer_failure",
                    "rounds": [],
                }
            )
            + "\n"
        )

    return run_dir, current_sha
