"""Synthesizer provenance cross-check regression tests.

Split out of ``tests/test_synthesizer.py`` to keep that file under the
LOC gate. Covers ``_validate_provenance_against_reviewers``' severity
cross-check — the guard that stops a synth from misreporting a
reviewer's severity to evade the unanimous-blocker deactivation rule.
"""

from __future__ import annotations

import pytest

from syncade.dispatcher import ReviewerRunResult
from syncade.findings import Finding, ReviewerOutput
from syncade.synthesis import (
    ConsolidatedFinding,
    FindingProvenance,
    SynthesizerOutput,
    SynthesizerOutputError,
)
from syncade.synthesizer import _validate_provenance_against_reviewers


class TestProvenanceSeverityValidation:
    """The unanimous-blocker deactivation guard keys on ``original_severity``,
    so a synth could slip a genuinely-unanimous blocker past it by recording
    ONE reviewer's severity as ``"minor"`` (a valid enum, real reviewer,
    in-range index) — ``all(== "blocker")`` then fails and the schema lets
    the dismissal through. ``_validate_provenance_against_reviewers`` closes
    this by cross-checking each entry's ``original_severity`` against the
    source reviewer's actual finding severity.
    """

    def _reviewer_results(self) -> list[ReviewerRunResult]:
        # Two reviewers, each with ONE blocker finding at index 0 — a
        # genuinely-unanimous blocker.
        def mk(name: str) -> ReviewerRunResult:
            out = ReviewerOutput(
                verdict="NO-SHIP",
                summary="bug",
                findings=[
                    Finding(
                        severity="blocker",
                        file="src/foo.py",
                        line=10,
                        spec_clause="must guard against None",
                        finding="missing null check",
                    )
                ],
                priority_order=[0],
                coverage_gaps=[],
                dismissed_concerns=[],
            )
            return ReviewerRunResult(
                reviewer_name=name,
                provider="anthropic",
                output=out,
                error=None,
                duration_seconds=1.0,
            )

        return [mk("claude-reviewer"), mk("codex-reviewer")]

    def _dismissed_output(self, codex_severity: str) -> SynthesizerOutput:
        return SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="genuinely unanimous blocker",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="claude-reviewer",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing null check",
                        ),
                        FindingProvenance(
                            reviewer_name="codex-reviewer",
                            original_severity=codex_severity,  # type: ignore[arg-type]
                            original_index=0,
                            original_description="missing null check",
                        ),
                    ],
                    dismissed=True,
                    dismissal_rationale="both reviewers misread; dismissing",
                )
            ],
            synthesis_summary="dismissed a finding two reviewers flagged",
        )

    def test_false_minor_severity_bypass_rejected(self) -> None:
        # The schema ACCEPTS this output (all(== blocker) is False because
        # codex's severity is recorded as "minor", so the unanimous-blocker
        # guard does not fire) — but the cross-check must reject it because
        # codex's ACTUAL finding severity is blocker.
        out = self._dismissed_output(codex_severity="minor")
        with pytest.raises(SynthesizerOutputError) as exc_info:
            _validate_provenance_against_reviewers(out, self._reviewer_results())
        assert "original_severity" in str(exc_info.value)
        assert "codex-reviewer" in str(exc_info.value)

    def test_truthful_severity_passes(self) -> None:
        # When codex's recorded severity matches its actual blocker finding,
        # the cross-check must NOT raise (no false positive). The schema
        # itself would have refused the dismissal here; the point of this
        # test is only that the severity cross-check passes truthful input.
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="genuinely unanimous blocker",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="claude-reviewer",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing null check",
                        ),
                        FindingProvenance(
                            reviewer_name="codex-reviewer",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing null check",
                        ),
                    ],
                    dismissed=False,
                )
            ],
            synthesis_summary="two reviewers flagged the same blocker",
        )
        _validate_provenance_against_reviewers(out, self._reviewer_results())
