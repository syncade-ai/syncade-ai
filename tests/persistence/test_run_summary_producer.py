"""Tests for :mod:`syncade.persistence`.

Constructs :class:`ReviewerRunResult` / :class:`DispatchResult` /
:class:`Snapshot` / :class:`SubprocessResult` directly — no real
subprocess calls or git operations. The orchestrator is the only
production caller; these tests target the persistence module in
isolation so a future regression in file-layout, JSON shape, or
manifest schema fails here rather than at the integration boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import Finding, ReviewerOutput
from syncade.persistence import (
    persist_round_manifest,
    persist_run_summary,
)
from syncade.process import SubprocessTimeoutError
from syncade.snapshot import Snapshot
from tests.persistence._helpers import _FIXED_STARTED_AT, _make_round_dir


class TestRunSummaryProducerSection:
    """PR-8 polish R1.T4: ``persist_run_summary`` accepts a
    ``producer_result`` kwarg and renders a ``## Producer``
    subsection when supplied. Pre-R1.T4 summary.md was written
    once inside _run_one_round (BEFORE the producer phase) and
    never re-rendered — operators reading per-round summary.md
    saw no indication that the producer attempted a fix on this
    round.

    The orchestrator now re-renders summary.md after the
    producer phase with the producer_result threaded in.
    """

    def _build_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=ReviewerOutput(
                        verdict="NO-SHIP",
                        findings=[
                            Finding(
                                severity="blocker",
                                file="src/x.py",
                                spec_clause="G1",
                                finding="bug",
                            )
                        ],
                        summary="finding",
                        priority_order=[0],
                        coverage_gaps=[],
                        dismissed_concerns=[],
                    ),
                    error=None,
                    duration_seconds=2.0,
                ),
            ],
            total_duration_seconds=2.0,
        )

    def _producer_committed(self):
        from syncade.adapters.producer import ProducerOutput
        from syncade.producer import ProducerResult

        return ProducerResult(
            outcome="committed",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=20.0,
            output=ProducerOutput(narrative_text="fix applied"),
            error=None,
        )

    def _producer_stalled(self):
        from syncade.adapters.producer import ProducerOutput
        from syncade.producer import ProducerResult

        return ProducerResult(
            outcome="stalled",
            starting_sha="a" * 40,
            ending_sha="a" * 40,
            duration_seconds=8.0,
            output=ProducerOutput(narrative_text="cannot fix without more info"),
            error=None,
        )

    def _producer_subprocess_error(self):
        from syncade.producer import ProducerResult

        return ProducerResult(
            outcome="subprocess_error",
            starting_sha="a" * 40,
            ending_sha="a" * 40,
            duration_seconds=5.0,
            output=None,
            error=SubprocessTimeoutError("timeout", stdout="", stderr="", timeout=10.0),
        )

    def _producer_indeterminate_subprocess_error(self):
        from syncade.producer import ProducerResult

        return ProducerResult(
            outcome="subprocess_error",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=5.0,
            output=None,
            error=SubprocessTimeoutError("timeout", stdout="partial", stderr="err", timeout=10.0),
        )

    def _producer_escalated(self):
        from syncade.adapters.producer import ProducerOutput
        from syncade.producer import ProducerResult
        from syncade.producer_escalation import ProducerEscalation

        return ProducerResult(
            outcome="escalated",
            starting_sha="a" * 40,
            ending_sha="a" * 40,
            duration_seconds=9.0,
            output=ProducerOutput(narrative_text="this needs an operator decision"),
            error=None,
            escalation=ProducerEscalation(
                finding_indices=[0],
                finding="spec vs code conflict",
                decision="X or Y?",
                options=["X", "Y"],
                rationale="reproduced both constraints",
            ),
        )

    def test_escalated_producer_manifest_carries_escalation(self, tmp_path):
        """PR-22 QA finding 3: the round manifest's producer block carries the
        structured escalation so a tool reading manifest.json alone can
        reconstruct WHY the loop paused (not only decision-needed.md)."""
        round_dir = _make_round_dir(tmp_path)
        path = persist_round_manifest(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            round_idx=0,
            producer_result=self._producer_escalated(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        prod = json.loads(path.read_text())["producer"]
        assert prod["outcome"] == "escalated"
        assert prod["escalation"] == {
            "finding": "spec vs code conflict",
            "decision": "X or Y?",
            "options": ["X", "Y"],
            "rationale": "reproduced both constraints",
        }

    def test_non_escalated_producer_manifest_omits_escalation_key(self, tmp_path):
        """Byte-identical for committed/stalled/error: no 'escalation' key."""
        round_dir = _make_round_dir(tmp_path)
        path = persist_round_manifest(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            round_idx=0,
            producer_result=self._producer_committed(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        assert "escalation" not in json.loads(path.read_text())["producer"]

    def test_indeterminate_producer_manifest_marks_moved_head(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        path = persist_round_manifest(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            round_idx=0,
            producer_result=self._producer_indeterminate_subprocess_error(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        prod = json.loads(path.read_text())["producer"]
        assert prod["outcome"] == "subprocess_error"
        assert prod["starting_sha"] == "a" * 40
        assert prod["ending_sha"] == "b" * 40
        assert prod["indeterminate_commit"] is True

    def test_escalated_producer_renders_summary_section(self, tmp_path):
        """The summary.md producer section names the escalation + the decision
        when the coverage guard HONORED the escalation (PR-24)."""
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=10,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_escalated(),
            producer_provider="anthropic",
            producer_model="sonnet",
            escalation_honored=True,
        )
        text = path.read_text()
        assert "**Outcome:** escalated (operator decision needed)" in text
        assert "**Decision needed:** X or Y?" in text
        assert "decision-needed.md" in text

    def test_honored_escalation_next_steps_no_prior_advance(self, tmp_path):
        """When no prior round advanced the branch, the escalation next-steps must
        say 'No branch was advanced by this round', not the prior (stale) unconditional
        'WITHOUT advancing any branch'."""
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=10,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_escalated(),
            producer_provider="anthropic",
            producer_model="sonnet",
            escalation_honored=True,
            branch_already_advanced=False,
        ).read_text()
        ns = text[text.find("## Next steps") :]
        assert "No branch was advanced by this round" in ns
        assert "An earlier round" not in ns

    def test_honored_escalation_next_steps_with_prior_advance(self, tmp_path):
        """When a prior round already advanced the branch, the escalation next-steps
        must say so rather than claiming no branch was advanced."""
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=10,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_escalated(),
            producer_provider="anthropic",
            producer_model="sonnet",
            escalation_honored=True,
            branch_already_advanced=True,
        ).read_text()
        ns = text[text.find("## Next steps") :]
        assert "An earlier round already advanced your branch" in ns
        assert "No branch was advanced" not in ns

    def test_rejected_escalation_renders_stall_not_checkpoint(self, tmp_path):
        """PR-24: when the coverage guard REJECTED the escalation
        (``escalation_honored=False``, the default), the summary's producer
        section must describe a stall — no "operator decision needed" claim and
        no link to a decision-needed.md that was never written."""
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_escalated(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        text = path.read_text()
        assert "escalated but not honored" in text
        assert "operator decision needed" not in text
        assert "decision-needed.md" not in text

    def _snapshot(self) -> Snapshot:
        return Snapshot(
            repo_root=Path("/tmp/x"),
            commit_sha="a" * 40,
            branch="main",
            base_ref=None,
            diff_text="",
            dirty_state="clean",
        )

    def test_committed_producer_renders_section(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_committed(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        text = path.read_text()
        assert "## Producer" in text
        assert "**Outcome:** committed" in text
        assert "**Provider / model:** anthropic / sonnet" in text
        assert "bbbbbbbbbbbb" in text  # short SHA
        assert "[producer.commit.txt](producer.commit.txt)" in text

    def test_stalled_producer_renders_section(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_stalled(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        text = path.read_text()
        assert "## Producer" in text
        assert "**Outcome:** stalled" in text

    def test_subprocess_error_producer_renders_error_link(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_subprocess_error(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        text = path.read_text()
        assert "## Producer" in text
        assert "**Outcome:** subprocess_error" in text
        assert "**Error:** SubprocessTimeoutError" in text
        # error.txt linked on the subprocess_error path
        assert "[producer.error.txt](producer.error.txt)" in text

    def test_indeterminate_subprocess_error_renders_moved_head_warning(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_indeterminate_subprocess_error(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        text = path.read_text()
        assert "**Outcome:** subprocess_error" in text
        assert "**Indeterminate producer commit:**" in text
        assert "aaaaaaaaaaaa" in text
        assert "bbbbbbbbbbbb" in text
        assert "branch was not advanced" in text

    def test_no_producer_arg_omits_section(self, tmp_path):
        """When producer_result is None (no producer ran on this
        round — SHIP at this round, or max_rounds=1, or
        single-pass), the Producer section is omitted entirely.
        Back-compat: pre-PR-8 summary.md shape preserved when
        producer_result is the new arg's default None."""
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
        )
        text = path.read_text()
        assert "## Producer" not in text

    def test_committed_producer_next_steps_changes(self, tmp_path):
        """R1.T4 Next-steps re-routing: when a producer ran on
        this round, the Next-steps text mentions the producer's
        commit instead of saying "hand to producer for fix"
        (which would be misleading — the producer ALREADY tried).

        The committed-producer next-steps should reference the
        commit SHA AND the producer.stdout artifact."""
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_committed(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        text = path.read_text()
        next_section = text[text.find("## Next steps") :]
        assert "producer subprocess committed" in next_section
        assert "bbbbbbbbbbbb" in next_section
        assert "producer.stdout" in next_section
        # The pre-R1.T4 misleading wording must be gone.
        assert "hand to producer" not in next_section.lower()

    def test_stalled_producer_next_steps_mentions_stall(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            self._snapshot(),
            self._build_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
            producer_result=self._producer_stalled(),
            producer_provider="anthropic",
            producer_model="sonnet",
        )
        next_section = path.read_text()[path.read_text().find("## Next steps") :]
        assert "did NOT commit" in next_section or "stalled" in next_section.lower()
        assert "clarify the spec" in next_section.lower() or "manually" in next_section.lower()
