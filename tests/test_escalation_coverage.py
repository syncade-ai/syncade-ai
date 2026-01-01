"""PR-24: the escalation coverage guard.

``escalation_covers_active_blockers`` decides whether the loop honors a
producer escalation (checkpoint + exit 10) or treats it as a stall
(exit 30). The honor condition is mechanical set-containment over the
synth's OWN active-blocker set — every active blocker must be covered by
the escalation's ``finding_indices`` AND every referenced index must be a
real active blocker. These tests pin both directions.
"""

from __future__ import annotations

from syncade.orchestrator.escalation_coverage import escalation_covers_active_blockers
from syncade.producer_escalation import ProducerEscalation
from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput


def _finding(severity: str, *, dismissed: bool = False) -> ConsolidatedFinding:
    return ConsolidatedFinding(
        description=f"a {severity} finding",
        file="src/x.py",
        severity=severity,  # type: ignore[arg-type]
        provenance=[
            FindingProvenance(
                reviewer_name="rv1",
                original_severity=severity,  # type: ignore[arg-type]
                original_index=0,
                original_description="src finding",
            )
        ],
        dismissed=dismissed,
        dismissal_rationale="exempt per spec" if dismissed else None,
    )


def _synth(*findings: ConsolidatedFinding) -> SynthesizerOutput:
    return SynthesizerOutput(
        consolidated_findings=list(findings),
        synthesis_summary="coverage-guard test fixture",
    )


def _esc(*indices: int) -> ProducerEscalation:
    return ProducerEscalation(
        finding_indices=list(indices),
        finding="f",
        decision="d",
        options=["o"],
        rationale="r",
    )


class TestEscalationCoversActiveBlockers:
    def test_single_blocker_covered(self):
        synth = _synth(_finding("blocker"))
        assert escalation_covers_active_blockers(_esc(0), synth) is True

    def test_all_blockers_covered(self):
        synth = _synth(_finding("blocker"), _finding("blocker"), _finding("blocker"))
        assert escalation_covers_active_blockers(_esc(0, 1, 2), synth) is True

    def test_coverage_is_order_insensitive(self):
        synth = _synth(_finding("blocker"), _finding("blocker"))
        assert escalation_covers_active_blockers(_esc(1, 0), synth) is True

    def test_one_blocker_left_uncovered_not_honored(self):
        """codex's reproduced gap: escalate one, leave one → not honored."""
        synth = _synth(_finding("blocker"), _finding("blocker"))
        assert escalation_covers_active_blockers(_esc(0), synth) is False

    def test_reference_to_minor_not_honored(self):
        # index 1 is a minor, not an active blocker.
        synth = _synth(_finding("blocker"), _finding("minor"))
        assert escalation_covers_active_blockers(_esc(1), synth) is False

    def test_reference_includes_non_blocker_not_honored(self):
        # covers the real blocker (0) but ALSO a non-blocker (1) → strict reject.
        synth = _synth(_finding("blocker"), _finding("minor"))
        assert escalation_covers_active_blockers(_esc(0, 1), synth) is False

    def test_out_of_range_index_not_honored(self):
        synth = _synth(_finding("blocker"))
        assert escalation_covers_active_blockers(_esc(5), synth) is False

    def test_dismissed_blocker_is_not_active(self):
        # idx 1 is a dismissed blocker (not active); the active one is idx 0.
        synth = _synth(_finding("blocker"), _finding("blocker", dismissed=True))
        # Escalation must cover {0} only; referencing the dismissed one fails.
        assert escalation_covers_active_blockers(_esc(0), synth) is True
        assert escalation_covers_active_blockers(_esc(1), synth) is False
        assert escalation_covers_active_blockers(_esc(0, 1), synth) is False

    def test_no_active_blockers_not_honored(self):
        # NO-SHIP via something other than a blocker (e.g. a failing test) —
        # no active blocker for a decision to resolve.
        synth = _synth(_finding("minor"))
        assert escalation_covers_active_blockers(_esc(0), synth) is False

    def test_none_synth_output_not_honored(self):
        assert escalation_covers_active_blockers(_esc(0), None) is False
