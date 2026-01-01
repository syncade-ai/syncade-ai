"""Tests for :mod:`syncade.orchestrator` — test-leg / exit-code matrix.

Moved verbatim from ``tests/test_orchestrator.py`` (PR-R2 decomposition):
``TestSkipReasonPrecedence``, ``TestTestReRunActive``,
``TestComputeExitCodePr75Matrix``.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from syncade.synthesis import SynthesizerOutput
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _ship,
    _synth_with_blocker,
)

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestSkipReasonPrecedence:
    """R2.T2.7: when multiple skip conditions hold simultaneously,
    report the upstream causality reason (the actually-blocking
    failure), not the operator-opt-out reason.

    The pre-R2.T2.7 order checked ``test_command is None`` FIRST,
    so a run where both reviewer failed AND test_command was
    unset reported ``test_command_unset``. That misled operators
    into thinking their config was the issue when the actionable
    root cause was the reviewer failure."""

    def _config(self, test_command: str | None) -> SyncadeConfig:
        # PR-8: single-pass back-compat — these tests don't drive
        # multi-round behavior.
        loop: dict[str, object] = {"max_rounds": 1}
        if test_command is not None:
            loop["test_command"] = test_command
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop=loop,
        )

    def test_reviewer_failed_beats_test_command_unset(self, repo_with_pr_doc):
        """test_command unset + reviewer failure → skip_reason
        must be reviewer_failed (not test_command_unset)."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(
                canned_exception=ReviewerInvocationError(
                    "auth fail", returncode=1, stdout="", stderr=""
                )
            ),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(None),  # test_command UNSET
            adapter_factory=_factory_returning(*adapters),
        )
        assert result.test_skip_reason == "reviewer_failed", (
            f"reviewer-failed must outrank test_command_unset in the "
            f"skip-reason order; got {result.test_skip_reason!r}"
        )
        # Manifest agrees (R2.T2.6)
        manifest = json.loads(result.artifacts.manifest_path.read_text())
        assert manifest["test_skip_reason"] == "reviewer_failed"

    def test_synth_blocker_beats_test_command_unset(self, repo_with_pr_doc):
        """test_command unset + synth blocker → skip_reason must
        be synth_blocker (not test_command_unset)."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(None),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_output=_synth_with_blocker(reviewer_name="rv1")
            ),
        )
        assert result.test_skip_reason == "synth_blocker"

    def test_test_command_unset_only_when_pipeline_clean(self, repo_with_pr_doc):
        """When every upstream phase succeeded and the only
        reason the test leg didn't fire is the operator's opt-
        out, then-and-only-then report test_command_unset."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(None),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.test_skip_reason == "test_command_unset"


