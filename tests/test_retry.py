"""Regression tests for H5 — bounded retry on transient reviewer / synth
API errors.

Covers the four surfaces the fix touches:

* :mod:`syncade.retry` — the shared transient/permanent classifier and the
  jittered backoff seam.
* :mod:`syncade.dispatcher` — per-reviewer dispatch retries a transient
  ``ReviewerInvocationError`` (up to ``retry.MAX_RETRIES`` extra attempts)
  and surfaces the count on :class:`ReviewerRunResult.retries`.
* :mod:`syncade.synthesizer.driver` — the cold-synth call retries the same
  transient class by re-running the subprocess.
* :mod:`syncade.persistence.round_manifest` — the round manifest records a
  ``"retried"`` annotation.

A parse/contract failure (``ReviewerOutputError`` / ``SynthesizerOutputError``)
or a wall-clock timeout is NEVER retried.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade import retry
from syncade.adapters.base import Invocation, ReviewerInvocationError
from syncade.adapters.fake import FakeAdapter
from syncade.dispatcher import DispatchResult, ReviewerRunResult, dispatch_reviewers
from syncade.findings import ReviewerOutputError
from syncade.persistence import persist_round_manifest
from syncade.pricing_config import PricingConfig
from syncade.process import (
    SubprocessNotFoundError,
    SubprocessResult,
    SubprocessTimeoutError,
)
from syncade.synthesis import SynthesizerOutputError
from syncade.synthesizer.result import SynthesizerResult
from tests.dispatcher._helpers import (
    _config,
    _factory_returning,
    _ship,
    _worktree_paths,
)
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _snapshot,
    _subprocess_result,
)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retries instant and deterministic — assert behavior, not timing.

    Both call sites resolve ``retry.backoff_sleep`` on the module object at
    call time, so patching the attribute here neutralizes the real sleep.
    """
    monkeypatch.setattr(retry, "backoff_sleep", lambda *_a, **_k: None)


def _invocation_error(
    message: str = "boom", *, status: int | None = None, stderr: str = ""
) -> ReviewerInvocationError:
    return ReviewerInvocationError(
        message, returncode=1, stdout="", stderr=stderr, api_error_status=status
    )


_CODEX_USAGE_JSONL = (
    '{"type":"turn.started"}\n'
    '{"type":"turn.completed","usage":{"input_tokens":2000,"cached_input_tokens":0,'
    '"output_tokens":600,"reasoning_output_tokens":0}}'
)


# ---------------------------------------------------------------------------
# Shared classifier — the transient whitelist (syncade.retry)
# ---------------------------------------------------------------------------


class TestTransientClassification:
    def test_429_and_5xx_are_transient(self) -> None:
        assert retry.is_transient_api_error(_invocation_error(status=429))
        assert retry.is_transient_api_error(_invocation_error(status=500))
        assert retry.is_transient_api_error(_invocation_error(status=503))

    def test_other_4xx_statuses_are_terminal(self) -> None:
        for status in (400, 401, 403, 404):
            assert not retry.is_transient_api_error(_invocation_error(status=status))

    def test_marker_substring_transient_only_without_status(self) -> None:
        assert retry.is_transient_api_error(_invocation_error(stderr="Connection reset by peer"))
        assert retry.is_transient_api_error(_invocation_error("upstream timed out"))

    def test_unknown_message_without_status_is_terminal(self) -> None:
        assert not retry.is_transient_api_error(_invocation_error("the model declined"))

    def test_parse_and_contract_errors_never_transient(self) -> None:
        # Deterministic failures: retrying cannot change the outcome.
        assert not retry.is_transient_api_error(ReviewerOutputError("unparseable"))
        assert not retry.is_transient_api_error(SynthesizerOutputError("invented a finding"))

    def test_timeout_and_missing_binary_never_transient(self) -> None:
        # SubprocessTimeoutError's message literally contains "timed out" — a
        # transient marker — so this pins that the type gate fires BEFORE the
        # substring scan. A timeout is the budget, not a blip.
        assert not retry.is_transient_api_error(
            SubprocessTimeoutError("timed out", stdout="", stderr="", timeout=1.0)
        )
        assert not retry.is_transient_api_error(SubprocessNotFoundError("claude"))


# ---------------------------------------------------------------------------
# Per-reviewer dispatch retry (syncade.dispatcher)
# ---------------------------------------------------------------------------


