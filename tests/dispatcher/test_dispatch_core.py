"""Tests for :mod:`syncade.dispatcher` — happy path, partial failure,
auth fail-fast, and timeout handling.

All tests use :class:`FakeAdapter` (or trivial subclasses) via the
``adapter_factory`` injection point. No real CLIs spawned; only the
no-op subprocess ``/bin/true`` (whatever ``FakeAdapter._noop_argv()``
returns) actually executes, except for the timeout test which uses
``sleep``.
"""

from __future__ import annotations

import time
from pathlib import Path

from syncade.adapters.base import (
    Invocation,
    ReviewerInvocationError,
)
from syncade.adapters.fake import FakeAdapter
from syncade.config import ReviewerConfig
from syncade.dispatcher import (
    DispatchResult,
    dispatch_reviewers,
)
from syncade.findings import ReviewerOutputError
from tests.dispatcher._helpers import (
    _config,
    _factory_returning,
    _no_ship_with_finding,
    _ship,
    _worktree_paths,
)

# ---------------------------------------------------------------------------
# Happy / failure-mix paths
# ---------------------------------------------------------------------------


class TestDispatchHappyPath:
    def test_two_reviewers_both_succeed(self, tmp_path: Path):
        configs = [_config("rv1"), _config("rv2")]
        adapters = [FakeAdapter(canned_output=_ship()) for _ in configs]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", "rv2", tmp_path=tmp_path),
            prompt="rendered prompt",
            adapter_factory=_factory_returning(*adapters),
        )
        assert isinstance(result, DispatchResult)
        assert result.all_succeeded
        assert len(result.successes) == 2
        assert len(result.failures) == 0
        # Each ReviewerRunResult maps to its input config by name
        assert result.results[0].reviewer_name == "rv1"
        assert result.results[1].reviewer_name == "rv2"
        assert result.results[0].output is not None
        assert result.results[0].output.verdict == "SHIP"

    def test_reviewer_name_propagates_not_provider(self, tmp_path: Path):
        """Important invariant: the orchestrator routes findings back
        by reviewer_name (from ReviewerConfig.name), not by provider.
        Two reviewers from the same provider need distinguishable
        names, and that name must make it into the run result."""
        configs = [
            _config("claude-primary", provider="fake"),
            _config("claude-secondary", provider="fake"),
        ]
        adapters = [FakeAdapter() for _ in configs]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("claude-primary", "claude-secondary", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(*adapters),
        )
        names = [r.reviewer_name for r in result.results]
        assert names == ["claude-primary", "claude-secondary"]
        providers = [r.provider for r in result.results]
        assert providers == ["fake", "fake"]


