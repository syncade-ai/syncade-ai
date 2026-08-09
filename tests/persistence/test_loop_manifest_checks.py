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


class TestLoopManifestRefusalFields:
    """Regression: loop-manifest.json per-round entries must mirror refusal discriminator
    fields from the per-round manifest so tooling can classify refusal causes without reading
    individual round-N/manifest.json files."""

    def _refusal_round_result(
        self,
        *,
        round_dir: Path,
        oversize_prompt_chars: int | None = None,
        oversize_diff_bytes: int | None = None,
        fail_closed_headers: list[str] | None = None,
        filtered_diff_bytes: int | None = None,
        raw_diff_bytes: int | None = None,
    ):
        from syncade.dispatcher import DispatchResult
        from syncade.orchestrator import RoundArtifacts, RoundResult

        return RoundResult(
            round_idx=0,
            snapshot=_snapshot(),
            dispatch_result=DispatchResult(results=[], total_duration_seconds=0.0),
            synth_result=None,
            test_result=None,
            test_skip_reason=None,
            test_worktree_error=None,
            producer_result=None,
            round_exit_code=60,
            artifacts=RoundArtifacts(
                round_idx=0,
                round_dir=round_dir,
                manifest_path=round_dir / "manifest.json",
                summary_path=round_dir / "summary.md",
            ),
            oversize_prompt_chars=oversize_prompt_chars,
            oversize_diff_bytes=oversize_diff_bytes,
            fail_closed_headers=fail_closed_headers,
            filtered_diff_bytes=filtered_diff_bytes,
            raw_diff_bytes=raw_diff_bytes,
        )

    def _loop_entry(self, run_dir: Path, rr, *, termination: str = "prompt_too_large") -> dict:
        loop_path = persist_loop_manifest(
            run_dir,
            final_exit_code=60,
            final_round=0,
            termination_reason=termination,
            rounds=[rr],
            max_rounds=1,
            started_at=_FIXED_STARTED_AT,
            producer_provider=None,
            producer_model=None,
        )
        return json.loads(loop_path.read_text())["rounds"][0]

    def test_prompt_too_large_refusal_fields_in_loop_manifest(self, tmp_path: Path):
        """Regression: a prompt_too_large refusal must set refusal_reason and
        oversize_prompt_chars in the loop manifest's per-round entry."""
        round_dir = _make_round_dir(tmp_path)
        rr = self._refusal_round_result(
            round_dir=round_dir,
            oversize_prompt_chars=1_100_000,
        )
        entry = self._loop_entry(round_dir.parent, rr)

        assert entry.get("refusal_reason") == "prompt_too_large", (
            f"loop manifest missing refusal_reason='prompt_too_large', "
            f"got {entry.get('refusal_reason')!r}"
        )
        assert entry.get("oversize_prompt_chars") == 1_100_000, (
            "loop manifest missing oversize_prompt_chars"
        )

    def test_diff_too_large_refusal_reason_in_loop_manifest(self, tmp_path: Path):
        """Regression: a diff_too_large refusal must set refusal_reason in the loop manifest.

        This test uses the REAL _diff_too_large_result path so it catches the bug where
        _persist_refusal passed sizes to persist_round_manifest but did NOT populate
        filtered_diff_bytes/raw_diff_bytes on the returned RoundResult — causing
        persist_loop_manifest to write null for both size fields.
        """
        from syncade.config import SyncadeConfig
        from syncade.logging import Logger
        from syncade.orchestrator.round_no_changes import _diff_too_large_result

        diff_text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n+foo\n"
        snap = _snapshot(diff_text=diff_text, base_oid="b" * 40)
        config = SyncadeConfig()
        round_dir = _make_round_dir(tmp_path)
        logger = Logger()

        rr = _diff_too_large_result(
            round_idx=0,
            snapshot=snap,
            round_dir=round_dir,
            config=config,
            started_at=_FIXED_STARTED_AT,
            resumed_under_drift=False,
            diff_bytes=2_000_000,
            logger=logger,
        )

        # RoundResult must carry the sizes so persist_loop_manifest can write them.
        assert rr.filtered_diff_bytes == 2_000_000
        assert rr.raw_diff_bytes == len(diff_text.encode("utf-8"))

        loop_path = persist_loop_manifest(
            round_dir.parent,
            final_exit_code=60,
            final_round=0,
            termination_reason="diff_too_large",
            rounds=[rr],
            max_rounds=1,
            started_at=_FIXED_STARTED_AT,
            producer_provider=None,
            producer_model=None,
        )

        entry = json.loads(loop_path.read_text())["rounds"][0]
        assert entry.get("refusal_reason") == "diff_too_large", (
            f"loop manifest missing refusal_reason='diff_too_large', "
            f"got {entry.get('refusal_reason')!r}"
        )
        # The diff sizes must appear in the snapshot section of the loop manifest.
        assert entry["snapshot"].get("diff_bytes_reviewed") == 2_000_000, (
            "loop manifest missing diff_bytes_reviewed from diff_too_large path"
        )
        assert entry["snapshot"].get("diff_bytes") == len(diff_text.encode("utf-8")), (
            "loop manifest missing diff_bytes from diff_too_large path"
        )

    def test_diff_malformed_refusal_fields_in_loop_manifest(self, tmp_path: Path):
        """Regression: a diff_malformed refusal must set refusal_reason and
        diff_filter_refusal_headers in the loop manifest's per-round entry."""
        round_dir = _make_round_dir(tmp_path)
        rr = self._refusal_round_result(
            round_dir=round_dir,
            fail_closed_headers=["diff --git a/foo b/bar"],
        )
        entry = self._loop_entry(round_dir.parent, rr, termination="diff_malformed")

        assert entry.get("refusal_reason") == "diff_malformed"
        assert entry.get("diff_filter_refusal_headers") == ["diff --git a/foo b/bar"]

    def test_clean_round_has_no_refusal_fields(self, tmp_path: Path):
        """A normal successful round must not carry refusal_reason or related fields."""
        from syncade.dispatcher import DispatchResult
        from syncade.orchestrator import RoundArtifacts, RoundResult

        round_dir = _make_round_dir(tmp_path)
        rr_clean = RoundResult(
            round_idx=0,
            snapshot=_snapshot(),
            dispatch_result=DispatchResult(results=[], total_duration_seconds=0.0),
            synth_result=None,
            test_result=None,
            test_skip_reason=None,
            test_worktree_error=None,
            producer_result=None,
            round_exit_code=0,
            artifacts=RoundArtifacts(
                round_idx=0,
                round_dir=round_dir,
                manifest_path=round_dir / "manifest.json",
                summary_path=round_dir / "summary.md",
            ),
        )
        entry = self._loop_entry(round_dir.parent, rr_clean, termination="ship")
        assert "refusal_reason" not in entry
        assert "oversize_prompt_chars" not in entry
        assert "diff_filter_refusal_headers" not in entry
