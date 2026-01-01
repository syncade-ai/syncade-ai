"""Reviewer + synthesizer test doubles.

:class:`FakeAdapter` and :class:`FakeSynthesizerAdapter` cover the reviewer and
synthesizer adapter surfaces, plus their zero-finding canned-output defaults.
Re-exported from ``fake.py``.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from syncade.adapters.base import Invocation
from syncade.config import ReviewerConfig
from syncade.findings import ReviewerOutput
from syncade.process import SubprocessResult
from syncade.synthesis import SynthesizerOutput
from syncade.worktree_env import worktree_scoped_env

from .fake_common import _noop_argv


def _default_canned_output() -> ReviewerOutput:
    """The conventional zero-finding SHIP verdict used when callers don't
    supply a ``canned_output``. Returned as a fresh instance each call so
    mutation by one test doesn't leak into the next.

    The narrative-surface fields (``summary``, ``priority_order``,
    ``coverage_gaps``, ``dismissed_concerns``) are required on
    :class:`ReviewerOutput`, so the test default populates a minimal
    valid combination: a non-empty ``summary``, an empty
    ``priority_order`` (correct for zero findings), and empty
    coverage/dismissal lists.
    """
    return ReviewerOutput(
        verdict="SHIP",
        findings=[],
        summary="fake reviewer canned SHIP",
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


class FakeAdapter:
    """In-memory test double for :class:`ReviewerAdapter`.

    Configured at construction time. After use, ``self.invocations``
    holds every ``build_invocation`` call's arguments (if recording is
    enabled), which dispatcher tests can inspect to assert correct
    fan-out behavior.

    Args:
        canned_output: The :class:`ReviewerOutput` to return from
            ``parse_output``. Defaults to a zero-finding SHIP verdict.
        canned_exception: If supplied, ``parse_output`` raises this
            instead of returning. Use to exercise dispatcher
            failure-handling paths (for example reviewer invocation or output
            parse errors).
        record_invocations: If True (default), every ``build_invocation``
            call is appended to ``self.invocations`` as a tuple of
            ``(reviewer_config, worktree_path, prompt)``. Disable to
            save memory in tests that fan out heavily.
        canned_auth_exception: If supplied, ``check_auth`` raises this.
            Use to test dispatcher auth-fail-fast behavior without
            spawning a real subprocess. ``check_auth`` always
            increments ``self.check_auth_calls`` regardless, so
            dispatcher tests can assert the pre-flight phase ran.
    """

    name = "fake"

    def __init__(
        self,
        canned_output: ReviewerOutput | None = None,
        canned_exception: Exception | None = None,
        record_invocations: bool = True,
        canned_auth_exception: Exception | None = None,
    ) -> None:
        self.canned_output = (
            canned_output if canned_output is not None else _default_canned_output()
        )
        self.canned_exception = canned_exception
        self.canned_auth_exception = canned_auth_exception
        self.record_invocations = record_invocations
        self.invocations: list[tuple[ReviewerConfig, Path, str]] = []
        self.check_auth_calls = 0
        self.parse_output_calls = 0
        # Guards check_auth_calls, parse_output_calls, and invocations
        # against races when the dispatcher calls into this adapter
        # from multiple worker threads. See the fake module docstring.
        self._lock = threading.Lock()

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Record the call and return a no-op shell :class:`Invocation`.

        The returned Invocation has ``cwd = worktree_path`` and the
        caller's environment so a dispatcher that actually executes it
        still gets a clean exit-0 SubprocessResult on platforms where
        ``/bin/true`` (or ``cmd /c exit 0``) exists.
        """
        if self.record_invocations:
            with self._lock:
                self.invocations.append((reviewer_config, worktree_path, prompt))
        return Invocation(
            argv=_noop_argv(),
            cwd=worktree_path,
            # mirror the real reviewer adapters' worktree-scoped env so
            # this double stays faithful. (The synth/auditor fakes deliberately
            # keep the bare env — their real counterparts run cold in isolated
            # tempdirs, not worktrees.)
            env=worktree_scoped_env(worktree_path),
            stdin_text=None,
            timeout_seconds=None,
        )

    def parse_output(self, result: SubprocessResult) -> ReviewerOutput:
        """Return the canned output, or raise the canned exception.

        Increments ``self.parse_output_calls`` so tests that need to
        assert "this code path was (not) reached" — notably the
        auth-fail-fast tests, which must verify that NO reviewer's
        parse_output ran when pre-flight failed — can do so directly
        rather than inferring it from ``invocations``.

        The ``result`` argument is ignored entirely — that's the whole
        point of the fake. If you need parse behavior that varies with
        ``result``, write a real adapter or a custom subclass.
        """
        with self._lock:
            self.parse_output_calls += 1
        if self.canned_exception is not None:
            raise self.canned_exception
        return self.canned_output

    def extract_final_text(
        self,
        result: SubprocessResult,
        *,
        empty_output_exception_class: type[Exception],
    ) -> str:
        """Return the canned output as JSON text, or raise the canned
        exception.

        Serializing via ``model_dump_json`` (rather than returning some
        placeholder string) keeps the identity a real adapter guarantees:
        ``parse_output(r)`` == ``parse_reviewer_output(extract_final_text(r))``.
        A fake that broke that identity would let a caller pass tests against
        the fake and fail against ``claude``.

        ``empty_output_exception_class`` is accepted for protocol symmetry but
        ignored — the fake never has empty output. To simulate that case, pass
        the relevant error as ``canned_exception``.

        Deliberately does NOT touch ``parse_output_calls``: the auth-fail-fast
        dispatcher tests assert that counter is 0 to prove no reviewer parsed
        anything, and a different method inflating it would quietly rot that
        assertion.
        """
        del result, empty_output_exception_class  # explicitly unused
        if self.canned_exception is not None:
            raise self.canned_exception
        return self.canned_output.model_dump_json()

    def check_auth(self) -> None:
        """Record the call and raise the canned auth exception if set.

        Increments ``self.check_auth_calls`` on every invocation so
        dispatcher tests can assert the pre-flight phase ran exactly
        once per adapter. If ``canned_auth_exception`` was supplied at
        construction time, it's raised here — this is how dispatcher
        tests simulate "one reviewer's auth is broken" without spawning
        real subprocesses.
        """
        with self._lock:
            self.check_auth_calls += 1
        if self.canned_auth_exception is not None:
            raise self.canned_auth_exception