class TestDispatchPartialFailure:
    def test_one_fails_others_succeed(self, tmp_path: Path):
        """One reviewer's parse_output raises; siblings produce
        ReviewerOutput. No silent degradation — both results appear,
        one as a failure."""
        configs = [_config("rv1"), _config("rv2"), _config("rv3")]
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(
                canned_exception=ReviewerInvocationError(
                    "model unavailable",
                    returncode=1,
                    stdout="",
                    stderr="",
                )
            ),
            FakeAdapter(canned_output=_no_ship_with_finding()),
        ]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", "rv2", "rv3", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(*adapters),
        )
        assert not result.all_succeeded
        assert len(result.successes) == 2
        assert len(result.failures) == 1
        # The middle reviewer is the failure
        failure = result.failures[0]
        assert failure.reviewer_name == "rv2"
        assert isinstance(failure.error, ReviewerInvocationError)

    def test_all_fail(self, tmp_path: Path):
        configs = [_config("rv1"), _config("rv2")]
        adapters = [
            FakeAdapter(canned_exception=ReviewerOutputError("bad output")),
            FakeAdapter(
                canned_exception=ReviewerInvocationError(
                    "auth gone",
                    returncode=1,
                    stdout="",
                    stderr="",
                )
            ),
        ]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", "rv2", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(*adapters),
        )
        assert not result.all_succeeded
        assert len(result.successes) == 0
        assert len(result.failures) == 2
        # Each failure carries its own distinct exception
        assert isinstance(result.failures[0].error, ReviewerOutputError)
        assert isinstance(result.failures[1].error, ReviewerInvocationError)

    def test_parse_output_error_recorded_cleanly(self, tmp_path: Path):
        """ReviewerOutputError (exit 70) is a recognized failure
        category and must surface intact in the result, not get
        flattened to a generic Exception."""
        configs = [_config("rv1")]
        adapters = [FakeAdapter(canned_exception=ReviewerOutputError("unparseable"))]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(*adapters),
        )
        assert isinstance(result.failures[0].error, ReviewerOutputError)

    def test_transient_api_failure_retried_once_then_succeeds(self, tmp_path: Path):
        class FlakyAdapter(FakeAdapter):
            def parse_output(self, result):  # type: ignore[override]
                with self._lock:
                    self.parse_output_calls += 1
                    calls = self.parse_output_calls
                if calls == 1:
                    raise ReviewerInvocationError(
                        "rate limit",
                        returncode=1,
                        stdout="",
                        stderr="",
                        api_error_status=429,
                    )
                return self.canned_output

        configs = [_config("rv1")]
        adapter = FlakyAdapter(canned_output=_ship())
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(adapter),
        )
        assert result.all_succeeded
        assert adapter.parse_output_calls == 2
        assert len(adapter.invocations) == 2

    def test_auth_like_api_failure_is_not_retried(self, tmp_path: Path):
        err = ReviewerInvocationError(
            "auth failed",
            returncode=1,
            stdout="",
            stderr="",
            api_error_status=401,
        )
        configs = [_config("rv1")]
        adapter = FakeAdapter(canned_exception=err)
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(adapter),
        )
        assert result.failures[0].error is err
        assert adapter.parse_output_calls == 1
        assert len(adapter.invocations) == 1

    def test_parse_error_is_not_retried(self, tmp_path: Path):
        configs = [_config("rv1")]
        adapter = FakeAdapter(canned_exception=ReviewerOutputError("unparseable"))
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(adapter),
        )
        assert isinstance(result.failures[0].error, ReviewerOutputError)
        assert adapter.parse_output_calls == 1
        assert len(adapter.invocations) == 1


# ---------------------------------------------------------------------------
# Auth fail-fast
# ---------------------------------------------------------------------------


class TestAuthFailFast:
    def test_one_auth_failure_aborts_whole_batch(self, tmp_path: Path):
        """When one adapter's check_auth raises, NO reviewer
        subprocess runs — verified by asserting build_invocation AND
        parse_output were never called on any adapter. The dispatcher
        calls build_invocation only in phase 3, after auth passes;
        parse_output runs strictly later. Both counters at zero
        proves the pre-flight short-circuit reached neither phase."""
        configs = [_config("rv1"), _config("rv2")]
        auth_error = ReviewerInvocationError(
            "codex auth failed: Not logged in — run `codex login`",
            returncode=1,
            stdout="",
            stderr="",
        )
        adapter_with_bad_auth = FakeAdapter(canned_auth_exception=auth_error)
        adapter_with_good_auth = FakeAdapter(canned_output=_ship())
        adapters = [adapter_with_bad_auth, adapter_with_good_auth]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", "rv2", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(*adapters),
        )
        # The whole batch failed
        assert not result.all_succeeded
        assert len(result.failures) == 2
        # Both reviewers' results record the same auth error
        assert result.failures[0].error is auth_error
        assert result.failures[1].error is auth_error
        # check_auth ran on both (parallel pre-flight)
        assert adapter_with_bad_auth.check_auth_calls == 1
        assert adapter_with_good_auth.check_auth_calls == 1
        # Neither adapter's build_invocation OR parse_output fired —
        # no reviewer subprocess started. parse_output_calls is the
        # most direct assertion the dispatcher's brief calls for.
        assert adapter_with_bad_auth.invocations == []
        assert adapter_with_good_auth.invocations == []
        assert adapter_with_bad_auth.parse_output_calls == 0
        assert adapter_with_good_auth.parse_output_calls == 0

    def test_skip_auth_check_bypasses_pre_flight(self, tmp_path: Path):
        """With skip_auth_check=True, even an adapter configured to
        raise on auth check is never asked. Used by tests that don't
        want to exercise the pre-flight phase."""
        configs = [_config("rv1")]
        adapter = FakeAdapter(
            canned_auth_exception=RuntimeError("should not be called"),
            canned_output=_ship(),
        )
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            skip_auth_check=True,
            adapter_factory=_factory_returning(adapter),
        )
        assert result.all_succeeded
        assert adapter.check_auth_calls == 0
        # build_invocation DID run because the reviewer phase still
        # proceeds.
        assert len(adapter.invocations) == 1


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class _SlowAdapter(FakeAdapter):
    """FakeAdapter that emits an Invocation invoking ``sleep`` so the
    dispatcher's timeout path can be exercised against real
    run_subprocess SIGKILL handling."""

    name = "fake-slow"

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        if self.record_invocations:
            self.invocations.append((reviewer_config, worktree_path, prompt))
        import os

        return Invocation(
            argv=["sleep", "30"],
            cwd=worktree_path,
            env=dict(os.environ),
            stdin_text=None,
            timeout_seconds=None,
        )


