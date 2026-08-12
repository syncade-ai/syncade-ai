"""Tests for :class:`syncade.adapters.openai.CodexAdapter`.

``build_invocation`` and ``parse_output`` are pure-data tests with
canned :class:`SubprocessResult` objects. ``check_auth`` requires real
subprocess calls (we test against the actual ``codex login status``
command) and skips if ``codex`` is not on PATH — same discipline as
``tests/test_process.py``.

End-to-end smoke tests live in ``tests/smoke/test_codex_smoke.py`` and
run only via ``pytest -m smoke``.
"""

from __future__ import annotations

import shutil

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.openai import CodexAdapter
from syncade.findings import ReviewerOutputError
from syncade.process import SubprocessResult
from tests.adapters._openai_helpers import _jsonl_envelope

# ---------------------------------------------------------------------------
# check_auth — real subprocess calls, skipped if codex isn't on PATH
# ---------------------------------------------------------------------------


class TestCheckAuth:
    def test_check_auth_succeeds_when_codex_logged_in(self):
        """Live test against the user's actual codex auth state.
        Skips if codex is not on PATH (test environment without the
        CLI installed)."""
        if shutil.which("codex") is None:
            pytest.skip("codex CLI not on PATH")
        adapter = CodexAdapter()
        # This test passes if the developer running it is logged in;
        # if they aren't, the test failure carries the actual
        # "codex auth check failed" message which is exactly the
        # right signal.
        adapter.check_auth()  # must not raise

    def test_check_auth_raises_when_codex_not_on_path(self, monkeypatch):
        """Force the binary-not-found path by stubbing PATH to a dir
        that doesn't contain codex. Verifies the error message
        mentions installation."""
        monkeypatch.setenv("PATH", "/nonexistent-syncade-test-path")
        adapter = CodexAdapter()
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.check_auth()
        msg = str(exc_info.value)
        assert "codex" in msg.lower()
        assert "not found" in msg.lower() or "install" in msg.lower()

    def test_check_auth_raises_when_timeout_fires(self, monkeypatch):
        """The timeout branch of check_auth: when ``codex login status``
        hangs longer than the adapter's hardcoded timeout, the
        adapter raises ReviewerInvocationError with a message that
        names the timeout and points at ``codex login`` as the
        remediation step.

        We exercise the real ``run_subprocess`` timeout path by
        substituting ``sleep`` for the auth-check argv (no mocking
        of subprocess.run itself — that's per the PR-4 brief's
        check_auth testing discipline). ``sleep 30`` with a 0.3s
        timeout cleanly triggers SIGKILL via the
        :class:`SubprocessTimeoutError` branch.
        """
        # Swap _CODEX_AUTH_CHECK_ARGV and _CODEX_AUTH_CHECK_TIMEOUT_SECONDS
        # via monkeypatch so the adapter actually exercises the
        # timeout branch against a real sleep subprocess.
        monkeypatch.setattr(
            "syncade.adapters.openai._CODEX_AUTH_CHECK_ARGV",
            ["sleep", "30"],
        )
        monkeypatch.setattr(
            "syncade.adapters.openai._CODEX_AUTH_CHECK_TIMEOUT_SECONDS",
            0.3,
        )
        adapter = CodexAdapter()
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.check_auth()
        msg = str(exc_info.value)
        # Message names the timeout duration and points at the fix
        assert "timed out" in msg.lower()
        assert "0.3" in msg
        assert "codex login" in msg


# ---------------------------------------------------------------------------
# Documentation surface
# ---------------------------------------------------------------------------


def test_public_surface_has_docstrings():
    import inspect

    assert inspect.getdoc(CodexAdapter)
    assert inspect.getdoc(CodexAdapter.build_invocation)
    assert inspect.getdoc(CodexAdapter.parse_output)
    assert inspect.getdoc(CodexAdapter.check_auth)
    assert inspect.getdoc(CodexAdapter.extract_final_text)
    # PR-14: extract_response_text is the symmetric per-adapter
    # interface paired with AnthropicAdapter.extract_response_text.
    assert inspect.getdoc(CodexAdapter.extract_response_text)