class _FlakyReviewerAdapter(FakeAdapter):
    """Raises a transient API error on the first ``fail_times`` parse calls,
    then returns the canned output."""

    def __init__(self, *, fail_times: int, exc: Exception, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._fail_times = fail_times
        self._exc = exc

    def parse_output(self, result: SubprocessResult):  # type: ignore[override]
        del result
        with self._lock:
            self.parse_output_calls += 1
            call = self.parse_output_calls
        if call <= self._fail_times:
            raise self._exc
        return self.canned_output


class TestReviewerDispatchRetry:
    def test_two_transient_failures_then_success(self, tmp_path: Path) -> None:
        adapter = _FlakyReviewerAdapter(
            fail_times=2, exc=_invocation_error("rate limit", status=429), canned_output=_ship()
        )
        result = dispatch_reviewers(
            [_config("rv1")],
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(adapter),
        )
        assert result.all_succeeded
        # 1 initial + 2 retries = 3 fresh subprocess attempts.
        assert adapter.parse_output_calls == 3
        assert len(adapter.invocations) == 3
        assert result.results[0].retries == 2

    def test_retries_are_bounded_then_failure(self, tmp_path: Path) -> None:
        adapter = _FlakyReviewerAdapter(
            fail_times=99,
            exc=_invocation_error("503 unavailable", status=503),
            canned_output=_ship(),
        )
        result = dispatch_reviewers(
            [_config("rv1")],
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(adapter),
        )
        assert not result.all_succeeded
        assert adapter.parse_output_calls == retry.MAX_RETRIES + 1
        assert result.results[0].retries == retry.MAX_RETRIES
        assert isinstance(result.failures[0].error, ReviewerInvocationError)

    def test_parse_contract_error_is_not_retried(self, tmp_path: Path) -> None:
        adapter = FakeAdapter(canned_exception=ReviewerOutputError("unparseable"))
        result = dispatch_reviewers(
            [_config("rv1")],
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(adapter),
        )
        assert isinstance(result.failures[0].error, ReviewerOutputError)
        assert adapter.parse_output_calls == 1
        assert len(adapter.invocations) == 1
        assert result.results[0].retries == 0

    def test_terminal_status_error_is_not_retried(self, tmp_path: Path) -> None:
        adapter = FakeAdapter(canned_exception=_invocation_error("auth failed", status=401))
        result = dispatch_reviewers(
            [_config("rv1")],
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(adapter),
        )
        assert isinstance(result.failures[0].error, ReviewerInvocationError)
        assert adapter.parse_output_calls == 1
        assert result.results[0].retries == 0

    @pytest.mark.parametrize("bound,attempts", [(0, 1), (1, 2), (5, 6)])
    def test_config_max_retries_threads_to_the_bound(
        self, tmp_path: Path, bound: int, attempts: int
    ) -> None:
        """PR-v2-9: ``config.retry.max_retries`` reaches the dispatch loop. A never-recovering
        transient 503 attempts exactly ``bound + 1`` times — 0 disables retries entirely (1
        attempt), and the surfaced count equals the configured bound, so the value threads
        through rather than being pinned to ``retry.MAX_RETRIES``."""
        adapter = _FlakyReviewerAdapter(
            fail_times=99, exc=_invocation_error("503", status=503), canned_output=_ship()
        )
        result = dispatch_reviewers(
            [_config("rv1")],
            worktree_paths=_worktree_paths("rv1", tmp_path=tmp_path),
            prompt="x",
            adapter_factory=_factory_returning(adapter),
            max_retries=bound,
        )
        assert not result.all_succeeded
        assert adapter.parse_output_calls == attempts
        assert result.results[0].retries == bound


# ---------------------------------------------------------------------------
# Cold-synth retry (syncade.synthesizer.driver)
# ---------------------------------------------------------------------------


class _FlakySynthAdapter:
    """Duck-typed synth adapter: raises ``exc`` on the first ``fail_times``
    extract calls, then returns a valid empty-consolidation JSON blob."""

    name = "openai"

    def __init__(self, *, fail_times: int, exc: Exception) -> None:
        self._fail_times = fail_times
        self._exc = exc
        self.extract_calls = 0

    def build_invocation(self, reviewer_config, worktree_path: Path, prompt: str) -> Invocation:
        del reviewer_config, prompt
        return Invocation(argv=["echo", "noop"], cwd=worktree_path, env={}, stdin_text=None)

    def extract_final_text(
        self, result: SubprocessResult, *, empty_output_exception_class: type[Exception]
    ) -> str:
        del result, empty_output_exception_class
        self.extract_calls += 1
        if self.extract_calls <= self._fail_times:
            raise self._exc
        return '{"consolidated_findings": [], "synthesis_summary": "ok"}'


def _synth_reviewer_result(name: str = "rv1") -> ReviewerRunResult:
    return ReviewerRunResult(
        reviewer_name=name, provider="anthropic", output=_ship(), error=None, duration_seconds=1.0
    )


@pytest.fixture
def repo_with_pr_doc(tmp_path: Path) -> tuple[Path, Path]:
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\nDoes the thing.\n")
    return tmp_path, pr_doc


def _run_synth(
    repo_with_pr_doc,
    monkeypatch,
    adapter,
    *,
    stdout: str = "",
    pricing: PricingConfig | None = None,
    max_retries: int | None = None,
) -> tuple[object, list[int]]:
    import syncade.synthesizer as synth
    import syncade.synthesizer.driver as synth_driver

    sub_calls: list[int] = []

    def fake_run_subprocess(argv, *, cwd, env, timeout, input_text):
        del argv, cwd, env, timeout, input_text
        sub_calls.append(1)
        return SubprocessResult(returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)

    monkeypatch.setattr(synth_driver, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(synth_driver, "_init_workspace_git", lambda workspace: None)

    repo, pr_doc = repo_with_pr_doc
    extra = {} if max_retries is None else {"max_retries": max_retries}
    result = synth.run_synthesizer(
        reviewer_results=[_synth_reviewer_result()],
        repo_root=repo,
        pr_doc_path=pr_doc,
        timeout_seconds=10.0,
        adapter=adapter,
        pricing=pricing,
        **extra,
    )
    return result, sub_calls


class TestSynthesizerRetry:
    def test_two_transient_failures_then_success(self, repo_with_pr_doc, monkeypatch) -> None:
        adapter = _FlakySynthAdapter(fail_times=2, exc=_invocation_error("rate limit", status=429))
        result, sub_calls = _run_synth(repo_with_pr_doc, monkeypatch, adapter)
        assert result.output is not None
        assert result.error is None
        assert adapter.extract_calls == 3
        # A fresh subprocess is launched on every attempt — re-parsing the same
        # stdout would just reproduce the transient failure.
        assert len(sub_calls) == 3
        # The synth retry count is surfaced on the result (H5), like a reviewer.
        assert result.retries == 2

    def test_transient_retry_accumulates_usage(self, repo_with_pr_doc, monkeypatch) -> None:
        adapter = _FlakySynthAdapter(fail_times=1, exc=_invocation_error("rate limit", status=429))
        result, sub_calls = _run_synth(
            repo_with_pr_doc,
            monkeypatch,
            adapter,
            stdout=_CODEX_USAGE_JSONL,
            pricing=PricingConfig(),
        )
        assert result.output is not None
        assert result.error is None
        assert len(sub_calls) == 2
        assert result.retries == 1
        assert result.usage is not None
        assert result.usage.input_tokens == 4000
        assert result.usage.output_tokens == 1200
        assert result.usage.total_tokens == 5200
        assert result.usage.cost_source == "estimated"
        assert result.usage.cost_usd and result.usage.cost_usd > 0

    def test_parse_contract_error_is_not_retried(self, repo_with_pr_doc, monkeypatch) -> None:
        adapter = _FlakySynthAdapter(fail_times=99, exc=SynthesizerOutputError("invented finding"))
        result, sub_calls = _run_synth(repo_with_pr_doc, monkeypatch, adapter)
        assert result.output is None
        assert isinstance(result.error, SynthesizerOutputError)
        assert adapter.extract_calls == 1
        assert len(sub_calls) == 1
        assert result.retries == 0  # contract failures are never retried

    def test_terminal_status_error_is_not_retried(self, repo_with_pr_doc, monkeypatch) -> None:
        adapter = _FlakySynthAdapter(fail_times=99, exc=_invocation_error("auth", status=401))
        result, sub_calls = _run_synth(repo_with_pr_doc, monkeypatch, adapter)
        assert result.output is None
        assert isinstance(result.error, ReviewerInvocationError)
        assert adapter.extract_calls == 1
        assert len(sub_calls) == 1
        assert result.retries == 0  # terminal status (401) is never retried

    @pytest.mark.parametrize("bound,attempts", [(0, 1), (1, 2), (5, 6)])
    def test_config_max_retries_threads_to_the_bound(
        self, repo_with_pr_doc, monkeypatch, bound: int, attempts: int
    ) -> None:
        """PR-v2-9: ``config.retry.max_retries`` reaches the cold-synth loop — a
        never-recovering transient 503 launches exactly ``bound + 1`` subprocesses."""
        adapter = _FlakySynthAdapter(fail_times=99, exc=_invocation_error("503", status=503))
        result, sub_calls = _run_synth(repo_with_pr_doc, monkeypatch, adapter, max_retries=bound)
        assert result.output is None
        assert len(sub_calls) == attempts
        assert result.retries == bound


# ---------------------------------------------------------------------------
# Round-manifest "retried" annotation (syncade.persistence.round_manifest)
# ---------------------------------------------------------------------------


def _dispatch_with_retries(*retries: int) -> DispatchResult:
    results = [
        ReviewerRunResult(
            reviewer_name=f"rv{i}",
            provider="anthropic",
            output=_ship(),
            error=None,
            duration_seconds=1.0,
            raw_subprocess_result=_subprocess_result(),
            retries=n,
        )
        for i, n in enumerate(retries)
    ]
    return DispatchResult(results=results, total_duration_seconds=1.0)


class TestManifestRetriedAnnotation:
    def test_retried_sums_reviewer_retries(self, tmp_path: Path) -> None:
        round_dir = _make_round_dir(tmp_path)
        dispatch = _dispatch_with_retries(2, 0, 1)
        path = persist_round_manifest(
            round_dir, _snapshot(), dispatch, exit_code=0, started_at=_FIXED_STARTED_AT
        )
        manifest = json.loads(path.read_text())
        assert manifest["retried"] == 3

    def test_retried_zero_when_no_retries(self, tmp_path: Path) -> None:
        round_dir = _make_round_dir(tmp_path)
        dispatch = _dispatch_with_retries(0, 0)
        path = persist_round_manifest(
            round_dir, _snapshot(), dispatch, exit_code=0, started_at=_FIXED_STARTED_AT
        )
        manifest = json.loads(path.read_text())
        assert manifest["retried"] == 0

    def test_retried_includes_synth_retries(self, tmp_path: Path) -> None:
        """H5: the round-level ``retried`` count sums reviewer-dispatch retries
        AND the synthesizer's own retries (SynthesizerResult.retries)."""
        round_dir = _make_round_dir(tmp_path)
        dispatch = _dispatch_with_retries(2, 0, 1)  # 3 reviewer retries
        synth = SynthesizerResult(
            output=None,
            error=SynthesizerOutputError("contract"),
            duration_seconds=1.0,
            retries=2,
        )
        path = persist_round_manifest(
            round_dir,
            _snapshot(),
            dispatch,
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
        )
        manifest = json.loads(path.read_text())
        assert manifest["retried"] == 5  # 3 reviewer + 2 synth


class TestStreamDisconnectIsTransient:
    """The most common real transient we hit, and it matched no marker.

    codex reports a dropped response stream as `stream disconnected before
    completion: error sending request for url (...)` with NO HTTP status, so
    neither the status gate nor any existing substring caught it. Two dogfood
    runs died at exit 40 — losing every remaining round — because the bounded
    retry that exists for precisely this case never fired. Verbatim message
    from `.syncade/runs/2026-07-30T08-16-56/round-1/codex-reviewer.error.txt`.
    """

    _REAL = (
        "codex failed (rc=1): stream disconnected before completion: "
        "error sending request for url (https://chatgpt.com/backend-api/codex/responses)"
    )

    def test_recorded_stream_disconnect_is_retried(self) -> None:
        exc = ReviewerInvocationError(self._REAL, returncode=1, stdout="", stderr="")
        assert retry.is_transient_api_error(exc) is True

    def test_matches_on_stderr_too(self) -> None:
        exc = ReviewerInvocationError(
            "codex failed (rc=1)", returncode=1, stdout="", stderr=self._REAL
        )
        assert retry.is_transient_api_error(exc) is True

    def test_a_concrete_4xx_status_still_wins(self) -> None:
        """A status-bearing error is a terminal provider verdict even if the
        text happens to contain a transient-looking phrase."""
        exc = ReviewerInvocationError(
            self._REAL, returncode=1, stdout="", stderr="", api_error_status=401
        )
        assert retry.is_transient_api_error(exc) is False

    def test_timeout_type_gate_still_wins(self) -> None:
        """A wall-clock timeout is the budget, not a blip — the type gate must
        keep firing before the substring scan."""
        from syncade.process import SubprocessTimeoutError

        exc = SubprocessTimeoutError("codex timed out", stdout="", stderr="", timeout=1800.0)
        assert retry.is_transient_api_error(exc) is False
