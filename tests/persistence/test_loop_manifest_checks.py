"""Checks[] parity between the per-round manifest and the loop manifest.

N5: ``persist_loop_manifest`` previously never read ``r.check_results``,
so a check-driven round's loop-manifest entry omitted ``checks[]`` while
the per-round ``round-N/manifest.json`` included it. These tests pin the
two surfaces together: for a check-driven round both must carry an
identical ``checks[]`` block; for a zero-check round neither may carry a
``checks`` key (byte-identical to pre-checks behavior).
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence import persist_loop_manifest, persist_round_manifest
from syncade.test_runner import TestRunResult
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _ship,
    _snapshot,
    _subprocess_result,
    _synth_output_empty,
    _synth_result,
)


class TestLoopManifestChecksParity:
    def _dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def _checks(self) -> list[TestRunResult]:
        """A check-driven NO-SHIP: one BLOCKING check failed (drove the
        verdict), one advisory check passed. Two entries exercise the
        ``blocking`` boolean shape on both surfaces."""
        return [
            TestRunResult(
                name="file-length",
                severity="blocking",
                exit_code=1,
                outcome="failed",
                duration_seconds=0.5,
                stdout="src/x.py: 506 > 500\n",
                stderr="",
                command="scripts/check-loc.sh 500",
            ),
            TestRunResult(
                name="lint",
                severity="advisory",
                exit_code=0,
                outcome="passed",
                duration_seconds=0.3,
                stdout="",
                stderr="",
                command="ruff check .",
            ),
        ]

    def _round_result(self, *, check_results: list[TestRunResult], round_exit_code: int):
        """A synth-clean RoundResult whose NO-SHIP (when present) is
        check-driven. Mirrors the inputs handed to
        ``persist_round_manifest`` so the two surfaces are apples-to-apples."""
        from syncade.orchestrator import RoundArtifacts, RoundResult

        round_dir = Path("round-0")
        return RoundResult(
            round_idx=0,
            snapshot=_snapshot(),
            dispatch_result=self._dispatch(),
            synth_result=_synth_result(output=_synth_output_empty()),
            test_result=None,
            test_skip_reason="test_command_unset",
            test_worktree_error=None,
            producer_result=None,
            round_exit_code=round_exit_code,
            artifacts=RoundArtifacts(
                round_idx=0,
                round_dir=round_dir,
                manifest_path=round_dir / "manifest.json",
                summary_path=round_dir / "summary.md",
            ),
            check_results=check_results,
        )

    def test_check_driven_round_loop_entry_checks_match_round_manifest(self, tmp_path: Path):
        """Regression (N5): a check-driven NO-SHIP round's loop-manifest
        entry contains ``checks[]`` identical to the per-round manifest's."""
        round_dir = _make_round_dir(tmp_path)
        run_dir = round_dir.parent
        checks = self._checks()
        rr = self._round_result(check_results=checks, round_exit_code=30)

        round_path = persist_round_manifest(
            round_dir,
            rr.snapshot,
            rr.dispatch_result,
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=rr.synth_result,
            round_idx=0,
            check_results=checks,
        )
        loop_path = persist_loop_manifest(
            run_dir,
            final_exit_code=30,
            final_round=0,
            termination_reason="max_rounds_reached",
            rounds=[rr],
            max_rounds=1,
            started_at=_FIXED_STARTED_AT,
            producer_provider=None,
            producer_model=None,
        )

        round_manifest = json.loads(round_path.read_text())
        loop_manifest = json.loads(loop_path.read_text())
        loop_entry = loop_manifest["rounds"][0]

        # Loop entry must carry checks[] at all (the N5 bug omitted it).
        assert "checks" in loop_entry
        # The two surfaces must agree on presence AND exact shape.
        assert "checks" in round_manifest
        assert loop_entry["checks"] == round_manifest["checks"]
        # Spot-check the shape so a future change to either writer that
        # silently diverges is caught here too.
        assert [c["name"] for c in loop_entry["checks"]] == ["file-length", "lint"]
        assert loop_entry["checks"][0]["blocking"] is True
        assert loop_entry["checks"][1]["blocking"] is False

    def test_zero_check_round_loop_entry_and_round_manifest_both_omit_checks(self, tmp_path: Path):
        """Drift: a zero-check round must stay byte-identical — neither the
        per-round manifest nor the loop entry may carry a ``checks`` key."""
        round_dir = _make_round_dir(tmp_path)
        run_dir = round_dir.parent
        rr = self._round_result(check_results=[], round_exit_code=0)

        round_path = persist_round_manifest(
            round_dir,
            rr.snapshot,
            rr.dispatch_result,
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=rr.synth_result,
            round_idx=0,
        )
        loop_path = persist_loop_manifest(
            run_dir,
            final_exit_code=0,
            final_round=0,
            termination_reason="ship",
            rounds=[rr],
            max_rounds=1,
            started_at=_FIXED_STARTED_AT,
            producer_provider=None,
            producer_model=None,
        )

        round_manifest = json.loads(round_path.read_text())
        loop_entry = json.loads(loop_path.read_text())["rounds"][0]
        assert "checks" not in round_manifest
        assert "checks" not in loop_entry