# ---------------------------------------------------------------------------
# PR-14: extract_response_text — symmetric per-adapter helper for the
# orchestrator's prior-round-context plumbing. Pairs with
# AnthropicAdapter.extract_response_text. Round-trip pin: feed a known
# JSONL envelope, assert the extracted text equals the final
# agent_message's text.
# ---------------------------------------------------------------------------


class TestExtractResponseText:
    """PR-14 Task 4: :meth:`CodexAdapter.extract_response_text` is a
    thin str-in / str-out wrapper around
    :meth:`extract_final_text` that synthesizes a
    :class:`SubprocessResult` internally and defaults the
    ``empty_output_exception_class`` to
    :class:`ReviewerOutputError`. Symmetric counterpart to
    :meth:`syncade.adapters.anthropic.AnthropicAdapter.extract_response_text`."""

    def test_round_trip_final_agent_message_extracted_verbatim(self):
        """Feed a known JSONL envelope with one ``agent_message``;
        assert the extracted text equals that message's ``item.text``."""
        adapter = CodexAdapter()
        original_text = (
            "Here's my review:\n\nFinding 1 — null check missing at "
            'src/foo.py:42.\n\n```json\n{"verdict": "NO-SHIP"}\n```'
        )
        stdout = _jsonl_envelope(agent_messages=[original_text])
        extracted = adapter.extract_response_text(stdout)
        assert extracted == original_text, (
            "extract_response_text must return the final agent_message's "
            "item.text verbatim — no markdown unwrapping, no JSON "
            "parsing, no whitespace stripping"
        )

    def test_extract_returns_str_not_subprocessresult(self):
        """Symmetric return type with AnthropicAdapter: ``str`` is the
        shape the orchestrator's prior_round.py wiring expects to
        splice into ``{prior_round_output}``."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=["x"])
        assert isinstance(adapter.extract_response_text(stdout), str)

    def test_extract_returns_last_agent_message_when_multiple(self):
        """JSONL multi-turn flows can have multiple ``agent_message``
        events; the helper takes the LAST one. Same behavior as
        :meth:`extract_final_text`."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=["earlier message", "FINAL VERDICT TEXT"])
        extracted = adapter.extract_response_text(stdout)
        assert extracted == "FINAL VERDICT TEXT"

    def test_extract_raises_output_error_on_no_agent_message(self):
        """When the JSONL has zero ``agent_message`` events the
        helper raises :class:`ReviewerOutputError` — the default
        exception class. Mirrors the helper's parse_output
        equivalent for reviewer dispatch."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=[])
        with pytest.raises(ReviewerOutputError):
            adapter.extract_response_text(stdout)

    def test_extract_raises_invocation_error_on_failure_events(self):
        """Explicit failure events in the JSONL stream raise
        :class:`ReviewerInvocationError` (per
        :meth:`extract_final_text`'s contract); the
        default exception class doesn't override that path."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            agent_messages=[],
            turn_failed_message="codex blew up",
        )
        with pytest.raises(ReviewerInvocationError):
            adapter.extract_response_text(stdout)

    def test_the_typed_failure_kind_survives_into_the_message(self):
        """codex's typed variant is the only unambiguous quota signal in the event.

        The message beside it can be generic ("Your request could not be completed"), so
        dropping the type leaves downstream classification guessing from English prose. This
        asserts the end of that chain, not just the prefix: the real classifier must recognise
        the real adapter's real output.
        """
        from syncade.retry import is_usage_limit_error

        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            agent_messages=[],
            turn_failed_message="Your request could not be completed",
            turn_failed_type="UsageLimitReached",
        )
        with pytest.raises(ReviewerInvocationError) as excinfo:
            adapter.extract_response_text(stdout)

        assert "UsageLimitReached" in str(excinfo.value)
        assert is_usage_limit_error(excinfo.value) is True, (
            "the adapter preserved the type but the classifier did not act on it — the two "
            "halves of this fix must stay connected"
        )

    def test_an_untyped_failure_is_not_mistaken_for_a_quota(self):
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=[], turn_failed_message="codex blew up")
        with pytest.raises(ReviewerInvocationError) as excinfo:
            adapter.extract_response_text(stdout)

        from syncade.retry import is_usage_limit_error

        assert is_usage_limit_error(excinfo.value) is False


