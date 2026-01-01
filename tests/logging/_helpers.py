"""Shared helpers for the :mod:`syncade.logging` test package.

The Logger has no global state and writes via plain ``print`` — so
every test constructs its own Logger and captures output through
pytest's ``capsys``. No subprocesses, no filesystem.
"""

from __future__ import annotations

from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutput
from syncade.orchestrator import RunArtifacts, RunResult
from syncade.snapshot import Snapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(*, dirty: bool = False) -> Snapshot:
    # Keep the boolean kwarg on this helper for call-site stability and map
    # True to the tracked dirty state.
    return Snapshot(
        repo_root=Path("/tmp/repo"),
        commit_sha="abcdef0123456789" + "0" * 24,
        branch="main",
        base_ref=None,
        diff_text="",
        dirty_state="tracked" if dirty else "clean",
    )


def _ship_result(name: str = "rv1", provider: str = "anthropic") -> ReviewerRunResult:
    return ReviewerRunResult(
        reviewer_name=name,
        provider=provider,
        output=ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="logging test SHIP",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        ),
        error=None,
        duration_seconds=12.4,
    )


def _failed_result(name: str = "rv2", provider: str = "openai") -> ReviewerRunResult:
    return ReviewerRunResult(
        reviewer_name=name,
        provider=provider,
        output=None,
        error=RuntimeError("boom"),
        duration_seconds=3.1,
    )


def _run_result(
    *results: ReviewerRunResult,
    exit_code: int = 0,
    synth_result=None,
    findings_md_path: Path | None = None,
    synthesizer_paths=None,
) -> RunResult:
    run_dir = Path("/tmp/repo/.syncade/runs/2026-05-14T10-00-00")
    round_dir = run_dir / "round-0"
    return RunResult(
        artifacts=RunArtifacts(
            run_dir=run_dir,
            round_dir=round_dir,
            manifest_path=round_dir / "manifest.json",
            summary_path=round_dir / "summary.md",
            findings_md_path=findings_md_path,
            synthesizer_paths=synthesizer_paths,
        ),
        snapshot=_snapshot(),
        dispatch_result=DispatchResult(results=list(results), total_duration_seconds=15.0),
        exit_code=exit_code,
        synth_result=synth_result,
    )


def _synth_success_result(active_blockers: int = 0, dismissed: int = 0):
    """Build a SynthesizerResult representing a successful synth run.

    Used by the Logger.summary tests that exercise the findings.md
    pointer line on the synth-success path.
    """
    from syncade.synthesis import (
        ConsolidatedFinding,
        FindingProvenance,
        SynthesizerOutput,
    )
    from syncade.synthesizer import SynthesizerResult

    findings: list = []
    # active blockers first
    for i in range(active_blockers):
        findings.append(
            ConsolidatedFinding(
                description=f"active blocker {i}",
                file=None,
                severity="blocker",
                provenance=[
                    FindingProvenance(
                        reviewer_name="rv1",
                        original_severity="blocker",
                        original_index=i,
                        original_description=f"blocker {i}",
                    )
                ],
            )
        )
    # dismissed entries
    for i in range(dismissed):
        findings.append(
            ConsolidatedFinding(
                description=f"dismissed {i}",
                file=None,
                severity="minor",
                provenance=[
                    FindingProvenance(
                        reviewer_name="rv1",
                        original_severity="minor",
                        original_index=100 + i,
                        original_description=f"dismissed {i}",
                    )
                ],
                dismissed=True,
                dismissal_rationale="spec exempts this",
            )
        )
    return SynthesizerResult(
        output=SynthesizerOutput(
            consolidated_findings=findings,
            synthesis_summary="logging test synth output",
        ),
        error=None,
        duration_seconds=10.0,
        raw_subprocess_result=None,
    )


def _synth_parse_failure_result():
    """SynthesizerResult representing a SynthesizerOutputError parse
    failure — used by the Logger.summary tests that exercise the
    synth-pointer-line behavior (PR-7 fix #4)."""
    from syncade.synthesis import SynthesizerOutputError
    from syncade.synthesizer import SynthesizerResult

    return SynthesizerResult(
        output=None,
        error=SynthesizerOutputError("synth output had no parseable JSON"),
        duration_seconds=8.5,
        raw_subprocess_result=None,
    )