# ---------------------------------------------------------------------------
# Synthesizer test double
# ---------------------------------------------------------------------------


def _default_canned_synth_output() -> SynthesizerOutput:
    """The conventional zero-finding SynthesizerOutput used when
    callers don't supply a ``canned_output``. Returned as a fresh
    instance each call so mutation by one test doesn't leak into the
    next.
    """
    return SynthesizerOutput(
        consolidated_findings=[],
        synthesis_summary="fake synthesizer canned: nothing to consolidate",
    )


class FakeSynthesizerAdapter:
    """In-memory test double for the synthesizer codex adapter shape.

    The synthesizer module's adapter contract is narrower than the
    full :class:`~syncade.adapters.base.ReviewerAdapter` Protocol —
    it only calls :meth:`build_invocation` and
    :meth:`extract_final_text`. This fake implements that subset.

    The fake's ``extract_final_text`` produces text that
    will round-trip through :func:`syncade.synthesis.parse_synthesizer_output`
    to yield ``canned_output``. That keeps the test exercising the
    real parser path (so a regression in
    ``parse_synthesizer_output`` surfaces in tests that USE
    ``FakeSynthesizerAdapter`` even though they aren't reaching for a
    parser-specific assertion).

    Args:
        canned_output: The :class:`SynthesizerOutput` the fake produces
            when the synthesizer runs successfully. Default: an empty
            consolidation with a non-empty summary string.
        canned_exception: If supplied,
            ``extract_final_text`` raises this instead of
            returning text. Use to exercise the synthesizer module's
            failure paths. Pass :class:`syncade.synthesis.SynthesizerOutputError`
            to simulate a parse failure, or a reviewer invocation error to
            simulate a codex subprocess failure.
        record_invocations: If True (default), every
            ``build_invocation`` call's ``(reviewer_config,
            worktree_path, prompt)`` tuple is appended to
            ``self.invocations`` for test inspection.
    """

    name = "openai"  # so CodexAdapter.build_invocation's provider validation passes
    """Matches the codex provider key used by the synthetic ReviewerConfig.

    The fake adapter does not need this attribute itself; keeping it aligned
    with ``CodexAdapter`` makes test debugging simpler.
    """

    def __init__(
        self,
        canned_output: SynthesizerOutput | None = None,
        canned_exception: Exception | None = None,
        record_invocations: bool = True,
    ) -> None:
        self.canned_output = (
            canned_output if canned_output is not None else _default_canned_synth_output()
        )
        self.canned_exception = canned_exception
        self.record_invocations = record_invocations
        self.invocations: list[tuple[ReviewerConfig, Path, str]] = []
        self.extract_calls = 0

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Record the call and return an :class:`Invocation` pointing
        at a no-op shell command so ``run_subprocess`` still produces a clean
        exit-0 :class:`SubprocessResult`.

         clarification: ``worktree_path`` is the **synthesizer's
        cold-isolation tempdir workspace**, NOT ``repo_root``. As of
         + , ``run_synthesizer`` provisions an isolated
        tempdir, copies the PR doc into it, git-inits it, and passes
        the WORKSPACE path here. Tests asserting on the recorded
        path should check the ``syncade-synth-`` tempdir prefix
        (see ``test_synthesizer_runs_in_isolated_tempdir_workspace``).
        """
        if self.record_invocations:
            self.invocations.append((reviewer_config, worktree_path, prompt))
        return Invocation(
            argv=_noop_argv(),
            cwd=worktree_path,
            env=dict(os.environ),
            stdin_text=None,
            timeout_seconds=None,
        )

    def extract_final_text(
        self,
        result: SubprocessResult,
        *,
        empty_output_exception_class: type[Exception],
    ) -> str:
        """Return the canned output as JSON text, or raise the canned
        exception.

        The synthesizer module calls this immediately after
        ``run_subprocess``; the returned text is then fed to
        :func:`syncade.synthesis.parse_synthesizer_output`. Producing the canned
        output via ``model_dump_json`` exercises the parser end-to-end.

        ``empty_output_exception_class`` is accepted for protocol
        symmetry with :meth:`CodexAdapter.extract_final_text`
        but ignored — the fake never has an empty agent_message; if
        the caller wants to simulate that case, they pass an instance
        of :class:`SynthesizerOutputError` as ``canned_exception``
        directly.
        """
        del result, empty_output_exception_class  # explicitly unused
        self.extract_calls += 1
        if self.canned_exception is not None:
            raise self.canned_exception
        return self.canned_output.model_dump_json()
