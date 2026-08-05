"""PR-h-01 increment D — the paraphrase escalation.

Two reviewers describe one bug in different words. Consolidating that is the
synthesizer's whole job, so it can instead emit them as two single-reviewer
findings and dismiss each on its own merits: the unanimous-blocker rule never
fires (each finding has one reviewer), the exact-duplicate split guard never
fires (the texts differ), ``has_active_blocker`` sees nothing, and the round is
a clean SHIP. Reproduced end-to-end against ``2ca8a74``.

The guard does not try to decide whether two findings are the same concern —
a similarity heuristic would manufacture false locks. It asks a mechanical
question: did two or more distinct reviewers each say "blocker", and did every
one of those get deactivated? If so the round is exit 10 (decision needed),
which cannot false-lock: the worst case is a human being asked to look.
"""

from __future__ import annotations

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.findings import Finding, ReviewerOutput
from syncade.orchestrator import run_review
from syncade.synthesis import (
    ConsolidatedFinding,
    FindingProvenance,
    SynthesizerOutput,
)
from tests.orchestrator._helpers import _factory_returning, _two_reviewer_config

# The same defect, as two independent reviewers would actually word it.
_A = "login query interpolates the username directly into SQL"
_B = "unsanitized user input reaches the auth query without parameterization"


def _reviewer(finding_text: str, severity: str = "blocker") -> ReviewerOutput:
    return ReviewerOutput(
        verdict="NO-SHIP",
        summary="reviewed the auth path",
        findings=[
            Finding(
                severity=severity,
                file="src/auth.py",
                line=42,
                spec_clause="§3.1",
                finding=finding_text,
            )
        ],
        priority_order=[0],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _split(reviewer_name: str, quote: str, *, severity: str = "blocker") -> ConsolidatedFinding:
    """One single-reviewer consolidated finding, deactivated.

    ``severity="blocker"`` + ``dismissed=True`` is the dismissal path;
    ``severity="minor"`` is the downgrade path. Both deactivate.
    """
    kwargs: dict = {
        "description": f"concern raised by {reviewer_name}",
        "file": "src/auth.py",
        "severity": severity,
        "provenance": [
            FindingProvenance(
                reviewer_name=reviewer_name,
                original_severity="blocker",
                original_index=0,
                original_description=quote,  # verbatim, per increment C
            )
        ],
    }
    if severity == "blocker":
        kwargs["dismissed"] = True
        kwargs["dismissal_rationale"] = "input is validated upstream at the router"
    else:
        kwargs["severity_change_rationale"] = "reads as style, not a security defect"
    return ConsolidatedFinding(**kwargs)


def _run(repo, pr_doc, reviewers: list[ReviewerOutput], synth: SynthesizerOutput):
    return run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        adapter_factory=_factory_returning(*[FakeAdapter(canned_output=r) for r in reviewers]),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=synth),
    )


class TestParaphrasedBlockersEscalate:
    def test_split_and_dismissed_is_exit_10_not_ship(self, repo_with_pr_doc):
        """The reproduced attack: both reviewers said blocker, the synthesizer
        split the concern and dismissed each half, nothing is active."""
        repo, pr_doc = repo_with_pr_doc
        synth = SynthesizerOutput(
            consolidated_findings=[_split("rv1", _A), _split("rv2", _B)],
            synthesis_summary="both concerns reviewed and ruled out",
        )
        result = _run(repo, pr_doc, [_reviewer(_A), _reviewer(_B)], synth)
        assert result.exit_code == 10

    def test_split_and_downgraded_is_exit_10_not_ship(self, repo_with_pr_doc):
        """Downgrade off 'blocker' deactivates just as dismissal does; the
        guard must not be dismissal-only."""
        repo, pr_doc = repo_with_pr_doc
        synth = SynthesizerOutput(
            consolidated_findings=[
                _split("rv1", _A, severity="minor"),
                _split("rv2", _B, severity="minor"),
            ],
            synthesis_summary="both read as minor",
        )
        result = _run(repo, pr_doc, [_reviewer(_A), _reviewer(_B)], synth)
        assert result.exit_code == 10


