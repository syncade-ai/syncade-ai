"""Unanimous-blocker dismissal: distinct-reviewer keying.

Split out of ``tests/synthesis/test_models.py`` to keep that file
under the LOC gate. Covers the mis-key fix — the unanimous-blocker
rule keys on DISTINCT ``reviewer_name``s, not raw provenance entries.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence._markdown import _consensus_lines
from syncade.synthesis import ConsolidatedFinding
from tests.synthesis._helpers import _finding, _provenance


class TestUnanimousBlockerDistinctKeying:
    def test_single_reviewer_listed_twice_does_not_lock_dismissal(self) -> None:
        """Mis-key fix: the unanimous-blocker rule keys on DISTINCT
        reviewers, not raw provenance entries. ONE reviewer listed
        twice (two entries, same ``reviewer_name``, both blocker) is a
        single-reviewer blocker — dismissible — and must NOT be falsely
        locked. Before the fix ``len(provenance) >= 2`` counted the two
        raw entries and rejected the dismissal.
        """
        cf = ConsolidatedFinding(
            **_finding(
                severity="blocker",
                provenance=[
                    _provenance(reviewer_name="claude-reviewer", original_index=0),
                    _provenance(reviewer_name="claude-reviewer", original_index=1),
                ],
                dismissed=True,
                dismissal_rationale=(
                    "the single reviewer flagged this twice; both are the same "
                    "false positive the spec exempts in §3.2"
                ),
            )
        )
        assert cf.dismissed is True

    def test_two_distinct_reviewers_both_blocker_still_locked(self) -> None:
        """Keep: two DISTINCT reviewers both at blocker is a genuine
        unanimous blocker — dismissal stays refused after the
        distinct-keying change (the mis-key fix must not weaken the
        guard for the real case)."""
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="blocker",
                    provenance=[
                        _provenance(reviewer_name="claude-reviewer"),
                        _provenance(reviewer_name="codex-reviewer"),
                    ],
                    dismissed=True,
                    dismissal_rationale="I think both reviewers misread the spec",
                )
            )
        message = str(exc_info.value)
        assert "unanimous-blocker" in message
        # Distinct count of 2 reflected in the message, not a raw-entry count.
        assert "by 2 reviewers" in message

    def test_schema_guard_and_consensus_renderer_both_count_distinct_reviewers(self) -> None:
        finding = ConsolidatedFinding(
            **_finding(
                severity="blocker",
                provenance=[
                    _provenance(reviewer_name="claude-reviewer", original_index=0),
                    _provenance(reviewer_name="claude-reviewer", original_index=1),
                ],
                dismissed=True,
                dismissal_rationale="same reviewer duplicated the same false positive",
            )
        )
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="claude-reviewer",
                    provider="anthropic",
                    output=None,
                    error=RuntimeError("not used by consensus rendering"),
                    duration_seconds=1.0,
                ),
                ReviewerRunResult(
                    reviewer_name="codex-reviewer",
                    provider="openai",
                    output=None,
                    error=RuntimeError("not used by consensus rendering"),
                    duration_seconds=1.0,
                ),
            ],
            total_duration_seconds=2.0,
        )

        assert _consensus_lines(finding, dispatch) == ["**Consensus:** 1 of 2 reviewers  "]
