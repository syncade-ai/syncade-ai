"""Render helpers for the prose-writer drift sweep.

Each function invokes one prose persistence writer with minimal fixtures,
writes to a tmp_path sub-directory, and returns the rendered text for
item-numbering scanning. Kept in a separate module so test_authority_claim_drift
stays within the code-LOC gate while the sweep covers all writers.
"""

from __future__ import annotations

from pathlib import Path


def sweep_persist_loop_summary(d: Path) -> str:
    from datetime import UTC, datetime

    from syncade.persistence.loop_summary import persist_loop_summary

    _at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    return persist_loop_summary(
        d,
        final_exit_code=20,
        final_round=0,
        termination_reason="max_rounds_reached",
        rounds=[],
        max_rounds=1,
        started_at=_at,
        completed_at=_at,
    ).read_text(encoding="utf-8")


def sweep_persist_run_summary(d: Path) -> str:
    from datetime import UTC, datetime

    from syncade.dispatcher import DispatchResult
    from syncade.persistence import persist_run_summary
    from syncade.snapshot import Snapshot

    _at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    run_dir = d / "2026-08-01T00-00-00"
    round_dir = run_dir / "round-0"
    round_dir.mkdir(parents=True)
    snap = Snapshot(
        repo_root=d,
        commit_sha="a" * 40,
        branch="main",
        base_ref=None,
        diff_text="",
        dirty_state="clean",
    )
    return persist_run_summary(
        round_dir,
        snap,
        DispatchResult(results=[], total_duration_seconds=0.0),
        exit_code=30,
        started_at=_at,
    ).read_text(encoding="utf-8")


def sweep_persist_handoff(d: Path) -> str:
    from datetime import UTC, datetime

    from syncade.dispatcher import DispatchResult, ReviewerRunResult
    from syncade.findings import ReviewerOutput
    from syncade.orchestrator import RoundArtifacts, RoundResult
    from syncade.persistence import persist_handoff
    from syncade.snapshot import Snapshot
    from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput
    from syncade.synthesizer import SynthesizerResult

    _at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    d.mkdir(parents=True, exist_ok=True)
    blocker = ConsolidatedFinding(
        description="test finding for sweep",
        file="src/x.py",
        severity="blocker",
        provenance=[
            FindingProvenance(
                reviewer_name="rv1",
                original_severity="blocker",
                original_index=0,
                original_description="test finding for sweep",
            )
        ],
        dismissed=False,
        dismissal_rationale=None,
        severity_change_rationale=None,
    )
    synth = SynthesizerResult(
        output=SynthesizerOutput(consolidated_findings=[blocker], synthesis_summary="s"),
        error=None,
        duration_seconds=1.0,
    )
    snap = Snapshot(
        repo_root=d,
        commit_sha="a" * 40,
        branch="main",
        base_ref=None,
        diff_text="",
        dirty_state="clean",
    )
    dispatch = DispatchResult(
        results=[
            ReviewerRunResult(
                reviewer_name="rv1",
                provider="anthropic",
                output=ReviewerOutput(
                    verdict="NO-SHIP",
                    findings=[],
                    summary="s",
                    priority_order=[],
                    coverage_gaps=[],
                    dismissed_concerns=[],
                ),
                error=None,
                duration_seconds=1.0,
            )
        ],
        total_duration_seconds=1.0,
    )
    round_dir = d / "round-0"
    round_result = RoundResult(
        round_idx=0,
        snapshot=snap,
        dispatch_result=dispatch,
        synth_result=synth,
        test_result=None,
        test_skip_reason="test_command_unset",
        test_worktree_error=None,
        producer_result=None,
        round_exit_code=30,
        artifacts=RoundArtifacts(
            round_idx=0,
            round_dir=round_dir,
            manifest_path=round_dir / "manifest.json",
            summary_path=round_dir / "summary.md",
        ),
    )
    path = persist_handoff(
        d,
        final_exit_code=30,
        final_round=0,
        termination_reason="producer_stalled",
        rounds=[round_result],
        max_rounds=1,
    )
    assert path is not None, "persist_handoff returned None — sweep is vacuous"
    return path.read_text(encoding="utf-8")


