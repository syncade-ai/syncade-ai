from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _ship,
    _two_reviewer_config,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestSynthesizerNarrowExceptionHandling:
    """QA fix #9 (P1.4): ``run_synthesizer`` should bucket only
    expected build_invocation failures (ValueError from provider /
    permissions validation) into a clean SynthesizerResult. Genuine
    programming bugs (TypeError, AttributeError, etc.) should
    propagate as crashes — burying them as exit-40 with a polite
    ``synthesizer.error.txt`` hides bugs that need stack traces.
    """

    def _adapter_raising(self, exc_instance):
        """Build an adapter whose build_invocation raises a given
        exception. Used to exercise both the expected ValueError path
        and the programming-bug-propagation path."""

        class _RaisingAdapter:
            name = "openai"

            def __init__(self):
                self.invocations = []

            def build_invocation(self, reviewer_config, worktree_path, prompt):
                self.invocations.append((reviewer_config, worktree_path, prompt))
                raise exc_instance

            def extract_final_text(self, result, *, empty_output_exception_class):
                # Unreachable in these tests — build_invocation raises
                # before we'd ever call this.
                del result, empty_output_exception_class
                return ""

        return _RaisingAdapter()

    def test_value_error_from_build_invocation_bucketed_as_synth_failure(self, repo_with_pr_doc):
        """ValueError is the expected exception class — surfaces as
        SynthesizerResult.error → exit 40 → synthesizer.error.txt
        persisted. The CLI keeps running; the operator gets a
        polite failure summary."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._adapter_raising(ValueError("provider mismatch")),
        )
        assert result.exit_code == 40
        assert result.synth_result is not None
        assert isinstance(result.synth_result.error, ValueError)
        # The polite failure mode produces an error.txt for the operator.
        assert (result.artifacts.round_dir / "synthesizer.error.txt").is_file()

    def test_type_error_from_build_invocation_propagates_as_crash(self, repo_with_pr_doc):
        """TypeError is a programming bug — should crash the run,
        not get bucketed as exit 40. The unprotected-bubble-up is
        the right behavior here; the user needs the stack trace,
        not a polite synthesizer.error.txt that hides the bug.
        """
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        with pytest.raises(TypeError, match="programming bug"):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(*adapters),
                synthesizer_adapter=self._adapter_raising(TypeError("programming bug")),
            )

    def test_attribute_error_from_build_invocation_propagates_as_crash(self, repo_with_pr_doc):
        """AttributeError: same as TypeError — should NOT be
        absorbed as a polite synth failure."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        with pytest.raises(AttributeError, match="programming bug"):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(*adapters),
                synthesizer_adapter=self._adapter_raising(AttributeError("programming bug")),
            )


class TestSynthesizerExtractorBroadCatch:
    """R2.2 regression: unexpected exceptions from the extractor
    (``extract_final_text``) must NOT bubble out of
    ``run_review``. They must be caught, mapped to a polite
    ``SynthesizerResult``, and the orchestrator must continue to
    persist all artifacts (manifest.json, summary.md,
    synthesizer.error.txt, synthesizer.stdout/.stderr) so the
    operator has something to diagnose with.

    Asymmetric vs ``TestSynthesizerNarrowExceptionHandling``: the
    extractor consumes stdout from a foreign codex subprocess (the
    input is not controlled by our code), so unexpected exception
    shapes are more plausibly triggered by model-output variation
    than by programming bugs. build_invocation gets a narrow
    catch (only ValueError); extract_final_text gets
    a broad catch.
    """

    def _adapter_extract_raising(self, exc_instance):
        """Build a synth adapter whose ``extract_final_text``
        raises a given exception. ``build_invocation`` is a no-op
        that succeeds so the subprocess output exists by the time
        extract raises — exactly the failure mode R2.2 fixed."""
        import os

        from syncade.adapters.base import Invocation
        from syncade.adapters.fake import _noop_argv

        class _ExtractRaisingAdapter:
            name = "openai"

            def __init__(self):
                self.invocations = []

            def build_invocation(self, reviewer_config, worktree_path, prompt):
                self.invocations.append((reviewer_config, worktree_path, prompt))
                return Invocation(
                    argv=_noop_argv(),
                    cwd=worktree_path,
                    env=dict(os.environ),
                    stdin_text=None,
                    timeout_seconds=None,
                )

            def extract_final_text(self, result, *, empty_output_exception_class):
                del result, empty_output_exception_class
                raise exc_instance

        return _ExtractRaisingAdapter()

    def test_value_error_from_extract_maps_to_exit_40_with_artifacts(self, repo_with_pr_doc):
        """ValueError raised by the extractor used to bubble out of
        run_review (it's not in the typed ``(ReviewerInvocationError,
        SynthesizerOutputError)`` tuple). R2.2's broad catch
        converts it to a polite SynthesizerResult → exit 40 + all
        artifacts on disk."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._adapter_extract_raising(
                ValueError("bizarre JSONL shape from codex")
            ),
        )
        # Exit 40 (defensive bucket — not SynthesizerOutputError,
        # not a typed subprocess error).
        assert result.exit_code == 40
        assert result.synth_result is not None
        assert isinstance(result.synth_result.error, ValueError)
        # All persistence artifacts present.
        round_dir = result.artifacts.round_dir
        assert round_dir.is_dir()
        assert (round_dir / "manifest.json").is_file()
        assert (round_dir / "summary.md").is_file()
        assert (round_dir / "synthesizer.error.txt").is_file()
        assert (round_dir / "synthesizer.stdout").is_file()
        assert (round_dir / "synthesizer.stderr").is_file()
        # findings.md is NOT written (synth_result.output is None).
        assert not (round_dir / "findings.md").exists()
        # error.txt names the original exception class so the
        # operator can route by failure shape.
        assert "ValueError" in (round_dir / "synthesizer.error.txt").read_text()

    def test_attribute_error_from_extract_maps_to_exit_40_with_artifacts(self, repo_with_pr_doc):
        """AttributeError from the extractor (e.g. ``event.get`` on
        an unexpected non-dict object): same treatment as
        ValueError. Pinning that the broad catch covers the full
        Exception hierarchy, not just ValueError."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._adapter_extract_raising(
                AttributeError("unexpected non-dict event")
            ),
        )
        assert result.exit_code == 40
        assert isinstance(result.synth_result.error, AttributeError)
        assert (result.artifacts.round_dir / "synthesizer.error.txt").is_file()

    def test_synthesizer_output_error_from_extract_still_maps_to_exit_70(self, repo_with_pr_doc):
        """The expected case still routes correctly: SynthesizerOutputError
        from extract → exit 70 (not the new exit-40 broad-catch bucket).
        The order-of-except matters; pin it."""
        from syncade.synthesis import SynthesizerOutputError

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._adapter_extract_raising(
                SynthesizerOutputError("no agent_message")
            ),
        )
        # SynthesizerOutputError → exit 70 (the typed branch wins).
        assert result.exit_code == 70
        assert isinstance(result.synth_result.error, SynthesizerOutputError)
