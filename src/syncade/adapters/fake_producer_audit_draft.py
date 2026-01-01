"""Producer / spec-auditor / spec-drafter test doubles.

:class:`FakeProducerAdapter` (the :class:`~syncade.adapters.producer.ProducerAdapter`
double, with the optional fixture-commit that drives the orchestrator's SHA-based
stall detection), :class:`FakeAuditorAdapter`, and :class:`FakeDrafterAdapter`,
plus their canned-output defaults. Re-exported from ``fake.py``.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from syncade.adapters.base import Invocation
from syncade.adapters.producer import ProducerOutput
from syncade.config import ProducerConfig, ReviewerConfig
from syncade.process import SubprocessResult
from syncade.worktree_env import worktree_scoped_env

from .fake_common import _noop_argv

if TYPE_CHECKING:
    from syncade.spec_audit import SpecAuditOutput
    from syncade.spec_draft import SpecDraftOutput


def _default_canned_producer_output() -> ProducerOutput:
    """The conventional "I fixed it" narrative used when callers don't
    supply a ``canned_output``. Returned as a fresh instance each
    call so mutation by one test doesn't leak into the next.

    Empty / minimal narrative is intentional — the orchestrator's
    stall detection is SHA-based, not narrative-based, so test
    cases that exercise "producer committed" need a non-empty SHA
    move on the worktree but can leave the narrative spartan.
    """
    return ProducerOutput(narrative_text="fake producer canned: addressed the findings")


class FakeProducerAdapter:
    """In-memory test double for :class:`ProducerAdapter`.

    Configured at construction time. The test double can optionally
    write a fixture commit to the producer worktree on
    ``build_invocation`` so the orchestrator's stall detection
    sees the worktree HEAD move — without this, every fake-
    backed orchestrator test would have to manually script the
    worktree's git state and ``run_producer``'s "did HEAD move?"
    check would always report "stalled".

    The fixture commit is written by ``build_invocation``, not by
    ``parse_output``, because the orchestrator dispatches the
    subprocess BETWEEN those calls. Pre-dispatch the worktree is
    at the round-start SHA; post-dispatch (where the real
    producer would have made its edits) the worktree should have
    new commits. Mimicking that in build_invocation produces the
    same SHA-move signal :func:`syncade.producer.run_producer`
    detects without ever spawning a subprocess.

    Args:
        canned_output: The :class:`ProducerOutput` to return from
            ``parse_output``. Defaults to a short canned narrative.
        canned_exception: If supplied, ``parse_output`` raises this
            instead of returning. Use to exercise the producer
            module's failure paths
            (:class:`~syncade.adapters.base.ReviewerInvocationError`
            for subprocess-side failure → exit 40;
            :class:`~syncade.findings.ReviewerOutputError` for
            unparseable producer output, also exit 40 since the
            producer has no separate parse-failure exit code).
        record_invocations: If True (default), every
            ``build_invocation`` call's ``(producer_config,
            worktree_path, prompt)`` tuple is appended to
            ``self.invocations``.
        commit_message: When non-None, ``build_invocation`` writes
            a fixture commit with this subject to the worktree. The
            commit creates a file ``.syncade-fake-producer-N``
            (numbered per call to allow multiple commits) and
            stages + commits it. When ``None`` (default), no commit
            is written — the worktree HEAD stays at the round-start
            SHA, which simulates the producer-stall path in
            orchestrator tests.
        canned_auth_exception: If supplied, ``check_auth`` raises
            this. Same auth-fail simulation pattern as
            :class:`FakeAdapter`.
    """

    name = "fake-producer"

    def __init__(
        self,
        canned_output: ProducerOutput | None = None,
        canned_exception: Exception | None = None,
        record_invocations: bool = True,
        commit_message: str | None = None,
        canned_auth_exception: Exception | None = None,
    ) -> None:
        self.canned_output = (
            canned_output if canned_output is not None else _default_canned_producer_output()
        )
        self.canned_exception = canned_exception
        self.canned_auth_exception = canned_auth_exception
        self.record_invocations = record_invocations
        self.commit_message = commit_message
        self.invocations: list[tuple[ProducerConfig, Path, str]] = []
        self.check_auth_calls = 0
        self.parse_output_calls = 0
        # Counter used to name fixture commits uniquely when
        # build_invocation runs more than once against the same
        # worktree (which is the multi-commit producer scenario).
        self._fixture_commit_index = 0
        # Producer dispatch is single-threaded per round (one
        # producer subprocess at a time), so the lock the reviewer
        # fake carries isn't strictly needed here — but it's cheap
        # and protects against a future caller that drives multiple
        # FakeProducerAdapter instances concurrently in one test.
        self._lock = threading.Lock()

    def build_invocation(
        self,
        producer_config: ProducerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Record the call, optionally write a fixture commit to the
        worktree, and return an :class:`Invocation` pointing at a
        no-op shell command.

        The fixture-commit branch is what lets orchestrator tests
        exercise the producer-committed path without spawning a
        real subprocess: when ``commit_message`` is set,
        ``build_invocation`` writes a fixture file under the
        worktree (``.syncade-fake-producer-<N>``) and runs
        ``git add`` + ``git commit -m <message>`` from the
        worktree. The HEAD-moves-vs-stays signal
        :func:`syncade.producer.run_producer` reads is then the
        same shape a real producer would produce.

        When ``commit_message`` is None, no commit is written —
        the worktree stays at the round-start SHA, which is the
        stall path.

        Raises:
            subprocess.CalledProcessError: If the fixture-commit
                ``git`` calls fail (no git on PATH, the worktree
                isn't a git working tree, etc.). The fake raises
                rather than swallowing so the test failure surface
                is legible — these failures indicate a broken
                test fixture, not a real-world condition the
                production code path would encounter.
        """
        if self.record_invocations:
            with self._lock:
                self.invocations.append((producer_config, worktree_path, prompt))
        if self.commit_message is not None:
            self._write_fixture_commit(worktree_path)
        return Invocation(
            argv=_noop_argv(),
            cwd=worktree_path,
            # mirror the real producer adapters' worktree-scoped env.
            env=worktree_scoped_env(worktree_path),
            stdin_text=None,
            timeout_seconds=None,
        )

    def _write_fixture_commit(self, worktree_path: Path) -> None:
        """Write a fixture commit on top of the worktree's current HEAD.

        Used to simulate a real producer making a commit during its
        subprocess run. The fixture file's basename includes a
        per-call index so multiple ``build_invocation`` calls
        against the same worktree produce multiple distinct
        commits (the multi-commit producer scenario described in
        the producer prompt template).
        """
        import subprocess

        with self._lock:
            idx = self._fixture_commit_index
            self._fixture_commit_index += 1
        fixture_file = worktree_path / f".syncade-fake-producer-{idx}"
        fixture_file.write_text(
            f"FakeProducerAdapter fixture commit {idx} — produced by tests\n",
            encoding="utf-8",
        )
        # The fake's two git invocations bypass syncade.process so the
        # test double stays out of the production subprocess machinery.
        # Failures bubble as CalledProcessError, which the test surface
        # treats as a broken fixture (see docstring).
        subprocess.run(
            ["git", "add", fixture_file.name],
            cwd=worktree_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # Allow the test's commit to land regardless of the
        # repo-level user.email/user.name config (which may be
        # absent in CI containers); --author embeds the metadata
        # without touching repo state. The author string mimics the
        # real-producer convention so commit-history inspection in
        # tests reads naturally.
        commit_msg = self.commit_message or "fix: fake producer commit"
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=fake-producer@syncade.test",
                "-c",
                "user.name=Fake Producer",
                "commit",
                "-m",
                commit_msg,
            ],
            cwd=worktree_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def parse_output(self, result: SubprocessResult) -> ProducerOutput:
        """Return the canned output, or raise the canned exception.

        Increments ``self.parse_output_calls`` so tests that need
        to assert "this code path was reached" can do so directly.
        The ``result`` argument is ignored — same pattern as
        :class:`FakeAdapter.parse_output`.
        """
        with self._lock:
            self.parse_output_calls += 1
        if self.canned_exception is not None:
            raise self.canned_exception
        return self.canned_output

    def check_auth(self) -> None:
        """Record the call and raise the canned auth exception if set."""
        with self._lock:
            self.check_auth_calls += 1
        if self.canned_auth_exception is not None:
            raise self.canned_auth_exception


