"""Shared fixture-construction helpers for the resume unit tests.

Constructs ``<runs_root>/<run-id>/`` directory fixtures directly on
``tmp_path`` (no real syncade subprocess invocation) so the resume
helpers can be exercised in isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.orchestrator.resume import (
    LOOP_MANIFEST_FILENAME,
    ROUND_MANIFEST_FILENAME,
)
from syncade.persistence import RUN_INIT_FILENAME


def _write_run_init(
    run_dir: Path,
    *,
    starting_sha: str = "a" * 40,
    operator_branch: str | None = "main",
    max_rounds: int = 3,
    pr_doc_path: str = "path/to/pr.md",
    syncade_version: str = "0.1.0",
    base_ref: str | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "syncade_version": syncade_version,
        "started_at_utc": "2026-05-28T16:43:59Z",
        "pr_doc_path": pr_doc_path,
        "base_ref": base_ref,
        "starting_sha": starting_sha,
        "operator_branch": operator_branch,
        "max_rounds": max_rounds,
        "config_snapshot": {"loop": {"max_rounds": max_rounds}},
    }
    (run_dir / RUN_INIT_FILENAME).write_text(json.dumps(payload, indent=2) + "\n")


def _write_loop_manifest(
    run_dir: Path,
    *,
    final_exit_code: int,
    final_round: int = 0,
    termination_reason: str = "ship",
) -> None:
    payload = {
        "syncade_version": "0.1.0",
        "run_id": run_dir.name,
        "started_at_utc": "2026-05-28T16:43:59Z",
        "max_rounds": 3,
        "final_exit_code": final_exit_code,
        "final_round": final_round,
        "termination_reason": termination_reason,
        "rounds": [],
    }
    (run_dir / LOOP_MANIFEST_FILENAME).write_text(json.dumps(payload, indent=2) + "\n")


def _write_round_manifest(
    run_dir: Path,
    round_idx: int,
    *,
    snapshot_sha: str = "a" * 40,
    round_exit_code: int = 0,
    reviewers_succeeded: bool = True,
    synth_succeeded: bool | None = True,
    test_outcome: str | None = None,
    producer_outcome: str | None = None,
    producer_ending_sha: str | None = None,
) -> Path:
    round_dir = run_dir / f"round-{round_idx}"
    round_dir.mkdir(parents=True, exist_ok=True)
    reviewers = [
        {
            "name": "claude-reviewer",
            "provider": "anthropic",
            "verdict": "SHIP" if round_exit_code == 0 else "NO-SHIP",
            "finding_count": 0,
            "duration_seconds": 1.0,
            "outcome": "success" if reviewers_succeeded else "subprocess_error",
            "error_type": None if reviewers_succeeded else "SubprocessTimeoutError",
        }
    ]
    synth_block: dict | None
    if synth_succeeded is None:
        synth_block = None
    elif synth_succeeded:
        synth_block = {
            "outcome": "success",
            "stdout_path": "synthesizer.stdout",
            "stderr_path": "synthesizer.stderr",
            "parsed_path": "synthesizer.parsed.json",
            "error_path": None,
            "duration_seconds": 1.0,
            "dismissed_count": 0,
            "active_blocker_count": 0,
            "active_minor_count": 0,
            "active_nit_count": 0,
        }
    else:
        synth_block = {
            "outcome": "subprocess_error",
            "stdout_path": "synthesizer.stdout",
            "stderr_path": "synthesizer.stderr",
            "parsed_path": None,
            "error_path": "synthesizer.error.txt",
            "duration_seconds": 1.0,
            "dismissed_count": None,
            "active_blocker_count": None,
            "active_minor_count": None,
            "active_nit_count": None,
        }
    test_block: dict | None = None
    if test_outcome is not None:
        test_block = {
            "outcome": test_outcome,
            "exit_code": 0 if test_outcome == "passed" else 1,
            "command": "pytest -q",
            "duration_seconds": 1.0,
            "stdout_path": "test-run.stdout",
            "stderr_path": "test-run.stderr",
            "exit_code_path": "test-run.exit-code.txt",
            "error_type": None if test_outcome != "subprocess_error" else "SubprocessTimeoutError",
        }
    producer_block: dict | None = None
    if producer_outcome is not None:
        producer_block = {
            "outcome": producer_outcome,
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "starting_sha": snapshot_sha,
            "ending_sha": producer_ending_sha
            if producer_ending_sha is not None
            else (snapshot_sha if producer_outcome != "committed" else "b" * 40),
            "duration_seconds": 1.0,
            "stdout_path": "producer.stdout",
            "stderr_path": "producer.stderr",
            "commit_sha_path": "producer.commit.txt",
            "error_path": None,
            "error_type": None,
        }
    payload = {
        "syncade_version": "0.1.0",
        "run_id": run_dir.name,
        "round": round_idx,
        "started_at_utc": "2026-05-28T16:43:59Z",
        "snapshot": {
            "commit_sha": snapshot_sha,
            "branch": "main",
            "base_ref": None,
            "diff_present": False,
        },
        "reviewers": reviewers,
        "synthesizer": synth_block,
        "test_run": test_block,
        "test_skip_reason": None,
        "producer": producer_block,
        "round_exit_code": round_exit_code,
    }
    (round_dir / ROUND_MANIFEST_FILENAME).write_text(json.dumps(payload, indent=2) + "\n")
    return round_dir