def sweep_persist_decision_needed(d: Path) -> str:
    from syncade.persistence import persist_decision_needed
    from syncade.producer_escalation import ProducerEscalation

    d.mkdir(parents=True, exist_ok=True)
    esc = ProducerEscalation(
        finding_indices=[0],
        finding="test finding",
        decision="A or B?",
        options=["A", "B"],
        rationale="spec conflict",
    )
    return persist_decision_needed(
        d,
        round_idx=0,
        escalation=esc,
        run_id="2026-08-01T00-00-00",
    ).read_text(encoding="utf-8")


def sweep_persist_deactivated_blockers_decision_needed(d: Path) -> str:
    from syncade.persistence import persist_deactivated_blockers_decision_needed

    d.mkdir(parents=True, exist_ok=True)
    return persist_deactivated_blockers_decision_needed(
        d,
        round_idx=0,
        run_id="2026-08-01T00-00-00",
        deactivated=[("rv1", "test blocker text", "dismissed: rationale")],
    ).read_text(encoding="utf-8")


def sweep_persist_findings_md(d: Path) -> str:
    from datetime import UTC, datetime

    from syncade.persistence import persist_findings_md
    from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput
    from syncade.synthesizer import SynthesizerResult

    _at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    round_dir = d / "round-0"
    round_dir.mkdir(parents=True)
    finding = ConsolidatedFinding(
        description="test finding for findings.md sweep",
        file="src/x.py",
        severity="blocker",
        provenance=[
            FindingProvenance(
                reviewer_name="rv1",
                original_severity="blocker",
                original_index=0,
                original_description="test finding for findings.md sweep",
            )
        ],
        dismissed=False,
        dismissal_rationale=None,
        severity_change_rationale=None,
    )
    synth = SynthesizerResult(
        output=SynthesizerOutput(consolidated_findings=[finding], synthesis_summary="s"),
        error=None,
        duration_seconds=1.0,
    )
    return persist_findings_md(
        round_dir,
        synth,
        started_at=_at,
    ).read_text(encoding="utf-8")


def sweep_persist_current_findings_md(d: Path) -> str:
    from datetime import UTC, datetime

    from syncade.persistence import persist_current_findings_md, persist_findings_md
    from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput
    from syncade.synthesizer import SynthesizerResult

    _at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    round_dir = d / "round-0"
    round_dir.mkdir(parents=True)
    finding = ConsolidatedFinding(
        description="current findings sweep test",
        file="src/x.py",
        severity="blocker",
        provenance=[
            FindingProvenance(
                reviewer_name="rv1",
                original_severity="blocker",
                original_index=0,
                original_description="current findings sweep test",
            )
        ],
        dismissed=False,
        dismissal_rationale=None,
        severity_change_rationale=None,
    )
    synth = SynthesizerResult(
        output=SynthesizerOutput(consolidated_findings=[finding], synthesis_summary="s"),
        error=None,
        duration_seconds=1.0,
    )
    source = persist_findings_md(round_dir, synth, started_at=_at)
    path = persist_current_findings_md(d, source)
    assert path is not None, "persist_current_findings_md returned None — sweep is vacuous"
    return path.read_text(encoding="utf-8")


