"""Tests for :mod:`syncade.dispatcher` — binary-not-found, unknown
provider, parallelism, DispatchResult properties, missing worktree
path, and the None-input contract.

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
)
from syncade.adapters.fake import FakeAdapter
from syncade.config import ReviewerConfig
from syncade.dispatcher import (
    DispatchResult,
    ReviewerRunResult,
    dispatch_reviewers,
)
from tests.dispatcher._helpers import (
    _config,
    _factory_returning,
    _ship,
    _worktree_paths,
)

# ---------------------------------------------------------------------------
# Binary not found — the contrast case: no partial output to preserve
# ---------------------------------------------------------------------------


class _MissingBinaryAdapter(FakeAdapter):
    """FakeAdapter whose Invocation points at a binary that isn't on
    PATH, so run_subprocess raises SubprocessNotFoundError before the
    process ever starts."""

    name = "fake-missing-binary"

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
            argv=["syncade-no-such-binary-7f3a9c"],
            cwd=worktree_path,
            env=dict(os.environ),
            stdin_text=None,
            timeout_seconds=None,
        )


class TestBinaryNotFound:
    def test_binary_not_found_leaves_raw_subprocess_result_none(self, tmp_path: Path):
        """SubprocessNotFoundError carries no partial output — the
        process never started — so raw_subprocess_result stays None.
        This is the deliberate contrast with the timeout path, where the
        dispatcher synthesizes a SubprocessResult from the exception's
        captured streams (see TestDispatchTimeout)."""
        from syncade.process import SubprocessNotFoundError

        configs = [_config("rv1")]
        adapters = [_MissingBinaryAdapter()]
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(*adapters),
        )
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert isinstance(failure.error, SubprocessNotFoundError)
        # No subprocess ran -> nothing to preserve -> None (NOT a
        # synthesized SubprocessResult like the timeout path produces).
        assert failure.raw_subprocess_result is None


# ---------------------------------------------------------------------------
# Unknown provider — whole batch fails immediately
# ---------------------------------------------------------------------------


class TestUnknownProvider:
    def test_unknown_provider_fails_whole_batch_before_dispatch(self, tmp_path: Path):
        """Default factory (the production registry) doesn't know
        'not-a-real-provider'; the dispatch must fail with that
        registry error for every reviewer in the batch."""
        configs = [
            _config("rv1", provider="anthropic"),
            _config("rv2", provider="not-a-real-provider"),
        ]
        # Use the real registry (default adapter_factory). The
        # anthropic config would resolve cleanly, but the second
        # config's bad provider aborts the whole batch.
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", "rv2", tmp_path=tmp_path),
            prompt="x",
        )
        assert not result.all_succeeded
        assert len(result.failures) == 2
        # The error is the registry's ValueError
        for failure in result.failures:
            assert isinstance(failure.error, ValueError)
            assert "not-a-real-provider" in str(failure.error)


# ---------------------------------------------------------------------------
# Parallelism
# ---------------------------------------------------------------------------


class _SleepyParseAdapter(FakeAdapter):
    """FakeAdapter whose parse_output sleeps for a configurable
    interval. Used to prove the dispatcher actually parallelizes —
    two of these running concurrently should finish in roughly the
    per-adapter sleep time, not 2× that."""

    name = "fake-sleepy"

    def __init__(self, sleep_seconds: float, **kwargs):
        super().__init__(**kwargs)
        self.sleep_seconds = sleep_seconds

    def parse_output(self, result):  # type: ignore[override]
        time.sleep(self.sleep_seconds)
        return super().parse_output(result)


class TestParallelism:
    def test_two_reviewers_run_in_parallel_not_serial(self, tmp_path: Path):
        """Two adapters each take 1s in parse_output. With true
        parallelism the dispatch completes in ~1s; serial execution
        would take ~2s. Generous tolerance (< 1.8s) to avoid CI
        flakiness."""
        configs = [_config("rv1"), _config("rv2")]
        adapters = [
            _SleepyParseAdapter(sleep_seconds=1.0, canned_output=_ship()),
            _SleepyParseAdapter(sleep_seconds=1.0, canned_output=_ship()),
        ]
        start = time.monotonic()
        result = dispatch_reviewers(
            configs,
            worktree_paths=_worktree_paths("rv1", "rv2", tmp_path=tmp_path),
            prompt="x",
            skip_auth_check=True,  # keep this test focused
            adapter_factory=_factory_returning(*adapters),
        )
        elapsed = time.monotonic() - start
        assert result.all_succeeded
        # Two 1-second sleeps in parallel: should be well under 2s
        assert elapsed < 1.8, (
            f"dispatch took {elapsed:.2f}s; expected parallel ~1s, serial would be ~2s"
        )
        # And total_duration_seconds on the result aligns
        assert result.total_duration_seconds < 1.8


# ---------------------------------------------------------------------------
# DispatchResult properties
# ---------------------------------------------------------------------------


class TestDispatchResultProperties:
    def test_all_succeeded_false_when_any_fail(self):
        results = [
            ReviewerRunResult(
                reviewer_name="a",
                provider="p",
                output=_ship(),
                error=None,
                duration_seconds=0.1,
            ),
            ReviewerRunResult(
                reviewer_name="b",
                provider="p",
                output=None,
                error=RuntimeError("x"),
                duration_seconds=0.1,
            ),
        ]
        agg = DispatchResult(results=results, total_duration_seconds=0.2)
        assert not agg.all_succeeded
        assert len(agg.successes) == 1
        assert len(agg.failures) == 1

    def test_all_succeeded_false_for_empty_results(self):
        """No reviewers means no successes — all_succeeded is False
        regardless. Defensive: avoids ``all(<empty>)`` returning True."""
        agg = DispatchResult(results=[], total_duration_seconds=0.0)
        assert not agg.all_succeeded
        assert agg.successes == []
        assert agg.failures == []

    def test_partitioning_preserves_input_order(self):
        """The successes / failures lists preserve the input order
        within each partition."""
        results = [
            ReviewerRunResult(
                reviewer_name="a",
                provider="p",
                output=_ship(),
                error=None,
                duration_seconds=0.1,
            ),
            ReviewerRunResult(
                reviewer_name="b",
                provider="p",
                output=None,
                error=RuntimeError("x"),
                duration_seconds=0.1,
            ),
            ReviewerRunResult(
                reviewer_name="c",
                provider="p",
                output=_ship(),
                error=None,
                duration_seconds=0.1,
            ),
            ReviewerRunResult(
                reviewer_name="d",
                provider="p",
                output=None,
                error=RuntimeError("y"),
                duration_seconds=0.1,
            ),
        ]
        agg = DispatchResult(results=results, total_duration_seconds=0.4)
        assert [r.reviewer_name for r in agg.successes] == ["a", "c"]
        assert [r.reviewer_name for r in agg.failures] == ["b", "d"]


# ---------------------------------------------------------------------------
# Missing worktree path
# ---------------------------------------------------------------------------


class TestMissingWorktreePath:
    def test_missing_worktree_for_reviewer_fails_only_that_reviewer(self, tmp_path: Path):
        """A reviewer whose name isn't in worktree_paths fails
        individually (clear ValueError) while siblings run normally."""
        configs = [_config("rv1"), _config("rv2")]
        adapters = [FakeAdapter(canned_output=_ship()) for _ in configs]
        # rv1's worktree exists in the map; rv2's is omitted entirely.
        rv1_worktree = tmp_path / "rv1"
        rv1_worktree.mkdir()
        result = dispatch_reviewers(
            configs,
            worktree_paths={"rv1": rv1_worktree},
            prompt="x",
            adapter_factory=_factory_returning(*adapters),
        )
        assert len(result.results) == 2
        # rv1 succeeded (its worktree was provided)
        rv1 = next(r for r in result.results if r.reviewer_name == "rv1")
        assert rv1.output is not None
        # rv2 failed with the worktree-missing ValueError — that one
        # reviewer's failure does NOT cascade to rv1
        rv2 = next(r for r in result.results if r.reviewer_name == "rv2")
        assert rv2.error is not None
        assert isinstance(rv2.error, ValueError)
        assert "worktree_paths" in str(rv2.error)
        assert "rv2" in str(rv2.error)


class TestMissingPromptKey:
    def test_missing_prompt_key_raises_before_adapter_lookup(self, tmp_path: Path):
        configs = [_config("rv1"), _config("rv2")]

        def fail_if_called(provider: str):
            raise AssertionError(f"adapter lookup should not run for {provider}")

        import pytest

        with pytest.raises(KeyError) as exc_info:
            dispatch_reviewers(
                configs,
                worktree_paths=_worktree_paths("rv1", "rv2", tmp_path=tmp_path),
                prompt={"rv1": "prompt"},
                adapter_factory=fail_if_called,
            )

        msg = str(exc_info.value)
        assert "prompt" in msg
        assert "rv2" in msg
        assert "rv1" not in msg


# ---------------------------------------------------------------------------
# None-input contract — dispatcher's documented Raises section
# ---------------------------------------------------------------------------


class TestNoneInputs:
    """The dispatcher's contract is "captures every runtime failure in
    DispatchResult.failures, EXCEPT for caller bugs that aren't
    runtime conditions." Passing None instead of a list/dict is a
    type-level mistake, not something the orchestrator could
    sensibly recover from per-reviewer, so it raises TypeError
    rather than returning a DispatchResult full of None-related
    errors. These tests pin that contract."""

    def test_none_reviewer_configs_raises_type_error(self):
        import pytest

        with pytest.raises(TypeError) as exc_info:
            dispatch_reviewers(
                None,  # type: ignore[arg-type]
                worktree_paths={},
                prompt="x",
            )
        msg = str(exc_info.value)
        assert "reviewer_configs" in msg
        # Message points at the fix (use [] if no reviewers)
        assert "[]" in msg

    def test_none_worktree_paths_raises_type_error(self):
        import pytest

        with pytest.raises(TypeError) as exc_info:
            dispatch_reviewers(
                [],
                worktree_paths=None,  # type: ignore[arg-type]
                prompt="x",
            )
        msg = str(exc_info.value)
        assert "worktree_paths" in msg
        assert "{}" in msg

    def test_empty_lists_return_empty_dispatch_result(self):
        """The orchestrator might legitimately pass an empty configs
        list after future filtering or config resolution. That should
        not raise — it returns an empty DispatchResult."""
        result = dispatch_reviewers(
            [],
            worktree_paths={},
            prompt="x",
        )
        assert isinstance(result, DispatchResult)
        assert result.results == []
        assert not result.all_succeeded  # vacuously: no successes
