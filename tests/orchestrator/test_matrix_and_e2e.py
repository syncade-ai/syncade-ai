"""Tests for :mod:`syncade.orchestrator`.

Uses :class:`FakeAdapter` exclusively via the ``adapter_factory``
parameter — no real CLI calls. Each test sets up an ephemeral git
repo in ``tmp_path`` so the snapshot + worktree provisioning steps
exercise real git, then injects fakes for the reviewer dispatch.

Total runtime under 5 seconds (the brief's bound).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.findings import ReviewerOutputError
from syncade.logging import Logger
from syncade.orchestrator import RunArtifacts, RunResult, run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RegressionParserFakeAdapter,
    _ship,
    _SlowFakeAdapter,
    _synth_with_blocker,
    _two_reviewer_config,
)

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestComputeExitCodeFullMatrix:
    """PR-7 task 6: the rewritten ``_compute_exit_code`` decision table,
    exercised cell-by-cell through ``run_review``. Each test pins one
    row of the table; the matrix as a whole is the contract.

    Reviewer-phase failure rows (60/50/70/40) are also covered by the
    pre-PR-7 tests in TestReviewerFailures / TestWorktreeError /
    TestUnknownProvider; this class adds the rows the PR-7 synthesizer
    phase introduces.
    """

    def test_all_succeeded_synth_clean_exit_0(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),  # empty findings
        )
        assert result.exit_code == 0

    def test_all_succeeded_synth_active_blocker_exit_30(self, repo_with_pr_doc):
        """QA fix P0.2: synth provenance references rv1 at index 0;
        rv1 must therefore produce at least 1 finding for the
        validator to accept. ``_no_ship()`` produces 1 finding.
        """
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_output=_synth_with_blocker(reviewer_name="rv1")
            ),
        )
        assert result.exit_code == 30

    def test_all_succeeded_synth_parse_fail_exit_70(self, repo_with_pr_doc):
        from syncade.synthesis import SynthesizerOutputError

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_exception=SynthesizerOutputError("unparseable")
            ),
        )
        assert result.exit_code == 70

    def test_all_succeeded_synth_subprocess_fail_exit_40(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_exception=ReviewerInvocationError(
                    "auth failed",
                    returncode=1,
                    stdout="",
                    stderr="",
                )
            ),
        )
        assert result.exit_code == 40

    def test_reviewer_failure_precedence_skips_synth_phase(self, repo_with_pr_doc):
        """Reviewer-phase failures take precedence over the synth
        phase — the synth never runs. Pin that a reviewer
        ReviewerOutputError → exit 70 even when a hypothetical synth
        adapter would have surfaced something different."""
        from syncade.synthesis import SynthesizerOutputError

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_exception=ReviewerOutputError("parse fail")),
        ]
        # synth adapter never gets called because reviewer dispatch
        # failed; the canned_exception here would otherwise produce
        # a different exit code.
        synth_adapter = FakeSynthesizerAdapter(
            canned_exception=SynthesizerOutputError("would be 70 too but skipped")
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        # Reviewer parse failure → 70. Synth was never called.
        assert result.exit_code == 70
        assert len(synth_adapter.invocations) == 0

    def test_worktree_error_is_exception_routed_not_in_dispatch_result(self):
        """QA fix #19 (P1.14): pin that ``WorktreeError`` takes the
        exception-bubble path (raised pre-dispatch by
        ``WorktreeManager.create``, caught by the CLI), NOT the
        ``DispatchResult`` route. The dispatcher and adapters don't
        manipulate worktrees; no code path produces a
        ``DispatchResult.failures`` entry whose error is a
        ``WorktreeError``. The previous decision table had a
        WorktreeError branch in ``_compute_exit_code`` that was dead
        code; removed.

        If a future refactor accidentally routes WorktreeError
        through DispatchResult AND restores the
        ``_compute_exit_code`` branch, that's a contract change the
        operator should know about — this test catches the resurrection
        attempt by asserting the corrected behavior: a hand-crafted
        DispatchResult with a WorktreeError-typed failure falls
        through to the defensive "any other failure" bucket → exit
        40, NOT exit 60.
        """
        from syncade.dispatcher import DispatchResult, ReviewerRunResult
        from syncade.orchestrator import _compute_exit_code
        from syncade.worktree import WorktreeError

        dispatch_result = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="fake1",
                    output=None,
                    error=WorktreeError("hypothetical"),
                    duration_seconds=0.0,
                )
            ],
            total_duration_seconds=0.0,
        )
        exit_code = _compute_exit_code(dispatch_result, None)
        assert exit_code == 40, (
            f"Expected exit 40 (defensive bucket); got {exit_code}. "
            "If this is 60, the WorktreeError branch is back in "
            "_compute_exit_code — but WorktreeError still cannot reach "
            "DispatchResult through the production dispatcher, so the "
            "branch is dead code. Route exit 60 through the exception "
            "path (WorktreeManager.create → run_review → CLI exception "
            "handler) instead."
        )

    def test_internal_contract_violation_synth_none_on_all_success_raises(self):
        """Direct unit test of _compute_exit_code: passing
        synth_result=None when every reviewer succeeded is a contract
        violation — surface it rather than silently misclassifying."""
        from syncade.dispatcher import DispatchResult, ReviewerRunResult
        from syncade.orchestrator import _compute_exit_code

        dispatch_result = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="fake1",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                )
            ],
            total_duration_seconds=1.0,
        )
        with pytest.raises(RuntimeError, match="synth_result is None"):
            _compute_exit_code(dispatch_result, None)


class TestEndToEndPr56ParserRegression:
    """PR-5.6 task 5: end-to-end regression test using FakeAdapter that
    synthesizes the Acme 2026-05-15 failure mode through the real
    orchestrator dispatch + persistence path. Pre-fix: exit 70,
    .parsed.json missing for the failing reviewer. Post-fix: exit 30
    (the real NO-SHIP verdict the parser correctly extracted),
    .parsed.json contains the actual 8 findings.

    This complements the unit test in test_findings.py — that one
    pins parse_reviewer_output's behavior in isolation; this one
    proves the orchestrator + dispatcher + persistence wire it
    correctly with no on-the-way regression."""

    def test_jsx_prose_regression_does_not_exit_70_and_persists_real_verdict(
        self, repo_with_pr_doc
    ):
        from syncade.findings import parse_reviewer_output
        from syncade.synthesis import (
            ConsolidatedFinding,
            FindingProvenance,
            SynthesizerOutput,
        )

        repo, pr_doc = repo_with_pr_doc
        fixture = (
            Path(__file__).parent.parent
            / "fixtures"
            / "pr-5.6-parser-regression"
            / "claude-reviewer-prose-with-jsx.stdout"
        )
        envelope = json.loads(fixture.read_text())
        result_text = envelope["result"]

        # Sanity-check the fixture: the parser by itself extracts the
        # NO-SHIP verdict. If this fails, Task 1 regressed and the
        # orchestrator-level assertion below would be ambiguous.
        sanity = parse_reviewer_output(result_text)
        assert sanity.verdict == "NO-SHIP"
        assert len(sanity.findings) == 8

        adapters = [
            _RegressionParserFakeAdapter(result_text),
            FakeAdapter(canned_output=_ship()),
        ]
        # Finding R (cannot-OMIT pass-through): the JSX fixture (rv1) is NO-SHIP
        # with FOUR blockers, so the synth must pass through ALL of them.
        blocker_indices = [i for i, f in enumerate(sanity.findings) if f.severity == "blocker"]
        jsx_synth = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description=f"rv1 JSX-fixture blocker at index {idx}",
                    file="src/x.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="rv1",
                            original_severity="blocker",
                            original_index=idx,
                            original_description=sanity.findings[idx].finding,
                        )
                    ],
                    dismissed=False,
                )
                for idx in blocker_indices
            ],
            synthesis_summary="rv1's four JSX-fixture blockers, all passed through",
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            # PR-7: with the synthesizer phase wired in, exit 30 requires
            # the synth to surface an active blocker. The regression
            # we're pinning is that the reviewer parse succeeded (the
            # JSX trap didn't trigger exit 70); fake the synth output
            # so the mechanical verdict reflects the reviewer's NO-SHIP
            # findings.
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=jsx_synth),
        )

        # Post-fix: the JSX-laced response parses correctly through
        # the orchestrator's dispatch path. The reviewer's verdict
        # parsed cleanly (NO-SHIP, 8 findings), the synth surfaced
        # an active blocker → exit 30, NOT exit 70.
        assert result.exit_code == 30, (
            f"expected exit 30 (FINDINGS_PRESENT), got {result.exit_code} — "
            "if 70, the parser regressed against the Acme JSX case"
        )
        # The failing-pre-fix reviewer's .parsed.json exists and
        # contains the REAL verdict (not the JSX fragment).
        round_dir = result.artifacts.round_dir
        parsed_path = round_dir / "rv1.parsed.json"
        assert parsed_path.is_file(), "rv1.parsed.json missing — parser still failing"
        parsed = json.loads(parsed_path.read_text())
        assert parsed["verdict"] == "NO-SHIP"
        assert len(parsed["findings"]) == 8
        # Cross-check: rv1 has NO error.txt (the parse succeeded).
        assert not (round_dir / "rv1.error.txt").exists(), (
            "rv1.error.txt present — would mean the parse exception path fired"
        )


class TestEndToEndOperationalHardening:
    """6a: all five PR-5.5 fixes exercised together in one hermetic run —
    repo-root discovery (task 1), configurable timeout (task 2),
    partial-output preservation (task 3), lifecycle logging (task 4),
    and the run summary file (task 5)."""

    def test_subdir_timeout_run_lands_correctly(self, repo_with_pr_doc, capsys):
        repo, pr_doc = repo_with_pr_doc
        subdir = repo / "docs" / "feature-work"
        subdir.mkdir(parents=True)

        adapters = [_SlowFakeAdapter() for _ in range(2)]
        result = run_review(
            repo_root=subdir,  # invoked from a subdirectory (task 1)
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            timeout_seconds=0.5,  # real, fast timeout (task 2)
            logger=Logger("normal"),  # task 4
            adapter_factory=_factory_returning(*adapters),
        )

        # Task 1: artifacts at the repo root, not under the subdir.
        assert result.artifacts.run_dir.parent == repo / ".syncade" / "runs"
        assert not (subdir / ".syncade").exists()

        # Tasks 2 + 3: both reviewers timed out -> exit 40 per the table.
        assert result.exit_code == 40
        round_dir = result.artifacts.round_dir
        for name in ("rv1", "rv2"):
            # Task 3: the partial output the subprocess emitted before
            # the SIGKILL is preserved ON DISK — not merely that the
            # files exist.
            assert _SlowFakeAdapter.STDOUT_MARKER in (round_dir / f"{name}.stdout").read_text()
            assert _SlowFakeAdapter.STDERR_MARKER in (round_dir / f"{name}.stderr").read_text()
            error_text = (round_dir / f"{name}.error.txt").read_text()
            assert "SubprocessTimeoutError" in error_text

        # Task 5: summary.md exists and references the right files.
        summary_path = result.artifacts.summary_path
        assert summary_path == round_dir / "summary.md"
        assert summary_path.is_file()
        summary_text = summary_path.read_text()
        assert "**Exit code:** 40 (REVIEWER_FAILURE)" in summary_text
        for name in ("rv1", "rv2"):
            assert f"### {name}" in summary_text
            assert f"{name}.error.txt" in summary_text  # link to the error file
            assert f"{name}.stdout" in summary_text

        # Task 4: lifecycle logging — phase narrative + summary on
        # stdout; the timed-out (failed) reviewer lines routed to stderr.
        captured = capsys.readouterr()
        assert "dispatching" in captured.out.lower()
        assert "run complete" in captured.out.lower()
        assert "FAILED (SubprocessTimeoutError)" in captured.err


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_public_surface_has_docstrings():
    import inspect

    assert inspect.getdoc(run_review)
    assert inspect.getdoc(RunResult)
    assert inspect.getdoc(RunArtifacts)