def sweep_persist_producer_result(d: Path) -> str:
    """Sweep EVERY authored artifact this writer can emit, not just the one that motivated it.

    `persist_producer_result` writes two operator-visible prose channels, and an earlier version
    of this helper rendered only the first: `producer.error.txt` (subprocess failure, including
    the authored moved-HEAD sentence) and `producer.import.error.txt` (trusted-import
    diagnostics). The per-round next-steps text directs operators to read the second by name, so
    leaving it unswept meant a retired authority claim could ship in the exact file the run tells
    you to open.

    The general lesson, which is why this is written as a fold over cases rather than one render:
    classifying a WRITER as prose says nothing about how many prose ARTIFACTS it emits. Coverage
    has to be per-artifact, and the sweep must concatenate them all — otherwise the derived
    classification is sound while the thing it derives is still partial.

    Both recovery-ref states are rendered because the import-error text and the next-steps
    guidance around it differ on exactly that field.
    """
    from syncade.adapters.producer import ProducerOutput
    from syncade.persistence import persist_producer_result
    from syncade.process import SubprocessError
    from syncade.producer import ProducerResult
    from syncade.producer_import import CandidateImportResult

    sha_a, sha_b = "a" * 40, "b" * 40
    recovery = f"refs/syncade/recovery/sweep/round-0/{sha_b}"
    cases = [
        # subprocess failure after a moved HEAD — the authored `producer.error.txt` sentence.
        ProducerResult(
            outcome="subprocess_error",
            starting_sha=sha_a,
            ending_sha=sha_b,
            duration_seconds=1.0,
            output=None,
            error=SubprocessError("crash"),
            candidate_import=None,
        ),
        # import error WITHOUT a recovery ref — the actor store is the only copy.
        ProducerResult(
            outcome="committed",
            starting_sha=sha_a,
            ending_sha=sha_b,
            duration_seconds=1.0,
            output=ProducerOutput(narrative_text="ok"),
            error=None,
            candidate_import=CandidateImportResult(status="error", error="fetch failed"),
        ),
        # import error WITH a recovery ref — anchored in the operator repository.
        ProducerResult(
            outcome="committed",
            starting_sha=sha_a,
            ending_sha=sha_b,
            duration_seconds=1.0,
            output=ProducerOutput(narrative_text="ok"),
            error=None,
            candidate_import=CandidateImportResult(
                status="error", recovery_ref=recovery, error="quarantine cleanup failed"
            ),
        ),
        # a REJECTED candidate — `invalid` carries an error and never a ref.
        ProducerResult(
            outcome="committed",
            starting_sha=sha_a,
            ending_sha=sha_b,
            duration_seconds=1.0,
            output=ProducerOutput(narrative_text="ok"),
            error=None,
            candidate_import=CandidateImportResult(
                status="invalid", error="candidate is not a descendant"
            ),
        ),
    ]

    rendered: list[str] = []
    for index, result in enumerate(cases):
        case_dir = d / f"case-{index}"
        case_dir.mkdir(parents=True, exist_ok=True)
        paths = persist_producer_result(case_dir, result)
        for artifact in (paths.error, paths.import_error):
            if artifact is not None and artifact.is_file():
                rendered.append(artifact.read_text(encoding="utf-8"))
    return "\n".join(rendered)


def sweep_persist_reviewer_result(d: Path) -> str:
    """Sweep the authored prose in <reviewer>.error.txt."""
    from syncade.dispatcher import ReviewerRunResult
    from syncade.persistence import persist_reviewer_result
    from syncade.process import SubprocessError

    d.mkdir(parents=True, exist_ok=True)
    run_result = ReviewerRunResult(
        reviewer_name="sweepreviewer",
        provider="anthropic",
        output=None,
        error=SubprocessError("crash"),
        duration_seconds=1.0,
    )
    persist_reviewer_result(d, run_result, None)
    error_path = d / "sweepreviewer.error.txt"
    return error_path.read_text(encoding="utf-8") if error_path.exists() else ""


def sweep_persist_synthesizer_result(d: Path) -> str:
    """Sweep the authored prose in synthesizer.error.txt."""
    from syncade.persistence import persist_synthesizer_result
    from syncade.process import SubprocessError
    from syncade.synthesizer import SynthesizerResult

    d.mkdir(parents=True, exist_ok=True)
    result = SynthesizerResult(
        output=None,
        error=SubprocessError("crash"),
        duration_seconds=1.0,
    )
    paths = persist_synthesizer_result(d, result)
    return paths.error.read_text(encoding="utf-8") if paths.error else ""