class TestTestReRunActive:
    """PR-7.5: the test re-run leg that was deferred in PR-7 now
    ships. Replaces the prior ``TestTestReRunDeferred`` pin.

    The leg is opt-in via ``[loop] test_command``. When set AND every
    reviewer succeeded AND the synth is clean, the orchestrator
    provisions one additional clean worktree, runs the operator-
    configured command via ``sh -c``, and folds the result into the
    mechanical verdict. When unset, the orchestrator skips the leg
    entirely — pre-PR-7.5 behavior is preserved.

    These tests use shell ``true`` / ``false`` / ``exit N`` commands
    rather than real test runners; the real-pytest smoke lives in
    ``tests/smoke/test_orchestrator_smoke.py``.
    """

    def _config_with_test_command(self, test_command: str | None) -> SyncadeConfig:
        """Two reviewers + an optional ``test_command`` on the loop
        config. ``None`` (the default arg) keeps the leg disabled.

        PR-8: pins ``max_rounds=1`` — the single-pass back-compat
        path the brief calls out for the TestTestReRunActive
        pinning regression. With ``max_rounds > 1`` these tests
        would hit the producer phase on NO-SHIP rounds and fail
        because no producer_adapter is injected. The brief is
        explicit: PR-7.5's exact code path runs under
        ``max_rounds=1``."""
        loop: dict[str, object] = {"max_rounds": 1}
        if test_command is not None:
            loop["test_command"] = test_command
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop=loop,
        )

    def test_unset_test_command_skips_leg_exit_0(self, repo_with_pr_doc):
        """The opt-in default. With ``test_command`` unset, the
        orchestrator never invokes ``run_tests`` and exit 0 reflects
        synth-clean only — matching pre-PR-7.5 behavior."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config_with_test_command(None),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 0
        assert result.test_result is None

    def test_test_command_passing_exit_0(self, repo_with_pr_doc):
        """Synth-clean + test-passed → exit 0. The mechanical
        verdict OR's synth_has_blocker with test.outcome=="failed";
        both False here, so SUCCESS."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config_with_test_command("true"),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 0
        assert result.test_result is not None
        assert result.test_result.outcome == "passed"
        assert result.test_result.exit_code == 0

    def test_test_command_failing_exit_30(self, repo_with_pr_doc):
        """Synth-clean + test-failed → exit 30. The test failure
        contributes the blocker that flips the verdict to NO-SHIP.

        Notable: this is NOT a ConsolidatedFinding (preserves the
        cannot-invent invariant). The test result lives in its own
        section parallel to consolidated_findings, and the
        mechanical verdict OR's the two signals."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config_with_test_command("false"),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 30
        assert result.test_result is not None
        assert result.test_result.outcome == "failed"
        assert result.test_result.exit_code == 1

    def test_test_command_subprocess_error_exit_40(self, repo_with_pr_doc):
        """Synth-clean + test-subprocess-error (timeout) → exit 40.
        Same bucket as a reviewer or synth subprocess failure;
        operator inspects ``test-run.{stdout,stderr}`` to
        disambiguate."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        # 30-second sleep with a 0.5s timeout = guaranteed SIGKILL.
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=SyncadeConfig(
                reviewers=[
                    {"name": "rv1", "provider": "fake1", "model": "x"},
                    {"name": "rv2", "provider": "fake2", "model": "y"},
                ],
                # PR-8 single-pass back-compat
                loop={
                    "test_command": "sleep 30",
                    "test_timeout_seconds": 0.5,
                    "max_rounds": 1,
                },
            ),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 40
        assert result.test_result is not None
        assert result.test_result.outcome == "subprocess_error"
        assert result.test_result.exit_code == -1

    def test_synth_blocker_skips_test_leg(self, repo_with_pr_doc):
        """Synth surfaces an active blocker → test leg skipped (no
        wasted compute when verdict is already NO-SHIP). Exit 30
        from synth alone; ``test_result`` is None."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config_with_test_command("false"),  # would fail if run
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_output=_synth_with_blocker(reviewer_name="rv1")
            ),
        )
        assert result.exit_code == 30
        assert result.test_result is None, (
            "test leg should have been skipped (synth had active "
            "blocker → exit 30 from synth alone); test_command was "
            "set to ``false`` which would have produced exit 30 too "
            "but via a different path. The skipped-leg signal is "
            "the None test_result."
        )

    def test_reviewer_failure_skips_test_leg(self, repo_with_pr_doc):
        """Reviewer failure → synth skipped → test leg skipped.
        Precedence chain extends. The exit code follows the reviewer
        phase (40 here); ``test_result`` is None."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(
                canned_exception=ReviewerInvocationError(
                    "auth fail", returncode=1, stdout="", stderr=""
                )
            ),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config_with_test_command("true"),  # would pass if run
            adapter_factory=_factory_returning(*adapters),
        )
        assert result.exit_code == 40
        assert result.test_result is None

    def test_test_worktree_uses_strip_files(self, repo_with_pr_doc):
        """The test worktree is the SAME mechanism as reviewer
        worktrees: ``WorktreeManager`` provisioned at
        ``snapshot.commit_sha`` with CLAUDE.md / AGENTS.md
        stripped. Plant a CLAUDE.md in the repo, run a test command
        that asserts the file is NOT visible from the worktree."""
        repo, pr_doc = repo_with_pr_doc
        (repo / "CLAUDE.md").write_text("producer guidance\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add CLAUDE.md"], cwd=repo, check=True)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        # Test passes (exit 0) iff CLAUDE.md is absent from cwd.
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config_with_test_command("test ! -f CLAUDE.md"),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 0, (
            "test leg sees CLAUDE.md — the strip_files treatment didn't "
            "apply. The test worktree must reuse the reviewer worktrees' "
            "blindness mechanism."
        )
        assert result.test_result is not None
        assert result.test_result.outcome == "passed"

    def test_test_command_uses_test_timeout_seconds_when_set(self, repo_with_pr_doc):
        """``test_timeout_seconds`` overrides ``timeout_seconds`` for
        the test leg only. Set test_timeout_seconds to a value that
        triggers timeout; the reviewer ``timeout_seconds`` (default
        1800) is unused for the test leg's cap."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=SyncadeConfig(
                reviewers=[
                    {"name": "rv1", "provider": "fake1", "model": "x"},
                    {"name": "rv2", "provider": "fake2", "model": "y"},
                ],
                # Reviewer timeout stays high; test timeout is short
                # so the sleep triggers it.
                # PR-8: single-pass back-compat.
                loop={
                    "test_command": "sleep 30",
                    "timeout_seconds": 1800,
                    "test_timeout_seconds": 0.5,
                    "max_rounds": 1,
                },
            ),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.test_result is not None
        assert result.test_result.outcome == "subprocess_error"
        assert result.exit_code == 40


class TestComputeExitCodePr75Matrix:
    """PR-7.5 task 3: direct unit tests of ``_compute_exit_code``'s
    new test-leg branches. The end-to-end matrix above exercises the
    same logic through ``run_review``; these tests pin the function-
    level contract so a future refactor can prove the matrix from
    the unit level alone."""

    def _ok_dispatch(self):
        """A DispatchResult with one successful reviewer."""
        from syncade.dispatcher import DispatchResult, ReviewerRunResult

        return DispatchResult(
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

    def _ok_synth(self):
        """A SynthesizerResult with no consolidated findings (clean)."""
        from syncade.synthesizer import SynthesizerResult

        return SynthesizerResult(
            output=SynthesizerOutput(
                consolidated_findings=[],
                synthesis_summary="clean",
            ),
            error=None,
            duration_seconds=1.0,
        )

    def _passed_test(self):
        from syncade.test_runner import TestRunResult

        return TestRunResult(
            exit_code=0,
            outcome="passed",
            duration_seconds=0.1,
            stdout="ok\n",
            stderr="",
            command="true",
        )

    def _failed_test(self):
        from syncade.test_runner import TestRunResult

        return TestRunResult(
            exit_code=1,
            outcome="failed",
            duration_seconds=0.1,
            stdout="",
            stderr="FAIL\n",
            command="false",
        )

    def _errored_test(self):
        from syncade.process import SubprocessTimeoutError
        from syncade.test_runner import TestRunResult

        return TestRunResult(
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=0.5,
            stdout="",
            stderr="",
            error=SubprocessTimeoutError("timed out", stdout="", stderr="", timeout=0.5),
            command="sleep 30",
        )

    def test_synth_clean_test_skipped_returns_success(self):
        from syncade.orchestrator import _compute_exit_code

        assert _compute_exit_code(self._ok_dispatch(), self._ok_synth(), None) == 0

    def test_synth_clean_test_passed_returns_success(self):
        from syncade.orchestrator import _compute_exit_code

        assert _compute_exit_code(self._ok_dispatch(), self._ok_synth(), self._passed_test()) == 0

    def test_synth_clean_test_failed_returns_findings_present(self):
        from syncade.orchestrator import _compute_exit_code

        assert _compute_exit_code(self._ok_dispatch(), self._ok_synth(), self._failed_test()) == 30

    def test_synth_clean_test_subprocess_error_returns_reviewer_failure(self):
        from syncade.orchestrator import _compute_exit_code

        assert _compute_exit_code(self._ok_dispatch(), self._ok_synth(), self._errored_test()) == 40

    def test_test_result_defaults_to_none(self):
        """The third positional/keyword arg defaults to None so the
        pre-PR-7.5 call sites (``_compute_exit_code(dispatch, synth)``)
        keep working. Smoke check that the default-None path is
        equivalent to the explicit-None path."""
        from syncade.orchestrator import _compute_exit_code

        assert _compute_exit_code(self._ok_dispatch(), self._ok_synth()) == _compute_exit_code(
            self._ok_dispatch(), self._ok_synth(), None
        )
