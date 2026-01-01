"""Dispatcher usage wiring (PR-v2-04): when ``pricing`` is supplied, a successful
reviewer's ``ReviewerRunResult`` carries a priced ``Usage`` extracted from its
subprocess stdout. Uses a ``FakeAdapter`` whose subprocess emits a real codex
JSONL envelope (via ``/bin/sh -c printf``) so the extraction path is exercised
end-to-end, not mocked."""

from __future__ import annotations

from pathlib import Path

from syncade.adapters.base import Invocation, ReviewerInvocationError
from syncade.adapters.fake_reviewer_synth import FakeAdapter
from syncade.dispatcher import dispatch_reviewers
from syncade.pricing_config import PricingConfig
from syncade.worktree_env import worktree_scoped_env
from tests.dispatcher._helpers import _config, _factory_returning, _ship, _worktree_paths

_CODEX_JSONL = (
    '{"type":"turn.started"}\n'
    '{"type":"turn.completed","usage":{"input_tokens":2000,"cached_input_tokens":0,'
    '"output_tokens":600,"reasoning_output_tokens":0}}'
)


class _CodexStdoutAdapter(FakeAdapter):
    """FakeAdapter whose subprocess actually emits a codex JSONL envelope, so the
    dispatcher's usage extraction has a real payload to parse."""

    def build_invocation(self, reviewer_config, worktree_path, prompt) -> Invocation:
        return Invocation(
            argv=["/bin/sh", "-c", 'printf "%s" "$1"', "sh", _CODEX_JSONL],
            cwd=worktree_path,
            env=worktree_scoped_env(worktree_path),
            stdin_text=None,
            timeout_seconds=None,
        )


class _FlakyCodexStdoutAdapter(_CodexStdoutAdapter):
    def __init__(self, *, fail_times: int, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._fail_times = fail_times

    def parse_output(self, result):  # type: ignore[override]
        with self._lock:
            self.parse_output_calls += 1
            call = self.parse_output_calls
        if call <= self._fail_times:
            raise ReviewerInvocationError(
                "rate limited",
                returncode=1,
                stdout=result.stdout,
                stderr="429 too many requests",
                api_error_status=429,
            )
        return self.canned_output


def test_dispatch_populates_priced_usage_from_codex_stdout(tmp_path: Path):
    config = _config("cdx", provider="openai", model="gpt-5.5")
    result = dispatch_reviewers(
        [config],
        worktree_paths=_worktree_paths("cdx", tmp_path=tmp_path),
        prompt="x",
        adapter_factory=_factory_returning(_CodexStdoutAdapter(canned_output=_ship())),
        pricing=PricingConfig(),
    )
    r = result.results[0]
    assert r.output is not None  # reviewer succeeded
    assert r.usage is not None
    assert r.usage.model == "gpt-5.5"  # stamped from config (codex event has no model)
    assert r.usage.input_tokens == 2000 and r.usage.output_tokens == 600
    assert r.usage.cost_source == "estimated" and r.usage.cost_usd and r.usage.cost_usd > 0


def test_dispatch_without_pricing_leaves_usage_none(tmp_path: Path):
    # Backward compat: callers that don't pass pricing get usage=None, no crash.
    config = _config("cdx", provider="openai", model="gpt-5.5")
    result = dispatch_reviewers(
        [config],
        worktree_paths=_worktree_paths("cdx", tmp_path=tmp_path),
        prompt="x",
        adapter_factory=_factory_returning(_CodexStdoutAdapter(canned_output=_ship())),
    )
    assert result.results[0].output is not None
    assert result.results[0].usage is None


def test_dispatch_captures_usage_even_when_parse_fails(tmp_path: Path):
    # Finding #5: a subprocess that emitted valid usage but whose output failed to
    # parse must still record that usage — the call cost real tokens.
    from syncade.findings import ReviewerOutputError

    config = _config("cdx", provider="openai", model="gpt-5.5")
    fake = _CodexStdoutAdapter(canned_exception=ReviewerOutputError("unparseable"))
    result = dispatch_reviewers(
        [config],
        worktree_paths=_worktree_paths("cdx", tmp_path=tmp_path),
        prompt="x",
        adapter_factory=_factory_returning(fake),
        pricing=PricingConfig(),
    )
    r = result.results[0]
    assert r.output is None and r.error is not None  # the reviewer failed to parse
    assert r.usage is not None and r.usage.total_tokens > 0  # yet its usage is captured


def test_dispatch_accumulates_usage_from_transient_retry_attempts(tmp_path: Path):
    config = _config("cdx", provider="openai", model="gpt-5.5")
    fake = _FlakyCodexStdoutAdapter(fail_times=1, canned_output=_ship())
    result = dispatch_reviewers(
        [config],
        worktree_paths=_worktree_paths("cdx", tmp_path=tmp_path),
        prompt="x",
        adapter_factory=_factory_returning(fake),
        pricing=PricingConfig(),
    )
    r = result.results[0]
    assert r.output is not None
    assert r.retries == 1
    assert r.usage is not None
    assert r.usage.input_tokens == 4000
    assert r.usage.output_tokens == 1200
    assert r.usage.total_tokens == 5200
    assert r.usage.cost_source == "estimated"
    assert r.usage.cost_usd and r.usage.cost_usd > 0
