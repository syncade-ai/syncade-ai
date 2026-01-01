"""Producer-escalation loop + escalation coverage guard tests.

Moved verbatim from the former ``tests/test_orchestrator.py``.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _no_ship_n,
    _RoundCyclingSynth,
    _ship,
    _synth_with_blocker,
    _synth_with_n_blockers,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestProducerEscalationLoop:
    """PR-22 T4: when the producer escalates a finding (operator decision),
    the loop checkpoints and terminates with exit 10 + decision_needed, writes
    decision-needed.md, and does NOT override the mechanical verdict."""

    def _config(self, max_rounds: int = 3) -> SyncadeConfig:
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": max_rounds},
        )

    def _escalating_producer(self):
        import json

        from syncade.adapters.fake import FakeProducerAdapter
        from syncade.adapters.producer import ProducerOutput
        from syncade.producer_escalation import ESCALATE_CLOSE, ESCALATE_OPEN

        block = (
            "I cannot resolve this in code without a ruling.\n"
            f"{ESCALATE_OPEN}\n"
            + json.dumps(
                {
                    "finding_indices": [0],
                    "finding": "spec says omit empty checks; code echoes full schema",
                    "decision": "Should run-init omit empty checks or echo the full schema?",
                    "options": ["Omit empty checks", "Redefine the invariant"],
                    "rationale": (
                        "Reproduced: zero-config run-init.json gains checks:[]; the brief "
                        "demands byte-identity. Both cannot hold — operator must rule."
                    ),
                }
            )
            + f"\n{ESCALATE_CLOSE}\n"
        )
        return FakeProducerAdapter(canned_output=ProducerOutput(narrative_text=block))

    def test_escalation_terminates_decision_needed_exit_10(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker()),
            producer_adapter=self._escalating_producer(),
        )
        assert result.exit_code == 10  # CLARIFICATION_NEEDED reused for escalation
        assert result.termination_reason == "decision_needed"
        assert result.final_round == 0
        # decision-needed.md written at run root with the producer's case + options.
        dn = result.artifacts.run_dir / "decision-needed.md"
        assert dn.exists()
        text = dn.read_text()
        assert "Should run-init omit empty checks" in text
        assert "Omit empty checks" in text

    def test_escalation_does_not_override_mechanical_verdict(self, repo_with_pr_doc):
        """Escalation never dismisses a blocker: the round's mechanical verdict
        stays NO-SHIP (round_exit_code 30); only the loop-level exit code /
        termination reason reflect the escalation."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker()),
            producer_adapter=self._escalating_producer(),
        )
        assert result.rounds[0].round_exit_code == 30  # mechanical verdict unchanged
        assert result.rounds[0].producer_result.outcome == "escalated"

    def test_full_escalate_resume_with_decision_cycle(self, repo_with_pr_doc):
        """End-to-end (AC5/AC6/AC8): escalation checkpoints at exit 10 → the
        operator records a decision in decision.txt → --resume re-runs the
        escalated round with the decision fed to the producer's prompt. The
        whole cycle is headless — no in-process interactive wait."""
        from syncade.adapters.fake import FakeProducerAdapter
        from syncade.orchestrator.resume import plan_resume, read_resume_decision

        repo, pr_doc = repo_with_pr_doc

        # Phase 1 — the producer escalates → exit 10 + decision-needed.md.
        adapters1 = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
        r1 = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters1),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_with_blocker()),
            producer_adapter=self._escalating_producer(),
            logger=Logger(level="quiet"),
        )
        assert r1.exit_code == 10
        run_dir = r1.artifacts.run_dir
        assert (run_dir / "decision-needed.md").exists()

        # The operator records a decision (escalation didn't commit → no drift).
        (run_dir / "decision.txt").write_text(
            "DECISION: omit empty checks from config_snapshot.\n", encoding="utf-8"
        )

        # Phase 2 — resume re-runs the escalated round 0 with the decision.
        plan = plan_resume(repo, run_dir)
        assert plan.resumed_round == 0
        decision = read_resume_decision(run_dir, 0)
        assert decision == "DECISION: omit empty checks from config_snapshot."

        adapters2 = [FakeAdapter(canned_output=_no_ship()) for _ in range(4)]
        committing = FakeProducerAdapter(commit_message="fix: apply the operator's decision")
        r2 = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters2),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_with_blocker()),
            producer_adapter=committing,
            resume_plan=plan,
            operator_decision=decision,
            logger=Logger(level="quiet"),
        )
        # The injection seam: the resumed round-0 producer's prompt carried
        # the operator's decision.
        assert committing.invocations, "resumed producer should have run"
        _, _, prompt = committing.invocations[0]
        assert "DECISION: omit empty checks from config_snapshot." in prompt
        # Headless: the cycle ran to a terminal exit code with no wait.
        assert r2.exit_code in (0, 20, 30)
        # QA finding 2: the resolved checkpoint is cleaned — a converged resume
        # (producer COMMITTED, applying the decision) leaves no stale
        # decision-needed.md / decision.txt claiming a pending decision.
        assert not (run_dir / "decision-needed.md").exists()
        assert not (run_dir / "decision.txt").exists()

    def test_resume_that_does_not_commit_preserves_decision_txt(self, repo_with_pr_doc):
        """PR-22 dogfood (codex blocker): the operator's decision must NOT be
        lost when a resumed round fails to apply it. decision.txt is cleared
        only AFTER the resumed producer COMMITS (durably applies the ruling);
        on a non-commit outcome (here: producer subprocess_error / timeout) the
        decision is preserved so a re-resume re-injects it."""
        from syncade.adapters.base import ReviewerInvocationError as _RIE
        from syncade.adapters.fake import FakeProducerAdapter
        from syncade.orchestrator.resume import plan_resume, read_resume_decision

        repo, pr_doc = repo_with_pr_doc
        # Phase 1 — escalate.
        r1 = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(max_rounds=2),
            adapter_factory=_factory_returning(
                *[FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
            ),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_with_blocker()),
            producer_adapter=self._escalating_producer(),
            logger=Logger(level="quiet"),
        )
        assert r1.exit_code == 10
        run_dir = r1.artifacts.run_dir
        (run_dir / "decision.txt").write_text("DECISION: do X.\n", encoding="utf-8")

        # Phase 2 — resume, but the producer subprocess_errors (never commits).
        plan = plan_resume(repo, run_dir)
        decision = read_resume_decision(run_dir, plan.resumed_round)
        failing = FakeProducerAdapter(
            canned_exception=_RIE("producer died", returncode=1, stdout="", stderr="")
        )
        r2 = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(max_rounds=2),
            adapter_factory=_factory_returning(
                *[FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
            ),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_with_blocker()),
            producer_adapter=failing,
            resume_plan=plan,
            operator_decision=decision,
            logger=Logger(level="quiet"),
        )
        assert r2.exit_code == 40  # producer_subprocess_error
        # The decision survives — it was never durably applied.
        assert (run_dir / "decision.txt").exists()
        assert (run_dir / "decision.txt").read_text().strip() == "DECISION: do X."


