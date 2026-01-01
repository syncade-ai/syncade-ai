"""Tests for :class:`syncade.adapters.anthropic.AnthropicAdapter`.

Pure unit tests — no real ``claude`` subprocess is spawned here. The
end-to-end smoke test against the real CLI lives in ``tests/smoke/``
and runs only when ``pytest -m smoke`` is invoked explicitly (see
Task 7).

``build_invocation`` tests assert argv shape, env inheritance, and
cwd. ``parse_output`` tests feed canned :class:`SubprocessResult`
objects and assert the parsed :class:`ReviewerOutput` (or the right
exception type for failure paths).
"""

from __future__ import annotations

import json

import pytest

from syncade.adapters.anthropic import AnthropicAdapter
from syncade.adapters.base import ReviewerInvocationError
from syncade.findings import ReviewerOutput, ReviewerOutputError
from syncade.process import SubprocessResult
from tests.adapters._anthropic_helpers import _make_envelope_stdout, _make_stream_stdout

# ---------------------------------------------------------------------------
# PR-14: extract_response_text — envelope-strip helper for cross-round
# context. Round-trip test: feed a known envelope, assert the extracted
# text equals what `.result` contains.
# ---------------------------------------------------------------------------


class TestExtractResponseText:
    """PR-14 Task 4: :meth:`AnthropicAdapter.extract_response_text`
    returns the envelope's ``.result`` field for the orchestrator's
    prior-round-context plumbing. Pairs with
    :class:`syncade.adapters.openai.CodexAdapter.extract_response_text`
    as the symmetric per-adapter interface."""

    def test_round_trip_envelope_result_extracted_verbatim(self):
        """Feed a known envelope; assert the extracted text equals
        what ``.result`` contains. Round-trip pin: a future change to
        the extraction path would surface here."""
        adapter = AnthropicAdapter()
        original_text = (
            "Here's my review:\n\nFinding 1 — null check missing at "
            'src/foo.py:42.\n\n```json\n{"verdict": "NO-SHIP"}\n```'
        )
        envelope_stdout = _make_envelope_stdout(original_text)
        extracted = adapter.extract_response_text(envelope_stdout)
        assert extracted == original_text, (
            "extract_response_text must return the envelope's .result "
            "field verbatim — no markdown unwrapping, no JSON parsing, "
            "no whitespace stripping"
        )

    def test_extract_returns_str_not_subprocessresult(self):
        """The helper's return type is ``str`` — the same shape the
        orchestrator's prior_round.py wiring expects to splice into
        ``{prior_round_output}``."""
        adapter = AnthropicAdapter()
        envelope_stdout = _make_envelope_stdout("hello")
        assert isinstance(adapter.extract_response_text(envelope_stdout), str)

    def test_extract_raises_on_unparseable_json(self):
        """Bad JSON in the stdout raises :class:`ReviewerOutputError`
        — the orchestrator's prior-round loader catches it and falls
        back to passing the raw stdout to the prompt."""
        adapter = AnthropicAdapter()
        with pytest.raises(ReviewerOutputError):
            adapter.extract_response_text("not json at all")

    def test_extract_raises_on_missing_result_field(self):
        """An envelope without ``.result`` is a schema break —
        :class:`ReviewerOutputError`."""
        adapter = AnthropicAdapter()
        envelope = json.dumps({"type": "result", "subtype": "success"})
        with pytest.raises(ReviewerOutputError):
            adapter.extract_response_text(envelope)

    def test_extract_raises_on_non_dict_top_level(self):
        """A JSON array at the top level (not a dict) is also a
        schema break."""
        adapter = AnthropicAdapter()
        with pytest.raises(ReviewerOutputError):
            adapter.extract_response_text('["not", "an", "object"]')

    def test_parse_output_delegates_to_extract_response_text(self):
        """PR-14 T4 refactored :meth:`parse_output` to call
        :meth:`extract_response_text` on the success path. Sanity:
        a happy-path envelope still produces a valid ReviewerOutput
        — the refactor preserved behavior."""
        adapter = AnthropicAdapter()
        envelope = _make_envelope_stdout(
            '{"verdict": "SHIP", "findings": [], '
            '"summary": "delegated successfully", '
            '"priority_order": [], "coverage_gaps": [], '
            '"dismissed_concerns": []}'
        )
        out = adapter.parse_output(
            SubprocessResult(
                returncode=0,
                stdout=envelope,
                stderr="",
                duration_seconds=0.1,
            )
        )
        assert out.verdict == "SHIP"
        assert out.summary == "delegated successfully"


# ---------------------------------------------------------------------------
# Documentation surface — every public method has a docstring that
# round-trips through help(), per the brief.
# ---------------------------------------------------------------------------


