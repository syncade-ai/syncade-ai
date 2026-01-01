"""Finding A2 — a unanimous reviewer blocker cannot be DOWNGRADED off 'blocker'.

Sibling of the dismissal arm: the unanimous-blocker guarantee must forbid BOTH
deactivation paths. A synth that relabels a unanimous blocker to ``minor``/``nit``
with ``dismissed=False`` (so the dismissal arm never fires) would slip it past
``has_active_blocker`` — which counts only non-dismissed ``"blocker"`` findings —
yielding a false exit-0 SHIP. The schema validator
``_validate_unanimous_blocker_not_deactivated`` now rejects it at construction
(so a synth output that does this fails to parse → exit 70).
"""

from __future__ import annotations

import pytest

from syncade.synthesis import ConsolidatedFinding, FindingProvenance


def _unanimous_provenance() -> list[FindingProvenance]:
    """Two DISTINCT reviewers, both flagging the same finding at blocker."""
    return [
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
    ]


class TestUnanimousBlockerCannotBeDowngraded:
    def test_downgrade_to_minor_rejected(self) -> None:
        """THE A2 REPRO: a unanimous blocker relabeled to minor with
        dismissed=False (and a valid severity_change_rationale, so only the
        unanimous-blocker guard can object) must be rejected."""
        with pytest.raises(ValueError) as exc_info:
            ConsolidatedFinding(
                description="unanimous blocker, secretly downgraded",
                file="src/foo.py",
                severity="minor",
                provenance=_unanimous_provenance(),
                dismissed=False,
                severity_change_rationale="synth claims it's only minor",
            )
        msg = str(exc_info.value)
        assert "unanimous-blocker rule" in msg
        assert "downgrade" in msg

    def test_downgrade_to_nit_rejected(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            ConsolidatedFinding(
                description="unanimous blocker downgraded to nit",
                file="src/foo.py",
                severity="nit",
                provenance=_unanimous_provenance(),
                dismissed=False,
                severity_change_rationale="synth claims it's only a nit",
            )
        assert "unanimous-blocker rule" in str(exc_info.value)

    def test_active_blocker_is_allowed(self) -> None:
        """The only legitimate disposition for a unanimous blocker: keep it an
        ACTIVE blocker (severity 'blocker', not dismissed)."""
        ConsolidatedFinding(
            description="unanimous blocker kept active",
            file="src/foo.py",
            severity="blocker",
            provenance=_unanimous_provenance(),
            dismissed=False,
        )

    def test_dismissal_arm_still_rejected(self) -> None:
        """Regression: the original dismissal arm of the guard still fires."""
        with pytest.raises(ValueError) as exc_info:
            ConsolidatedFinding(
                description="unanimous blocker dismissed",
                file="src/foo.py",
                severity="blocker",
                provenance=_unanimous_provenance(),
                dismissed=True,
                dismissal_rationale="synth thinks both reviewers are wrong",
            )
        msg = str(exc_info.value)
        assert "unanimous-blocker rule" in msg
        assert "dismiss" in msg

    def test_single_reviewer_downgrade_allowed(self) -> None:
        """Non-unanimous (one reviewer at blocker): downgrading is legitimate
        severity arbitration, allowed with a severity_change_rationale."""
        ConsolidatedFinding(
            description="one reviewer's blocker, synth arbitrates to minor",
            file="src/foo.py",
            severity="minor",
            provenance=[
                FindingProvenance(
                    reviewer_name="claude-reviewer",
                    original_severity="blocker",
                    original_index=0,
                    original_description="missing null check",
                )
            ],
            dismissed=False,
            severity_change_rationale="single reviewer; synth's bounded judgment",
        )

    def test_duplicate_reviewer_downgrade_allowed(self) -> None:
        """One reviewer listed twice is NOT unanimity (distinct-reviewer keying),
        so a downgrade is allowed — the mis-key parity with the dismissal arm."""
        dup = "claude-reviewer"
        ConsolidatedFinding(
            description="same reviewer twice, not unanimous",
            file="src/foo.py",
            severity="minor",
            provenance=[
                FindingProvenance(
                    reviewer_name=dup,
                    original_severity="blocker",
                    original_index=0,
                    original_description="missing null check",
                ),
                FindingProvenance(
                    reviewer_name=dup,
                    original_severity="blocker",
                    original_index=1,
                    original_description="another issue",
                ),
            ],
            dismissed=False,
            severity_change_rationale="single reviewer (listed twice); arbitration",
        )