class TestEscalationCoverageGuard:
    """PR-24: the loop honors a producer escalation (exit 10 + decision_needed)
    ONLY when its finding_indices cover every active blocker in the round —
    mechanical set-containment over the synth's own consolidated_findings. An
    escalation that leaves an active blocker uncovered, or references a
    non-blocker / out-of-range index, is treated as a stall (exit 30). The
    mechanical verdict (round_exit_code) is 30 in every case."""

    def _config(self, max_rounds: int = 3) -> SyncadeConfig:
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": max_rounds},
        )

    def _escalating_producer(self, finding_indices):
        import json

        from syncade.adapters.fake import FakeProducerAdapter
        from syncade.adapters.producer import ProducerOutput
        from syncade.producer_escalation import ESCALATE_CLOSE, ESCALATE_OPEN

        block = (
            "The remaining blockers are all downstream of one design question.\n"
            f"{ESCALATE_OPEN}\n"
            + json.dumps(
                {
                    "finding_indices": list(finding_indices),
                    "finding": "spec/design conflict resolved by one ruling",
                    "decision": "Should the contract be X or Y?",
                    "options": ["Make code X", "Amend the spec to Y"],
                    "rationale": "Reproduced: both constraints hold; operator must rule.",
                }
            )
            + f"\n{ESCALATE_CLOSE}\n"
        )
        return FakeProducerAdapter(canned_output=ProducerOutput(narrative_text=block))

    def _run(self, repo, pr_doc, *, n_blockers, escalate_indices):
        # rv1 returns n blocker findings (so the synth's per-blocker provenance
        # original_index values are in range); rv2 ships (unreferenced).
        adapters = [
            FakeAdapter(canned_output=_no_ship_n(n_blockers)),
            FakeAdapter(canned_output=_ship()),
        ]
        return run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_n_blockers(n_blockers)),
            producer_adapter=self._escalating_producer(escalate_indices),
            logger=Logger(level="quiet"),
        )

    def test_escalate_all_blockers_honored_exit_10(self, repo_with_pr_doc):
        """(a) escalation covering every active blocker → honored (exit 10)."""
        repo, pr_doc = repo_with_pr_doc
        result = self._run(repo, pr_doc, n_blockers=2, escalate_indices=[0, 1])
        assert result.exit_code == 10
        assert result.termination_reason == "decision_needed"
        assert (result.artifacts.run_dir / "decision-needed.md").exists()
        # (e) mechanical verdict unchanged.
        assert result.rounds[0].round_exit_code == 30
        assert result.rounds[0].producer_result.outcome == "escalated"

    def test_escalate_one_of_two_stalls_exit_30(self, repo_with_pr_doc):
        """(b) codex's reproduced gap, now closed: escalate one of two active
        blockers → uncovered → NOT honored → producer_stalled (exit 30), and no
        decision-needed.md is written."""
        repo, pr_doc = repo_with_pr_doc
        result = self._run(repo, pr_doc, n_blockers=2, escalate_indices=[0])
        assert result.exit_code == 30
        assert result.termination_reason == "producer_stalled"
        assert not (result.artifacts.run_dir / "decision-needed.md").exists()
        # (e) mechanical verdict unchanged.
        assert result.rounds[0].round_exit_code == 30

    def test_escalate_all_three_downstream_of_one_decision_honored(self, repo_with_pr_doc):
        """(c) no false-downgrade: one operator decision legitimately resolves
        three blockers; the escalation references all three → honored (exit 10),
        one decision-needed.md."""
        repo, pr_doc = repo_with_pr_doc
        result = self._run(repo, pr_doc, n_blockers=3, escalate_indices=[0, 1, 2])
        assert result.exit_code == 10
        assert result.termination_reason == "decision_needed"
        assert (result.artifacts.run_dir / "decision-needed.md").exists()
        # (e) mechanical verdict unchanged.
        assert result.rounds[0].round_exit_code == 30

    def test_escalate_out_of_range_index_stalls(self, repo_with_pr_doc):
        """(d) an escalation referencing a non-existent index → not honored →
        stall (exit 30), no crash."""
        repo, pr_doc = repo_with_pr_doc
        result = self._run(repo, pr_doc, n_blockers=1, escalate_indices=[5])
        assert result.exit_code == 30
        assert result.termination_reason == "producer_stalled"
        assert not (result.artifacts.run_dir / "decision-needed.md").exists()
        # (e) mechanical verdict unchanged.
        assert result.rounds[0].round_exit_code == 30

    def test_rejected_escalation_artifacts_describe_stall_not_checkpoint(self, repo_with_pr_doc):
        """A rejected escalation (uncovered blocker → exit 30, no
        decision-needed.md) must not leave the persisted operator artifacts
        describing an honored decision checkpoint: the per-round summary must not
        link a missing decision-needed.md or claim exit-10, the loop summary must
        not label the producer "operator decision needed", and the handoff must
        classify the producer as an unhonored escalation, NOT a subprocess_error."""
        repo, pr_doc = repo_with_pr_doc
        result = self._run(repo, pr_doc, n_blockers=2, escalate_indices=[0])
        run_dir = result.artifacts.run_dir
        assert result.exit_code == 30
        assert not (run_dir / "decision-needed.md").exists()

        # Per-round summary.md: no dangling decision-needed.md link, no
        # "operator decision needed" claim; rendered as an unhonored stall.
        summary = (run_dir / "round-0" / "summary.md").read_text()
        assert "decision-needed.md" not in summary
        assert "operator decision needed" not in summary
        assert "escalated but not honored" in summary

        # loop-summary.md: per-round producer line is the unhonored variant.
        loop_summary = (run_dir / "loop-summary.md").read_text()
        assert "operator decision needed" not in loop_summary
        assert "escalated but not honored" in loop_summary

        # handoff.md fires on exit 30 + active blockers; the producer must NOT be
        # misclassified as a subprocess_error.
        handoff = (run_dir / "handoff.md").read_text()
        assert "subprocess_error" not in handoff
        assert "escalated but not honored" in handoff