def test_public_surface_has_docstrings():
    import inspect

    assert inspect.getdoc(AnthropicAdapter)
    assert inspect.getdoc(AnthropicAdapter.build_invocation)
    assert inspect.getdoc(AnthropicAdapter.parse_output)
    # PR-14: extract_response_text is part of the public surface.
    assert inspect.getdoc(AnthropicAdapter.extract_response_text)


# ---------------------------------------------------------------------------
# stream-json transcript parsing (reviewer transcript preservation)
# ---------------------------------------------------------------------------

_SHIP_JSON = (
    '{"verdict":"SHIP","findings":[],"summary":"verified",'
    '"priority_order":[],"coverage_gaps":[],"dismissed_concerns":[]}'
)


class TestStreamJsonParsing:
    """The reviewer adapter requests `--output-format stream-json --verbose`
    so the persisted `.stdout` is the full tool-call transcript. parse_output
    and extract_response_text must extract the terminal `type:"result"`
    envelope from the JSONL stream, while staying back-compatible with a
    single-object `json` envelope (the legacy format / existing fixtures)."""

    def test_parse_output_extracts_verdict_from_stream(self):
        adapter = AnthropicAdapter()
        stdout = _make_stream_stdout(_SHIP_JSON, with_tool_use=True)
        result = SubprocessResult(returncode=0, stdout=stdout, stderr="", duration_seconds=1.0)
        out = adapter.parse_output(result)
        assert isinstance(out, ReviewerOutput)
        assert out.verdict == "SHIP"

    def test_extract_response_text_from_stream(self):
        adapter = AnthropicAdapter()
        stdout = _make_stream_stdout(_SHIP_JSON, with_tool_use=True)
        assert adapter.extract_response_text(stdout) == _SHIP_JSON

    def test_stream_is_error_raises_invocation_error(self):
        adapter = AnthropicAdapter()
        stdout = _make_stream_stdout("Not logged in", is_error=True, with_tool_use=False)
        result = SubprocessResult(returncode=1, stdout=stdout, stderr="", duration_seconds=1.0)
        with pytest.raises(ReviewerInvocationError) as exc:
            adapter.parse_output(result)
        assert "Not logged in" in str(exc.value)

    def test_salvages_verdict_before_chatty_epilogue(self):
        # REGRESSION (run 2026-05-30T21-22-19 round 1): claude delivered its
        # verdict, then emitted a SPURIOUS second result turn with no JSON
        # ("My review is already complete..."), so taking only the LAST result
        # event lost the verdict and aborted the run at exit 70. The verdict in
        # an EARLIER result event must still be recovered: all result-event
        # texts are joined and parse_reviewer_output picks the final valid block.
        adapter = AnthropicAdapter()
        verdict = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": _SHIP_JSON}
        )
        chatty = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "My review is already complete and the verdict was delivered.",
            }
        )
        stdout = "\n".join([json.dumps({"type": "system"}), verdict, chatty]) + "\n"
        result = SubprocessResult(returncode=0, stdout=stdout, stderr="", duration_seconds=1.0)
        assert adapter.parse_output(result).verdict == "SHIP"

    def test_latest_valid_verdict_wins_across_result_events(self):
        # If two result turns BOTH carry a valid verdict, the LATER one wins
        # (parse_reviewer_output's "final valid block" semantics, now applied
        # across result events).
        adapter = AnthropicAdapter()
        no_ship = (
            '{"verdict":"NO-SHIP","findings":[],"summary":"draft",'
            '"priority_order":[],"coverage_gaps":[],"dismissed_concerns":[]}'
        )
        e1 = json.dumps({"type": "result", "is_error": False, "result": no_ship})
        e2 = json.dumps({"type": "result", "is_error": False, "result": _SHIP_JSON})
        stdout = "\n".join([e1, e2]) + "\n"
        result = SubprocessResult(returncode=0, stdout=stdout, stderr="", duration_seconds=1.0)
        assert adapter.parse_output(result).verdict == "SHIP"

    def test_back_compat_single_object_envelope_still_parses(self):
        # The legacy `--output-format json` single object must still work.
        adapter = AnthropicAdapter()
        stdout = _make_envelope_stdout(_SHIP_JSON)
        result = SubprocessResult(returncode=0, stdout=stdout, stderr="", duration_seconds=1.0)
        assert adapter.parse_output(result).verdict == "SHIP"
        assert adapter.extract_response_text(stdout) == _SHIP_JSON

    def test_no_result_envelope_in_stream_is_output_error(self):
        # A stream with no terminal result line (rc=0) is exit-70 territory.
        adapter = AnthropicAdapter()
        stdout = json.dumps({"type": "system", "subtype": "init"}) + "\n"
        result = SubprocessResult(returncode=0, stdout=stdout, stderr="", duration_seconds=1.0)
        with pytest.raises(ReviewerOutputError):
            adapter.parse_output(result)
