"""Resume classifier ↔ verdict agreement on checks-driven rounds.

Split out of ``tests/resume/test_find_resolve_plan.py`` to keep that
file under the LOC gate. Pins that ``resume._round_manifest_indicates_
phase_failure`` and ``verdict._classify_phase_failure`` agree on a
round driven to exit 40 by a blocking mechanical check's subprocess
error (a §5 drift fix).
"""

from __future__ import annotations

import types

from syncade.exit_codes import REVIEWER_FAILURE
from syncade.orchestrator import _classify_phase_failure
from syncade.orchestrator.resume import _round_manifest_indicates_phase_failure
from syncade.persistence.checks import _check_manifest_entry
from syncade.test_runner import TestRunResult


def _clean_manifest_with_checks(checks: list[dict]) -> dict:
    """A round manifest whose reviewer/synth/test/producer phases are all
    clean — only the ``checks[]`` array carries a failure signal. Mirrors the
    shape persistence writes (``persistence/round_manifest.py``)."""
    return {
        "reviewers": [{"name": "claude-reviewer", "outcome": "success"}],
        "synthesizer": {"outcome": "success"},
        "test_run": None,
        "test_skip_reason": None,
        "producer": {
            "outcome": "committed",
            "starting_sha": "a" * 40,
            "ending_sha": "b" * 40,
        },
        "round_exit_code": REVIEWER_FAILURE,
        "checks": checks,
    }


class TestChecksPhaseFailureClassification:
    """A blocking mechanical check whose subprocess errors drives the round to
    exit 40 (resume-eligible). The resume classifier must agree with
    ``verdict._classify_phase_failure`` — a §5 drift fix."""

    def test_blocking_check_subprocess_error_is_phase_failure(self):
        """checks:[{...outcome:"subprocess_error", blocking:true...}] with
        otherwise-clean phases → drop-and-retry."""
        check = TestRunResult(
            name="lint",
            severity="blocking",
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=1.0,
            stdout="",
            stderr="ruff: command not found",
            error=FileNotFoundError("ruff"),
            command="ruff check .",
        )
        manifest = _clean_manifest_with_checks([_check_manifest_entry(check)])
        assert _round_manifest_indicates_phase_failure(manifest) is True

    def test_blocking_check_failed_is_clean(self):
        """A blocking check that merely FAILED (→ exit 30) is a clean NO-SHIP
        signal handled by the terminator — NOT a phase failure (mirrors
        test_run 'failed')."""
        check = TestRunResult(
            name="lint",
            severity="blocking",
            exit_code=1,
            outcome="failed",
            duration_seconds=1.0,
            stdout="1 error",
            stderr="",
            command="ruff check .",
        )
        manifest = _clean_manifest_with_checks([_check_manifest_entry(check)])
        manifest["round_exit_code"] = 30
        assert _round_manifest_indicates_phase_failure(manifest) is False

    def test_advisory_check_subprocess_error_is_not_phase_failure(self):
        """An ADVISORY check's subprocess error never gates and never drives
        exit 40 — it is not a phase failure."""
        check = TestRunResult(
            name="lint",
            severity="advisory",
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=1.0,
            stdout="",
            stderr="boom",
            error=FileNotFoundError("ruff"),
            command="ruff check .",
        )
        manifest = _clean_manifest_with_checks([_check_manifest_entry(check)])
        manifest["round_exit_code"] = 0
        assert _round_manifest_indicates_phase_failure(manifest) is False

    def test_surfaces_agree_on_checks_driven_round(self):
        """Pin: the resume classifier and ``verdict._classify_phase_failure``
        agree on a checks-driven round. Both are fed the SAME blocking
        subprocess-error TestRunResult — verdict labels the live round
        ``check_subprocess_error`` (resume-eligible exit 40), so the resume
        classifier MUST mark the persisted round a phase failure."""
        check = TestRunResult(
            name="lint",
            severity="blocking",
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=1.0,
            stdout="",
            stderr="boom",
            error=FileNotFoundError("ruff"),
            command="ruff check .",
        )
        # Live-round surface: every other phase clean, blocking check errored.
        round_result = types.SimpleNamespace(
            round_exit_code=REVIEWER_FAILURE,
            dispatch_result=types.SimpleNamespace(all_succeeded=True),
            synth_result=types.SimpleNamespace(error=None),
            test_result=None,
            check_results=[check],
        )
        assert _classify_phase_failure(round_result) == "check_subprocess_error"
        # Persisted surface: the manifest that round would write.
        manifest = _clean_manifest_with_checks([_check_manifest_entry(check)])
        assert _round_manifest_indicates_phase_failure(manifest) is True


class TestDiffMalformedPhaseFailure:
    """Regression: a round with diff_filter_refusal_headers must be classified
    as a phase failure so plan_resume retries it rather than treating it as a
    cleanly-completed round that degenerates into a ResumeError."""

    def _diff_malformed_manifest(self) -> dict:
        return {
            "reviewers": [],
            "synthesizer": None,
            "test_run": None,
            "test_skip_reason": None,
            "producer": None,
            "round_exit_code": 60,
            "diff_filter_refusal_headers": ["diff --git a/bad.py b/bad.py"],
        }

    def test_diff_filter_refusal_headers_is_phase_failure(self):
        """A manifest with diff_filter_refusal_headers → phase failure (retry)."""
        manifest = self._diff_malformed_manifest()
        assert _round_manifest_indicates_phase_failure(manifest) is True

    def test_absent_diff_filter_refusal_headers_not_phase_failure(self):
        """None-valued diff_filter_refusal_headers does not trigger."""
        manifest = self._diff_malformed_manifest()
        manifest["diff_filter_refusal_headers"] = None
        assert _round_manifest_indicates_phase_failure(manifest) is False