# ---------------------------------------------------------------------------
# extract_final_text — QA fix #14 (P1.9)
#
# Direct unit tests for the reusable helper PR-7 factored out of
# parse_output. The synthesizer phase (syncade.synthesizer) calls
# this with empty_output_exception_class=SynthesizerOutputError;
# the reviewer phase keeps the original ReviewerOutputError default.
# Before fix #14 the kwarg path was only exercised end-to-end via
# the orchestrator smoke; this class pins the behavior directly so
# a regression surfaces at unit-test time.
# ---------------------------------------------------------------------------


class TestExtractFinalAgentMessageText:
    def _result(
        self,
        stdout: str,
        *,
        returncode: int = 0,
        stderr: str = "",
        duration: float = 0.5,
    ) -> SubprocessResult:
        return SubprocessResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    def test_returns_last_agent_message_text_on_success(self):
        """Happy path: codex emitted multiple agent_message events;
        the helper returns the LAST one's text. The
        empty_output_exception_class kwarg is irrelevant on this
        path."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=["intermediate", "final answer"])
        text = adapter.extract_final_text(
            self._result(stdout),
            empty_output_exception_class=ReviewerOutputError,
        )
        assert text == "final answer"

    def test_recovered_reconnect_error_with_agent_message_succeeds(self):
        """Codex can emit transient reconnect ``error`` events and still
        recover with an agent_message + turn.completed. That shape is a
        successful turn, not an invocation failure."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            agent_messages=["final answer"],
            error_messages=[
                "Reconnecting... 2/5 (stream disconnected before completion: "
                "IO error: Connection reset by peer (os error 54))"
            ],
        )
        text = adapter.extract_final_text(
            self._result(stdout),
            empty_output_exception_class=ReviewerOutputError,
        )
        assert text == "final answer"

    def test_raises_passed_class_on_missing_agent_message_with_reviewer_default(self):
        """Reviewer-dispatch path: no agent_message event → raise
        ReviewerOutputError. Pins the legacy default; CodexAdapter's
        parse_output uses this implicitly. Confirms the kwarg routes
        correctly."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=[])
        with pytest.raises(ReviewerOutputError) as exc_info:
            adapter.extract_final_text(
                self._result(stdout),
                empty_output_exception_class=ReviewerOutputError,
            )
        assert "no agent_message event" in str(exc_info.value)

    def test_raises_passed_class_on_missing_agent_message_with_synth_class(self):
        """Synth-dispatch path: no agent_message event → raise the
        SynthesizerOutputError the caller passed. Pins the
        per-phase routing — the helper does NOT hardcode
        ReviewerOutputError; it instantiates whatever class the
        caller supplies. PR-7's exit-code precedence relies on
        getting the right class here so the orchestrator's decision
        table routes the failure to its phase-appropriate
        diagnostic."""
        from syncade.synthesis import SynthesizerOutputError

        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=[])
        with pytest.raises(SynthesizerOutputError) as exc_info:
            adapter.extract_final_text(
                self._result(stdout),
                empty_output_exception_class=SynthesizerOutputError,
            )
        assert "no agent_message event" in str(exc_info.value)

    def test_subprocess_failure_raises_reviewer_invocation_error_regardless_of_class(self):
        """The subprocess-failure path (turn.failed event) raises
        ReviewerInvocationError independent of the
        empty_output_exception_class kwarg — that kwarg only
        controls the no-message branch. Pin the separation so a
        future refactor doesn't accidentally use the synth class
        for subprocess failures too."""
        from syncade.synthesis import SynthesizerOutputError

        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            agent_messages=[],
            turn_failed_message="codex blew up",
        )
        # The helper raises ReviewerInvocationError REGARDLESS of
        # the empty_output_exception_class kwarg.
        with pytest.raises(ReviewerInvocationError):
            adapter.extract_final_text(
                self._result(stdout, returncode=1),
                empty_output_exception_class=SynthesizerOutputError,
            )

    def test_kwarg_is_keyword_only(self):
        """The kwarg is keyword-only so callers can't accidentally
        pass it positionally and confuse the result type. Belt-and-
        braces pin: passing as a positional argument should TypeError."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=["x"])
        # Positional arg attempt should fail.
        with pytest.raises(TypeError):
            # Python's keyword-only enforcement raises TypeError for
            # positional supply of a keyword-only param.
            adapter.extract_final_text(  # type: ignore[misc]
                self._result(stdout),
                ReviewerOutputError,  # would be positional
            )