class TestControlsThatMustNotEscalate:
    """The guard is deliberately narrow. These four shapes must be untouched —
    a false escalation costs the operator's trust in every refusal."""

    def test_single_reviewer_blocker_dismissed_still_ships(self, repo_with_pr_doc):
        """Dismissal authority over a SINGLE-source blocker is intentional and
        unchanged: only one reviewer said blocker, so there is no independent
        corroboration to escalate about."""
        repo, pr_doc = repo_with_pr_doc
        clean = ReviewerOutput(
            verdict="SHIP",
            summary="looks fine",
            findings=[],
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        synth = SynthesizerOutput(
            consolidated_findings=[_split("rv1", _A)],
            synthesis_summary="one concern, ruled out",
        )
        result = _run(repo, pr_doc, [_reviewer(_A), clean], synth)
        assert result.exit_code == 0

    def test_two_reviewers_but_only_minor_findings_still_ships(self, repo_with_pr_doc):
        """No reviewer said blocker → nothing to corroborate → silent."""
        repo, pr_doc = repo_with_pr_doc
        synth = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="style nit",
                    file="src/auth.py",
                    severity="minor",
                    provenance=[
                        FindingProvenance(
                            reviewer_name=name,
                            original_severity="minor",
                            original_index=0,
                            original_description=text,
                        )
                        for name, text in (("rv1", _A), ("rv2", _B))
                    ],
                )
            ],
            synthesis_summary="minor only",
        )
        result = _run(
            repo,
            pr_doc,
            [_reviewer(_A, severity="minor"), _reviewer(_B, severity="minor")],
            synth,
        )
        assert result.exit_code == 0

    def test_surviving_active_blocker_is_still_exit_30(self, repo_with_pr_doc):
        """A determinate NO-SHIP outranks escalation — exit 30, not 10."""
        repo, pr_doc = repo_with_pr_doc
        synth = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="sql injection in auth",
                    file="src/auth.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="rv1",
                            original_severity="blocker",
                            original_index=0,
                            original_description=_A,
                        )
                    ],
                ),
                _split("rv2", _B),
            ],
            synthesis_summary="one stands, one ruled out",
        )
        result = _run(repo, pr_doc, [_reviewer(_A), _reviewer(_B)], synth)
        assert result.exit_code == 30

    def test_clean_run_with_no_findings_still_ships(self, repo_with_pr_doc):
        """The ordinary SHIP path is byte-identical."""
        repo, pr_doc = repo_with_pr_doc
        clean = ReviewerOutput(
            verdict="SHIP",
            summary="clean",
            findings=[],
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        synth = SynthesizerOutput(consolidated_findings=[], synthesis_summary="nothing to report")
        result = _run(repo, pr_doc, [clean, clean], synth)
        assert result.exit_code == 0


class TestOperatorDocument:
    """Exit 10 with no explanation is worse than useless: nothing is listed as
    an active blocker, so the operator has no way to see what happened."""

    def test_decision_needed_md_quotes_both_reviewers_and_dispositions(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        synth = SynthesizerOutput(
            consolidated_findings=[_split("rv1", _A), _split("rv2", _B, severity="minor")],
            synthesis_summary="both ruled out",
        )
        result = _run(repo, pr_doc, [_reviewer(_A), _reviewer(_B)], synth)
        assert result.exit_code == 10

        doc = next(repo.glob(".syncade/runs/*/decision-needed.md"))
        text = doc.read_text()
        # The reviewers' OWN words, not the synthesizer's framing.
        assert _A in text
        assert _B in text
        assert "rv1" in text and "rv2" in text
        # What the synthesizer did with each, so the comparison is possible.
        assert "dismissed" in text
        assert "downgraded to minor" in text
        assert "input is validated upstream at the router" in text
        # And what the operator is being asked to do.
        assert "decide whether the synthesizer was right" in text.lower()

    def test_no_decision_doc_on_an_ordinary_ship(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        clean = ReviewerOutput(
            verdict="SHIP",
            summary="clean",
            findings=[],
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        synth = SynthesizerOutput(consolidated_findings=[], synthesis_summary="nothing")
        result = _run(repo, pr_doc, [clean, clean], synth)
        assert result.exit_code == 0
        assert not list(repo.glob(".syncade/runs/*/decision-needed.md"))


class TestArtifactsDoNotContradictTheExitCode:
    """Found by adversarial review of increment D. Every one of these had the
    run's own artifacts telling the operator something the exit code denies."""

    def _escalated(self, repo, pr_doc):
        synth = SynthesizerOutput(
            consolidated_findings=[_split("rv1", _A), _split("rv2", _B)],
            synthesis_summary="both ruled out",
        )
        result = _run(repo, pr_doc, [_reviewer(_A), _reviewer(_B)], synth)
        assert result.exit_code == 10
        return next(d for d in sorted((repo / ".syncade" / "runs").iterdir()) if d.is_dir())

    def test_findings_md_does_not_say_SHIP(self, repo_with_pr_doc):
        """The most-read artifact said `Verdict: SHIP` on a round that exited 10
        precisely because it could not justify a SHIP.

        This bug class has regressed three times — failed test leg (T1.6),
        failed blocking check, and now this — each time because a new exit code
        landed without a matching branch in the findings.md verdict matrix.
        """
        run_dir = self._escalated(*repo_with_pr_doc)
        verdict_line = next(
            ln
            for ln in (run_dir / "round-0" / "findings.md").read_text().splitlines()
            if ln.startswith("**Verdict:**")
        )
        assert "SHIP" not in verdict_line.replace("NO-SHIP", "")
        assert "DECISION NEEDED" in verdict_line
        assert "decision-needed.md" in verdict_line

    def test_loop_summary_does_not_blame_a_producer_that_never_ran(self, repo_with_pr_doc):
        """max_rounds=1: no producer is ever dispatched, yet the summary
        attributed the termination to a producer escalation and told the
        operator to write decision.txt and resume."""
        run_dir = self._escalated(*repo_with_pr_doc)
        summary = (run_dir / "loop-summary.md").read_text()
        assert "producer escalation" not in summary
        assert "the producer escalated a finding" not in summary
        # Neither continuation applies when no blocker is active. The text may
        # NAME them (to say so); it must not INSTRUCT the operator to use them.
        assert "record your decision in" not in summary
        assert "syncade --resume" not in summary
        assert "do not apply here" in summary

    def test_resume_is_refused_rather_than_silently_wasting_a_panel(self, repo_with_pr_doc):
        """Resuming re-reviewed a byte-identical tree, silently ignored
        decision.txt (the reader is keyed to an escalated PRODUCER round), and
        deterministically reproduced exit 10 — at the cost of a full panel."""
        from syncade.orchestrator.resume_plan import plan_resume
        from syncade.orchestrator.resume_types import ResumeError

        repo, pr_doc = repo_with_pr_doc
        run_dir = self._escalated(repo, pr_doc)
        with pytest.raises(ResumeError) as exc:
            plan_resume(repo, run_dir)
        assert "nothing to resume" in str(exc.value)

    def test_decision_doc_branch_claim_is_not_hardcoded(self, repo_with_pr_doc):
        """`No branch was advanced.` was asserted unconditionally, so a
        multi-round run that had already fast-forwarded told the operator their
        branch was untouched. Here no branch advanced, so the claim is true —
        the point is that it is now derived rather than assumed."""
        from syncade.persistence import persist_deactivated_blockers_decision_needed

        run_dir = self._escalated(*repo_with_pr_doc)
        assert "No branch was advanced." in (run_dir / "decision-needed.md").read_text()

        advanced = persist_deactivated_blockers_decision_needed(
            run_dir,
            round_idx=1,
            run_id="r",
            deactivated=[("rv1", "t", "dismissed — x")],
            branch_advanced=True,
        ).read_text()
        assert "No branch was advanced." not in advanced
        assert "already advanced" in advanced

    def test_producer_escalation_doc_branch_claim_is_not_hardcoded(self, tmp_path):
        """`persist_decision_needed` (the producer escalation path) had the
        branch-advanced text hardcoded to 'No branch was advanced.' — so a
        multi-round run that had already fast-forwarded told the operator their
        branch was untouched even when it was not."""
        from syncade.persistence.decision_needed import persist_decision_needed
        from syncade.producer_escalation import ProducerEscalation

        escalation = ProducerEscalation(
            finding="spec conflict in auth handler",
            decision="should we allow legacy sessions?",
            options=["yes", "no"],
            rationale="the test suite reproduces the conflict",
            finding_indices=[0],
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        not_advanced = persist_decision_needed(
            run_dir, round_idx=0, escalation=escalation, run_id="r", branch_advanced=False
        ).read_text()
        assert "No branch was advanced." in not_advanced
        assert "already advanced" not in not_advanced

        advanced = persist_decision_needed(
            run_dir, round_idx=1, escalation=escalation, run_id="r", branch_advanced=True
        ).read_text()
        assert "No branch was advanced." not in advanced
        assert "already advanced" in advanced

    def test_multi_round_blockers_all_deactivated_reports_prior_advance(self, repo_with_pr_doc):
        """After a prior round committed producer fixes, the round-1
        blockers_all_deactivated decision-needed.md must say the branch
        was already advanced — not 'No branch was advanced.'

        This was broken because loop.py never threaded branch_advanced_during_run
        into _run_round_step, so both exit-10 writers always saw False."""

        from syncade.adapters.fake import FakeProducerAdapter
        from syncade.config import SyncadeConfig
        from tests.orchestrator._helpers import (
            _no_ship,
            _RoundCyclingSynth,
            _synth_with_blocker,
        )

        repo, pr_doc = repo_with_pr_doc

        # Round 0: two blockers → producer commits → branch advances.
        # Round 1: two reviewers each raise a blocker, synth dismisses both
        #          → blockers_all_deactivated → exit 10.
        round1_synth = SynthesizerOutput(
            consolidated_findings=[_split("rv1", _A), _split("rv2", _B)],
            synthesis_summary="both ruled out",
        )
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_reviewer(_A)),
                FakeAdapter(canned_output=_reviewer(_B)),
            ),
            synthesizer_adapter=_RoundCyclingSynth(
                _synth_with_blocker(),
                round1_synth,
            ),
            producer_adapter=FakeProducerAdapter(commit_message="fix: round-0 producer"),
        )

        assert result.exit_code == 10
        run_dir = next(d for d in sorted((repo / ".syncade" / "runs").iterdir()) if d.is_dir())
        doc_text = (run_dir / "decision-needed.md").read_text()
        assert "No branch was advanced." not in doc_text, (
            "branch WAS advanced in round 0; decision-needed.md must reflect that"
        )
        assert "already advanced" in doc_text
