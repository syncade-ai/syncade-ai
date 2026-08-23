"""`loop-summary.md` must describe where producer work actually is.

The next-steps text is keyed on `termination_reason`; the preservation decision in
`loop_finalize._unlanded_candidate_exists` is keyed on facts. They disagreed in BOTH directions
and the summary was wrong both times:

* `subprocess_error` with a moved HEAD — the producer committed and then crashed. The run
  preserves that repository as the only copy, while the summary said "no producer commits" and
  "failed before it could attempt the fix".
* `error` import carrying a recovery ref — the candidate DID reach the operator repository and
  was anchored, so the workspace is correctly deleted. The summary said the candidate "exists
  only in the preserved standalone producer repository" and pointed at a terminal safety notice
  that is not emitted on that path.

Both now read the same two facts the preservation decision reads — did HEAD move, and is there a
recovery ref — so the sentence and the decision cannot drift apart again. Asserted against the
RENDERED file, because a summary that is right in a helper and wrong on disk is still wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from syncade.adapters.producer import ProducerOutput
from syncade.orchestrator import RoundArtifacts, RoundResult
from syncade.persistence.loop_summary import persist_loop_summary
from syncade.process import SubprocessError
from syncade.producer import ProducerResult
from syncade.producer_import import CandidateImportResult
from syncade.snapshot import Snapshot
from syncade.synthesis import SynthesizerOutput
from syncade.synthesizer import SynthesizerResult

_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _round(producer: ProducerResult) -> RoundResult:
    round_dir = Path("round-0")
    return RoundResult(
        round_idx=0,
        snapshot=Snapshot(
            repo_root=Path("/tmp/test"),
            commit_sha="a" * 40,
            branch="work",
            base_ref=None,
            diff_text="",
            dirty_state="clean",
        ),
        dispatch_result=None,
        synth_result=SynthesizerResult(
            output=SynthesizerOutput(consolidated_findings=[], synthesis_summary="clean"),
            error=None,
            duration_seconds=1.0,
        ),
        test_result=None,
        test_skip_reason="test_command_unset",
        test_worktree_error=None,
        producer_result=producer,
        round_exit_code=30,
        artifacts=RoundArtifacts(
            round_idx=0,
            round_dir=round_dir,
            manifest_path=round_dir / "manifest.json",
            summary_path=round_dir / "summary.md",
        ),
    )


def _producer(outcome: str, candidate_import) -> ProducerResult:
    """`subprocess_error` carries an error and NO output — the schema enforces that pairing,
    which is precisely why the summary assumed nothing was committed on that path."""
    failed = outcome == "subprocess_error"
    return ProducerResult(
        outcome=outcome,
        starting_sha="a" * 40,
        ending_sha="b" * 40,
        duration_seconds=5.0,
        output=None if failed else ProducerOutput(narrative_text="ok"),
        error=SubprocessError("producer timed out after committing") if failed else None,
        candidate_import=candidate_import,
    )


def _summary(tmp_path: Path, producer: ProducerResult, reason: str, exit_code: int) -> str:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return persist_loop_summary(
        run_dir,
        final_exit_code=exit_code,
        final_round=0,
        termination_reason=reason,
        rounds=[_round(producer)],
        max_rounds=1,
        started_at=_AT,
        completed_at=_AT,
    ).read_text(encoding="utf-8")


def test_a_crash_after_committing_is_not_reported_as_no_commits(tmp_path):
    text = _summary(tmp_path, _producer("subprocess_error", None), "producer_subprocess_error", 40)

    assert "no producer commits" not in text, "the run preserves a repo it says holds nothing"
    assert "ONLY copy" in text
    assert "bbbbbbbbbbbb" in text, "the candidate SHA must appear in the commit series"


def test_an_anchored_candidate_is_not_reported_as_workspace_only(tmp_path):
    recovery = "refs/syncade/recovery/run-1/round-0/" + "b" * 40
    text = _summary(
        tmp_path,
        _producer(
            "committed",
            CandidateImportResult(
                status="error", recovery_ref=recovery, error="quarantine cleanup failed"
            ),
        ),
        "worktree_error",
        60,
    )

    assert "ONLY copy" not in text, "the candidate is anchored in the operator repository"
    assert recovery in text, "the summary must name the ref the operator can actually read"
    assert "IS in this repository" in text


def test_anchored_candidate_with_exit30_reports_workspace_also_preserved(tmp_path):
    """Exit 10/20/30 preserve actor workspaces by policy, even when a recovery ref exists.

    A committed/imported candidate followed by producer_stalled (exit 30) should say BOTH
    that the candidate is in the operator repository AND that the workspace is preserved —
    not that the workspace is not preserved, which was the incorrect pre-fix rendering.
    """
    recovery = "refs/syncade/recovery/run-1/round-0/" + "b" * 40
    text = _summary(
        tmp_path,
        _producer(
            "committed",
            CandidateImportResult(status="imported", recovery_ref=recovery, error=None),
        ),
        "producer_stalled",
        30,
    )

    assert "IS in this repository" in text
    assert recovery in text
    assert "not preserved" not in text, "exit 30 preserves actor workspaces; must not say otherwise"
    assert "ALSO preserved" in text


def test_anchored_candidate_with_exit20_reports_workspace_also_preserved(tmp_path):
    """max_rounds_reached (exit 20) also preserves actor workspaces by policy."""
    recovery = "refs/syncade/recovery/run-1/round-0/" + "b" * 40
    text = _summary(
        tmp_path,
        _producer(
            "committed",
            CandidateImportResult(status="imported", recovery_ref=recovery, error=None),
        ),
        "max_rounds_reached",
        20,
    )

    assert "IS in this repository" in text
    assert "not preserved" not in text, "exit 20 preserves actor workspaces; must not say otherwise"
    assert "ALSO preserved" in text


def test_anchored_candidate_with_exit60_reports_workspace_not_preserved(tmp_path):
    """Exit 60 does NOT preserve by exit-code policy; only unlanded candidates are kept."""
    recovery = "refs/syncade/recovery/run-1/round-0/" + "b" * 40
    text = _summary(
        tmp_path,
        _producer(
            "committed",
            CandidateImportResult(status="imported", recovery_ref=recovery, error=None),
        ),
        "worktree_error",
        60,
    )

    assert "IS in this repository" in text
    assert "not preserved" in text
    assert "ALSO preserved" not in text


def _multi_round_summary(
    tmp_path: Path,
    round0_producer: ProducerResult,
    round1_producer: ProducerResult,
    reason: str,
    exit_code: int,
) -> str:
    """Build a two-round loop summary for multi-round candidate-location tests."""
    round_dir_0 = Path("round-0")
    round_dir_1 = Path("round-1")
    snap = Snapshot(
        repo_root=Path("/tmp/test"),
        commit_sha="a" * 40,
        branch="work",
        base_ref=None,
        diff_text="",
        dirty_state="clean",
    )
    synth = SynthesizerResult(
        output=SynthesizerOutput(consolidated_findings=[], synthesis_summary="clean"),
        error=None,
        duration_seconds=1.0,
    )
    r0 = RoundResult(
        round_idx=0,
        snapshot=snap,
        dispatch_result=None,
        synth_result=synth,
        test_result=None,
        test_skip_reason="test_command_unset",
        test_worktree_error=None,
        producer_result=round0_producer,
        round_exit_code=30,
        artifacts=RoundArtifacts(
            round_idx=0,
            round_dir=round_dir_0,
            manifest_path=round_dir_0 / "manifest.json",
            summary_path=round_dir_0 / "summary.md",
        ),
    )
    r1 = RoundResult(
        round_idx=1,
        snapshot=snap,
        dispatch_result=None,
        synth_result=synth,
        test_result=None,
        test_skip_reason="test_command_unset",
        test_worktree_error=None,
        producer_result=round1_producer,
        round_exit_code=30,
        artifacts=RoundArtifacts(
            round_idx=1,
            round_dir=round_dir_1,
            manifest_path=round_dir_1 / "manifest.json",
            summary_path=round_dir_1 / "summary.md",
        ),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return persist_loop_summary(
        run_dir,
        final_exit_code=exit_code,
        final_round=1,
        termination_reason=reason,
        rounds=[r0, r1],
        max_rounds=3,
        started_at=_AT,
        completed_at=_AT,
    ).read_text(encoding="utf-8")


def test_multi_round_unlanded_r1_masks_anchored_r0(tmp_path):
    """Round 0 anchored candidate must not mask round 1's unlanded subprocess_error candidate.

    Before the fix, candidate_location_note() returned on the FIRST round with a moved HEAD.
    In a multi-round run where round 0 was imported (anchored) and round 1 crashed after
    committing, the note would point at round 0's recovery ref and say the workspace is
    reconstructible — while finalization preserved round 1's standalone repo as the only copy.
    """
    r0_recovery = "refs/syncade/recovery/run-x/round-0/" + "b" * 40
    r0 = ProducerResult(
        outcome="committed",
        starting_sha="a" * 40,
        ending_sha="b" * 40,
        duration_seconds=5.0,
        output=ProducerOutput(narrative_text="round 0 ok"),
        error=None,
        candidate_import=CandidateImportResult(
            status="imported", recovery_ref=r0_recovery, error=None
        ),
    )
    r1 = ProducerResult(
        outcome="subprocess_error",
        starting_sha="c" * 40,
        ending_sha="d" * 40,
        duration_seconds=3.0,
        output=None,
        error=SubprocessError("producer crashed after committing"),
        candidate_import=None,
    )
    text = _multi_round_summary(tmp_path, r0, r1, "producer_subprocess_error", 40)

    assert "ONLY copy" in text, "round 1 is unlanded; its standalone repo is the only copy"
    assert "dddddddddddd" in text, "round 1's candidate SHA must appear in the commit series"
    assert r0_recovery not in text or "ONLY copy" in text, (
        "round 0's recovery ref must not be the candidate-location note when round 1 is unlanded"
    )


def test_provider_usage_limit_with_anchored_candidate_does_not_claim_preserved_standalone(tmp_path):
    """provider_usage_limit must not claim the candidate is in the preserved standalone
    repository when it is actually anchored via a recovery_ref.

    Before the fix, _LOOP_NEXT_STEPS['provider_usage_limit'] ended with the sentence
    "Any isolated producer candidate remains in its preserved standalone repository."
    That sentence is wrong when the candidate was imported and anchored — the workspace
    was deleted because candidate_disposition sees a reachable_ref.  The sentence was
    removed; candidate_location_note renders the correct location instead.
    """
    recovery = "refs/syncade/recovery/run-1/round-0/" + "b" * 40
    text = _summary(
        tmp_path,
        _producer(
            "committed",
            CandidateImportResult(status="imported", recovery_ref=recovery, error=None),
        ),
        "provider_usage_limit",
        25,
    )
    assert "preserved standalone" not in text, (
        "provider_usage_limit must not claim preserved standalone when candidate is anchored"
    )
    assert recovery in text, "recovery ref must appear in candidate_location_note"


def test_budget_exceeded_with_anchored_candidate_does_not_claim_preserved_standalone(tmp_path):
    """budget_exceeded must not claim the candidate is in the preserved standalone repository
    when it is actually anchored via a recovery_ref (same class as provider_usage_limit)."""
    recovery = "refs/syncade/recovery/run-1/round-0/" + "b" * 40
    run_dir = tmp_path / "run-budget"
    run_dir.mkdir(parents=True, exist_ok=True)
    text = persist_loop_summary(
        run_dir,
        final_exit_code=25,
        final_round=0,
        termination_reason="budget_exceeded",
        rounds=[
            _round(
                _producer(
                    "committed",
                    CandidateImportResult(status="imported", recovery_ref=recovery, error=None),
                )
            )
        ],
        max_rounds=1,
        started_at=_AT,
        completed_at=_AT,
        budget_usages=[],  # bypass _run_usages which needs dispatch_result
    ).read_text(encoding="utf-8")
    assert "preserved standalone" not in text, (
        "budget_exceeded must not claim preserved standalone when candidate is anchored"
    )
    assert recovery in text, "recovery ref must appear in candidate_location_note"


def test_multi_round_both_anchored_shows_recovery_ref(tmp_path):
    """When all rounds are anchored, the summary reports the recovery ref (not 'ONLY copy')."""
    r0_recovery = "refs/syncade/recovery/run-x/round-0/" + "b" * 40
    r1_recovery = "refs/syncade/recovery/run-x/round-1/" + "d" * 40
    r0 = ProducerResult(
        outcome="committed",
        starting_sha="a" * 40,
        ending_sha="b" * 40,
        duration_seconds=5.0,
        output=ProducerOutput(narrative_text="round 0 ok"),
        error=None,
        candidate_import=CandidateImportResult(
            status="imported", recovery_ref=r0_recovery, error=None
        ),
    )
    r1 = ProducerResult(
        outcome="committed",
        starting_sha="c" * 40,
        ending_sha="d" * 40,
        duration_seconds=3.0,
        output=ProducerOutput(narrative_text="round 1 ok"),
        error=None,
        candidate_import=CandidateImportResult(
            status="imported", recovery_ref=r1_recovery, error=None
        ),
    )
    text = _multi_round_summary(tmp_path, r0, r1, "max_rounds_reached", 20)

    assert "ONLY copy" not in text, "all rounds are anchored; no unlanded candidate"
    assert r0_recovery in text or r1_recovery in text, "at least one recovery ref must appear"