class _SlowAdapterWithOutput(FakeAdapter):
    """Like _SlowAdapter, but the subprocess emits a known marker to
    stdout *before* it hangs — so the timeout path can be checked for
    actual partial-output preservation, not just a non-None field."""

    name = "fake-slow-output"
    STDOUT_MARKER = "partial-output-before-timeout"

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        if self.record_invocations:
            self.invocations.append((reviewer_config, worktree_path, prompt))
        import os

        return Invocation(
            argv=["sh", "-c", f"echo {self.STDOUT_MARKER}; sleep 30"],
            cwd=worktree_path,
            env=dict(os.environ),
            stdin_text=None,
            timeout_seconds=None,
        )


class TestDispatchTimeout:
    def test_timeout_records_subprocess_timeout_error(self, tmp_path: Path):
        """sleep 30 with a 0.5s dispatcher timeout — assert that the
        result records SubprocessTimeoutError and the total dispatch
        time is well under the natural runtime."""
        from syncade.process import SubprocessTimeoutError

        configs = [_config("rv1")]
        adapters = [_SlowAdapter()]
        start = time.monotonic()
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            timeout_seconds=0.5,
            adapter_factory=_factory_returning(*adapters),
        )
        elapsed = time.monotonic() - start
        # Timeout fired well under the natural 30-second runtime
        assert elapsed < 5.0, f"timeout took {elapsed}s; subprocess wasn't killed"
        assert len(result.failures) == 1
        assert isinstance(result.failures[0].error, SubprocessTimeoutError)

    def test_timeout_is_terminal_and_not_retried(self, tmp_path: Path):
        """A per-reviewer timeout is the wall-clock cap, not a transient
        adapter/API failure that gets another full subprocess window."""
        from syncade.process import SubprocessTimeoutError

        adapter = _SlowAdapter()
        start = time.monotonic()
        result = dispatch_reviewers(
            [_config("rv1")],
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            timeout_seconds=0.5,
            adapter_factory=_factory_returning(adapter),
        )
        elapsed = time.monotonic() - start

        assert len(adapter.invocations) == 1
        assert elapsed < 0.9, f"timeout was retried or delayed unexpectedly: {elapsed}s"
        assert len(result.failures) == 1
        assert isinstance(result.failures[0].error, SubprocessTimeoutError)

    def test_timeout_preserves_partial_output_in_raw_subprocess_result(self, tmp_path: Path):
        """PR-5.5: on a timeout, the ReviewerRunResult carries BOTH the
        SubprocessTimeoutError AND a synthesized raw_subprocess_result
        (sentinel returncode -1) so persistence can still write the
        .stdout / .stderr files. Before the fix, raw_subprocess_result
        was None and the partial output never reached disk."""
        from syncade.process import SubprocessResult, SubprocessTimeoutError

        configs = [_config("rv1")]
        adapters = [_SlowAdapter()]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            timeout_seconds=0.5,
            adapter_factory=_factory_returning(*adapters),
        )
        assert len(result.failures) == 1
        failure = result.failures[0]
        # error is set to the timeout exception
        assert isinstance(failure.error, SubprocessTimeoutError)
        # raw_subprocess_result is NO LONGER None — the field is populated
        assert isinstance(failure.raw_subprocess_result, SubprocessResult)
        # sentinel returncode -1 (SIGKILL'd, never exited cleanly)
        assert failure.raw_subprocess_result.returncode == -1
        # the synthesized streams match the exception's captured output
        assert failure.raw_subprocess_result.stdout == failure.error.stdout
        assert failure.raw_subprocess_result.stderr == failure.error.stderr

    def test_per_reviewer_timeout_overrides_the_global(self, monkeypatch, tmp_path: Path):
        """PR-v2-9: each reviewer's subprocess gets ITS OWN ``timeout_seconds``; a reviewer that
        leaves it unset falls back to the resolved loop/CLI global passed to dispatch. Capture the
        value handed to each reviewer's runner to prove the per-reviewer resolution end-to-end."""
        import syncade.dispatcher as dispatcher_mod
        from syncade.dispatcher import ReviewerRunResult

        captured: dict = {}

        def _capture(
            config, adapter, worktree_paths, prompt, timeout_seconds, pricing=None, max_retries=2
        ):
            captured[config.name] = timeout_seconds
            return ReviewerRunResult(
                reviewer_name=config.name,
                provider=config.provider,
                output=_ship(),
                error=None,
                duration_seconds=0.0,
            )

        monkeypatch.setattr(dispatcher_mod, "_run_single_reviewer", _capture)
        rv_custom = ReviewerConfig(
            name="rv-custom", provider="fake", model="m", timeout_seconds=600.0
        )
        rv_default = ReviewerConfig(name="rv-default", provider="fake", model="m")  # None → global
        dispatch_reviewers(
            [rv_custom, rv_default],
            worktree_paths=_worktree_paths("rv-custom", "rv-default", tmp_path=tmp_path),
            prompt="x",
            timeout_seconds=1800.0,  # the resolved loop/CLI global — the fallback
            adapter_factory=_factory_returning(FakeAdapter(), FakeAdapter()),
            skip_auth_check=True,
        )
        assert captured == {"rv-custom": 600.0, "rv-default": 1800.0}

    def test_timeout_partial_stdout_reaches_disk_via_persistence(self, tmp_path: Path):
        """The fix's payoff end-to-end: a subprocess that emits output
        before hanging has that partial stdout preserved through the
        dispatcher AND written to disk by persist_reviewer_result. The
        .error.txt still carries the timeout exception. This is the
        regression the Acme field run exposed — raw_subprocess_result
        was None, so persistence wrote empty .stdout / .stderr files."""
        from syncade.persistence import persist_reviewer_result

        configs = [_config("rv1")]
        adapters = [_SlowAdapterWithOutput()]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            timeout_seconds=0.5,
            adapter_factory=_factory_returning(*adapters),
        )
        run_result = result.failures[0]
        # The partial stdout survived the SIGKILL + dispatcher synthesis.
        assert run_result.raw_subprocess_result is not None
        assert _SlowAdapterWithOutput.STDOUT_MARKER in run_result.raw_subprocess_result.stdout

        round_dir = tmp_path / "round-0"
        round_dir.mkdir()
        persist_reviewer_result(round_dir, run_result, run_result.raw_subprocess_result)

        # .stdout on disk carries the partial content — not an empty file.
        assert _SlowAdapterWithOutput.STDOUT_MARKER in (round_dir / "rv1.stdout").read_text()
        # .stderr file exists (empty here — sh emitted nothing to stderr).
        assert (round_dir / "rv1.stderr").is_file()
        # .error.txt carries the timeout exception class.
        assert "SubprocessTimeoutError" in (round_dir / "rv1.error.txt").read_text()