# ---------------------------------------------------------------------------
# Spec auditor test double
# ---------------------------------------------------------------------------


def _default_canned_audit_output() -> SpecAuditOutput:
    """The conventional zero-finding READY verdict used when callers
    don't supply a ``canned_output``. Returned as a fresh instance each
    call so mutation by one test doesn't leak into the next.

    Imported lazily to avoid a circular import — spec_audit imports from
    prompts, which is clean, but making this module unconditionally import
    from spec_audit at load time would add an extra layer to the import
    graph.
    """
    from syncade.spec_audit import SpecAuditOutput

    return SpecAuditOutput(
        verdict="READY",
        findings=[],
        summary="fake auditor canned READY",
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


class FakeAuditorAdapter:
    """In-memory test double for the adapter surface that
    :func:`syncade.spec_audit.run_spec_audit` depends on.

    Mirrors :class:`FakeSynthesizerAdapter` — same two-method surface
    (``build_invocation`` + ``extract_final_text``), same
    "produce canned output as ``model_dump_json()`` so the real parser
    path is exercised" strategy.

    Args:
        canned_output: The :class:`~syncade.spec_audit.SpecAuditOutput`
            the fake produces when the audit runs successfully. Default:
            a zero-finding READY verdict.
        canned_exception: If supplied,
            ``extract_final_text`` raises this instead of
            returning text. Use to exercise the spec audit module's
            failure paths — e.g. pass
            :class:`~syncade.spec_audit.SpecAuditOutputError` to
            simulate a parse failure (exit 70), or
            :class:`~syncade.adapters.base.ReviewerInvocationError`
            to simulate a codex subprocess failure (exit 40).
        record_invocations: If True (default), every
            ``build_invocation`` call's ``(reviewer_config,
            worktree_path, prompt)`` tuple is appended to
            ``self.invocations`` for test inspection.
    """

    name = "openai"
    """Matches the codex provider key so the synthetic ReviewerConfig
    :mod:`syncade.spec_audit` constructs validates cleanly inside
    :meth:`CodexAdapter.build_invocation`."""

    def __init__(
        self,
        canned_output: SpecAuditOutput | None = None,
        canned_exception: Exception | None = None,
        record_invocations: bool = True,
    ) -> None:
        self._canned_output_arg = canned_output
        self.canned_exception = canned_exception
        self.record_invocations = record_invocations
        self.invocations: list[tuple[ReviewerConfig, Path, str]] = []
        self.extract_calls = 0

    @property
    def canned_output(self) -> SpecAuditOutput:
        if self._canned_output_arg is not None:
            return self._canned_output_arg
        return _default_canned_audit_output()

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Record the call and return a no-op :class:`Invocation`.

        ``worktree_path`` is the cold isolation workspace created by
        :func:`syncade.spec_audit.run_spec_audit`, not repo_root.
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
        """Return the canned output as JSON text, or raise the canned exception.

        Produces text via ``model_dump_json()`` so
        :func:`syncade.spec_audit.parse_spec_audit_output` is exercised
        end-to-end in tests. ``empty_output_exception_class`` is
        accepted for protocol symmetry but ignored.
        """
        del result, empty_output_exception_class
        self.extract_calls += 1
        if self.canned_exception is not None:
            raise self.canned_exception
        return self.canned_output.model_dump_json()


def _default_canned_draft_output() -> SpecDraftOutput:
    """The conventional minimal one-criterion draft used when callers don't supply
    a ``canned_output``. Fresh instance each call (no cross-test mutation). Lazy
    import to keep this module off spec_draft at load time (same rationale as the
    auditor default)."""
    from syncade.spec_draft import Criterion, SpecDraftOutput

    return SpecDraftOutput(
        proposal="fake drafter canned: a small update the user asked for",
        acceptance_criteria=[Criterion(text="the asked-for behavior works", origin="transcribed")],
        deltas=[],
        assumptions=[],
    )


class FakeDrafterAdapter:
    """In-memory test double for the adapter surface that
    :func:`syncade.spec_draft.run_spec_draft` depends on.

    Mirrors :class:`FakeAuditorAdapter` (same two-method surface, same
    "produce canned output as ``model_dump_json()`` so the real parser runs"
    strategy), plus a ``canned_text`` escape hatch for exercising the parse-failure
    path with arbitrary malformed stdout.

    Args:
        canned_output: The :class:`~syncade.spec_draft.SpecDraftOutput` produced on
            success (default: a minimal one-criterion draft).
        canned_text: If supplied, ``extract_final_text`` returns this
            RAW string instead of ``canned_output.model_dump_json()`` — use to feed
            malformed text and exercise the exit-70 parse-failure path.
        canned_exception: If supplied, ``extract_final_text`` raises
            it (e.g. ``ReviewerInvocationError`` → exit-40 subprocess failure).
        record_invocations: If True (default), each ``build_invocation`` call's
            ``(reviewer_config, worktree_path, prompt)`` is appended to
            ``self.invocations``.
    """

    name = "openai"
    """Matches the codex provider key so the synthetic ReviewerConfig validates."""

    def __init__(
        self,
        canned_output: SpecDraftOutput | None = None,
        canned_text: str | None = None,
        canned_exception: Exception | None = None,
        record_invocations: bool = True,
    ) -> None:
        self._canned_output_arg = canned_output
        self.canned_text = canned_text
        self.canned_exception = canned_exception
        self.record_invocations = record_invocations
        self.invocations: list[tuple[ReviewerConfig, Path, str]] = []
        self.extract_calls = 0

    @property
    def canned_output(self) -> SpecDraftOutput:
        if self._canned_output_arg is not None:
            return self._canned_output_arg
        return _default_canned_draft_output()

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Record the call and return a no-op :class:`Invocation`. ``worktree_path``
        is the drafter's cold isolation workspace, not repo_root."""
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
        """Return the canned text/output, or raise the canned exception.
        ``empty_output_exception_class`` is accepted for protocol symmetry but
        ignored (pass a ``canned_exception`` to simulate that case)."""
        del result, empty_output_exception_class
        self.extract_calls += 1
        if self.canned_exception is not None:
            raise self.canned_exception
        if self.canned_text is not None:
            return self.canned_text
        return self.canned_output.model_dump_json()
