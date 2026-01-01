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
from pathlib import Path

import pytest

from syncade.adapters.anthropic import AnthropicAdapter
from syncade.adapters.base import (
    Invocation,
    ReviewerAdapter,
    ReviewerInvocationError,
)
from syncade.findings import ReviewerOutput, ReviewerOutputError
from syncade.process import SubprocessResult
from tests.adapters._anthropic_helpers import _make_config, _make_envelope_stdout
from tests.test_worktree_env import make_worktree_src, resolve_syncade_in_child

# ---------------------------------------------------------------------------
# build_invocation — argv / cwd / env construction
# ---------------------------------------------------------------------------


class TestBuildInvocation:
    def test_basic_yolo_high_invocation(self, tmp_path):
        adapter = AnthropicAdapter()
        config = _make_config()
        prompt = "review this please"
        inv = adapter.build_invocation(config, tmp_path, prompt)

        assert isinstance(inv, Invocation)
        # argv[0] is the binary; the prompt is positional after `-p`.
        assert inv.argv[0] == "claude"
        assert "-p" in inv.argv
        assert prompt in inv.argv
        # Reviewer now requests the stream-json transcript (+ required
        # --verbose) so the full tool-call stream is captured in .stdout;
        # the adapter extracts the terminal result envelope from it.
        assert "--output-format" in inv.argv
        assert "stream-json" in inv.argv
        assert "json" not in inv.argv  # NOT the single-object format
        assert "--verbose" in inv.argv
        # Model, effort, permission-mode are pinned from config.
        assert "--model" in inv.argv
        assert "claude-opus-4-6" in inv.argv
        assert "--effort" in inv.argv
        assert "high" in inv.argv
        assert "--permission-mode" in inv.argv
        assert "bypassPermissions" in inv.argv
        # The worktree is granted tool access via --add-dir.
        assert "--add-dir" in inv.argv
        assert str(tmp_path) in inv.argv
        # cwd is the worktree; env is inherited; nothing on stdin.
        assert inv.cwd == tmp_path
        assert inv.env  # not empty
        assert inv.stdin_text is None
        assert inv.timeout_seconds is None

    @pytest.mark.parametrize(
        ("permissions", "expected_mode"),
        [
            ("yolo", "bypassPermissions"),
            ("trusted-execute", "bypassPermissions"),
        ],
    )
    def test_permission_mode_mapping(self, tmp_path, permissions, expected_mode):
        adapter = AnthropicAdapter()
        config = _make_config(permissions=permissions)
        inv = adapter.build_invocation(config, tmp_path, "x")
        # Locate the value following --permission-mode
        idx = inv.argv.index("--permission-mode")
        assert inv.argv[idx + 1] == expected_mode

    def test_wrong_provider_is_refused(self, tmp_path):
        """Defensive guard: a ReviewerConfig with provider!='anthropic'
        must NOT reach the argv-building logic. If the dispatcher
        misroutes a Codex (or other-provider) config to this adapter,
        the failure should be loud and actionable, not a confusing
        `claude` CLI error from a malformed invocation."""
        adapter = AnthropicAdapter()
        config = _make_config(provider="openai")
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "x")
        msg = str(exc_info.value)
        assert "AnthropicAdapter" in msg
        assert "anthropic" in msg
        assert "openai" in msg

    def test_permissions_safe_is_refused(self, tmp_path):
        """`permissions='safe'` maps to claude's `--permission-mode
        default`, which prompts for every tool use. In `-p` mode there
        is no human to confirm, so the subprocess would hang on the
        first tool call until syncade's own timeout fires. The adapter
        refuses at build_invocation time rather than letting the
        dispatcher hang."""
        adapter = AnthropicAdapter()
        config = _make_config(permissions="safe")
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "x")
        msg = str(exc_info.value)
        assert "safe" in msg
        assert "trusted-execute" in msg or "yolo" in msg

    def test_permissions_trusted_is_refused_when_schema_bypassed(self, tmp_path):
        adapter = AnthropicAdapter()
        config = _make_config(permissions="yolo").model_copy(update={"permissions": "trusted"})
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "x")
        assert "trusted" in str(exc_info.value)

    @pytest.mark.parametrize("thinking", ["low", "medium", "high", "xhigh", "max"])
    def test_thinking_to_effort_passthrough(self, tmp_path, thinking):
        """PR-8.5 dogfood QA: ``xhigh`` is a valid value for
        ``claude --effort`` on claude 2.1.152 (verified live; the
        ``--help`` output lists ``low, medium, high, xhigh, max``).
        The initial Task 4 implementation rejected ``xhigh`` at
        build-invocation time based on the brief's unverified claim
        that ``claude --effort`` only accepted low/medium/high; the
        first dogfood codex-reviewer surfaced that the brief was
        wrong and the rejection was incorrect. AnthropicAdapter
        now passes ``xhigh`` through verbatim, same as
        :class:`syncade.adapters.openai.CodexAdapter`."""
        adapter = AnthropicAdapter()
        config = _make_config(thinking=thinking)
        inv = adapter.build_invocation(config, tmp_path, "x")
        idx = inv.argv.index("--effort")
        assert inv.argv[idx + 1] == thinking

    def test_model_pins_directly_from_config(self, tmp_path):
        # Both full names and aliases are accepted by the CLI; the
        # adapter shouldn't second-guess the caller.
        adapter = AnthropicAdapter()
        for model in ("haiku", "claude-sonnet-4-6", "opus"):
            config = _make_config(model=model)
            inv = adapter.build_invocation(config, tmp_path, "x")
            idx = inv.argv.index("--model")
            assert inv.argv[idx + 1] == model

    def test_env_inherits_parent_environment(self, tmp_path, monkeypatch):
        # Set a sentinel var in our env; the Invocation.env should
        # contain it (we want existing claude auth in PATH/keychain
        # bindings/etc. to flow through to the subprocess).
        monkeypatch.setenv("SYNCADE_TEST_SENTINEL", "yes")
        adapter = AnthropicAdapter()
        inv = adapter.build_invocation(_make_config(), tmp_path, "x")
        assert inv.env.get("SYNCADE_TEST_SENTINEL") == "yes"
        # PATH must also be present so subprocess can find `claude`.
        assert "PATH" in inv.env

    def test_invocation_env_resolves_worktree_src_not_main(self, tmp_path):
        # PR-23: the reviewer subprocess must import `syncade` from its OWN
        # worktree, not the operator's MAIN src (the editable-install .pth).
        # Proven by a real child process — the .pth wins on sys.path order, so
        # an env-dict check alone wouldn't catch the precedence bug.
        worktree = make_worktree_src(tmp_path / "wt")
        adapter = AnthropicAdapter()
        inv = adapter.build_invocation(_make_config(), worktree, "x")
        resolved = resolve_syncade_in_child(inv.env, worktree)
        assert resolved.startswith(str((worktree / "src" / "syncade").resolve()))

    def test_invocation_is_immutable(self, tmp_path):
        adapter = AnthropicAdapter()
        inv = adapter.build_invocation(_make_config(), tmp_path, "x")
        # Invocation is a frozen dataclass — assigning to a field raises.
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            inv.cwd = Path("/elsewhere")  # type: ignore[misc]

    def test_implements_reviewer_adapter_protocol(self):
        """Runtime-checkable Protocol — the adapter is recognized as a
        ReviewerAdapter without needing explicit subclassing."""
        adapter = AnthropicAdapter()
        assert isinstance(adapter, ReviewerAdapter)
        assert adapter.name == "anthropic"


# ---------------------------------------------------------------------------
# parse_output — happy and unhappy paths against canned SubprocessResults
# ---------------------------------------------------------------------------


class TestParseOutput:
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

    def test_happy_path_bare_json_in_result(self):
        adapter = AnthropicAdapter()
        envelope = _make_envelope_stdout(
            '{"verdict": "SHIP", "findings": [], '
            '"summary": "verified the trivial diff", '
            '"priority_order": [], "coverage_gaps": [], '
            '"dismissed_concerns": []}'
        )
        out = adapter.parse_output(self._result(envelope))
        assert isinstance(out, ReviewerOutput)
        assert out.verdict == "SHIP"
        assert out.findings == []

    def test_happy_path_markdown_fenced_json_in_result(self):
        """The real CLI wraps JSON in ```json fences ~half the time —
        the parser must handle it transparently."""
        adapter = AnthropicAdapter()
        envelope = _make_envelope_stdout(
            '```json\n{"verdict": "NO-SHIP", "findings": [], '
            '"summary": "saw the diff but disagreed", '
            '"priority_order": [], "coverage_gaps": [], '
            '"dismissed_concerns": []}\n```'
        )
        out = adapter.parse_output(self._result(envelope))
        assert out.verdict == "NO-SHIP"

    def test_happy_path_findings_array_populated(self):
        adapter = AnthropicAdapter()
        result_text = (
            '{"verdict": "NO-SHIP", "findings": [{'
            '"severity": "blocker", "file": "src/x.py", "line": 42, '
            '"spec_clause": "G2", "finding": "boom"'
            '}], "summary": "found one blocker on line 42", '
            '"priority_order": [0], "coverage_gaps": [], '
            '"dismissed_concerns": []}'
        )
        envelope = _make_envelope_stdout(result_text)
        out = adapter.parse_output(self._result(envelope))
        assert len(out.findings) == 1
        assert out.findings[0].severity == "blocker"
        assert out.findings[0].line == 42

    def test_nonzero_rc_with_no_envelope_raises_invocation_error(self):
        """CLI-level failure shape: unknown flag, missing prompt. The
        envelope didn't parse; stderr carries the message. The adapter
        surfaces that as a ReviewerInvocationError."""
        adapter = AnthropicAdapter()
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(
                self._result(
                    "",
                    returncode=2,
                    stderr="Error: unknown option '--blah'",
                )
            )
        assert exc_info.value.returncode == 2
        assert "unknown option" in exc_info.value.stderr
        assert "unknown option" in str(exc_info.value)
        assert exc_info.value.api_error_status is None

    def test_nonzero_rc_with_envelope_surfaces_envelope_result(self):
        """The real bad-model failure shape (verified live in QA):
        ``rc=1``, empty stderr, JSON envelope on stdout with
        ``is_error: true``, ``api_error_status: 404``, and the human-
        readable message in ``.result``. The adapter must surface the
        envelope's ``result`` text, not "claude exited with code 1:"
        with nothing after."""
        adapter = AnthropicAdapter()
        envelope = _make_envelope_stdout(
            "There's an issue with the selected model. It may not exist...",
            is_error=True,
            api_error_status=404,
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(self._result(envelope, returncode=1, stderr=""))
        assert exc_info.value.returncode == 1
        assert exc_info.value.api_error_status == 404
        # The actionable envelope text is in the exception message.
        assert "issue with the selected model" in str(exc_info.value)

    def test_rc_zero_with_is_error_true_raises_invocation_error(self):
        """Defensive coverage for an ``rc=0`` + ``is_error: true``
        envelope shape. Note: on ``claude 2.1.137`` the observed
        ``--bare`` missing-auth failure actually returns ``rc=1`` (see
        the corrected discovery doc and ``test_rc_zero_with_is_error_true``
        being a defensive hedge, not a current-version repro). This
        test exists so a future CLI / auth-helper variant that returns
        ``rc=0`` with a failure envelope still gets mapped to
        :class:`ReviewerInvocationError` rather than letting the
        envelope's error text leak through as findings JSON — i.e.
        the adapter must NOT trust ``rc`` alone; ``is_error`` is the
        authoritative signal."""
        adapter = AnthropicAdapter()
        envelope = _make_envelope_stdout(
            "Not logged in · Please run /login",
            is_error=True,
            api_error_status=None,
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(self._result(envelope, returncode=0))
        assert exc_info.value.returncode == 0
        assert "Not logged in" in str(exc_info.value)
        assert exc_info.value.api_error_status is None

    def test_envelope_is_error_with_no_result_text_still_raises(self):
        """Edge case: envelope claims is_error=true but lacks a result
        string. Adapter must still raise ReviewerInvocationError with a
        sensible fallback message."""
        adapter = AnthropicAdapter()
        envelope = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 500,
            }
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(self._result(envelope, returncode=0))
        assert exc_info.value.api_error_status == 500
        # The message should still be useful — not just "claude
        # failed:" with nothing after.
        assert "is_error" in str(exc_info.value).lower() or "500" in str(exc_info.value)

    def test_envelope_api_error_status_non_int_coerces_to_none(self):
        """Defensive: if upstream changes the api_error_status type
        (string, dict, etc.), the attribute contract still holds
        (int | None)."""
        adapter = AnthropicAdapter()
        envelope = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": "404",  # wrong type
                "result": "boom",
            }
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(self._result(envelope, returncode=1))
        assert exc_info.value.api_error_status is None

    def test_malformed_envelope_with_rc_zero_raises_output_error(self):
        adapter = AnthropicAdapter()
        # Stdout isn't JSON; rc=0 — the "success that didn't produce
        # parseable output" path.
        with pytest.raises(ReviewerOutputError) as exc_info:
            adapter.parse_output(self._result("not json at all"))
        assert "envelope" in str(exc_info.value).lower()

    def test_envelope_missing_result_field_raises_output_error(self):
        adapter = AnthropicAdapter()
        envelope = json.dumps({"type": "result", "subtype": "success"})
        with pytest.raises(ReviewerOutputError) as exc_info:
            adapter.parse_output(self._result(envelope))
        assert "result" in str(exc_info.value).lower()

    def test_envelope_result_is_null_raises_output_error(self):
        adapter = AnthropicAdapter()
        envelope = json.dumps({"type": "result", "subtype": "success", "result": None})
        with pytest.raises(ReviewerOutputError):
            adapter.parse_output(self._result(envelope))

    def test_envelope_is_array_not_object_raises_output_error(self):
        adapter = AnthropicAdapter()
        with pytest.raises(ReviewerOutputError):
            adapter.parse_output(self._result('["not", "an", "object"]'))

    def test_unparseable_inner_result_raises_output_error(self):
        adapter = AnthropicAdapter()
        # Envelope parses, .result is a string, but it isn't JSON.
        envelope = _make_envelope_stdout("just some prose, no JSON anywhere")
        with pytest.raises(ReviewerOutputError):
            adapter.parse_output(self._result(envelope))


# ---------------------------------------------------------------------------
# extract_final_text — the provider-agnostic seam (PR-v2-23)
#
# The judge/drafter parse claude's output into shapes that are NOT a
# ReviewerOutput, so they need the text without the reviewer schema. Before
# this method existed on the Protocol, the only way to get it was to name
# CodexAdapter concretely — which is how the judge ended up codex-only.
# ---------------------------------------------------------------------------


class _NotAReviewerError(Exception):
    """Stands in for SynthesizerOutputError / SpecDraftOutputError.

    Deliberately NOT imported from those modules: the point is that the adapter
    raises whatever the caller hands it, without knowing what the caller parses
    into. A test that used the real synth error could pass on an adapter that
    hardcoded it.
    """


class TestExtractFinalText:
    def _result(self, stdout: str, *, returncode: int = 0, stderr: str = "") -> SubprocessResult:
        return SubprocessResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.5,
        )

    def test_returns_envelope_result_text_verbatim(self):
        """Text passes through unparsed — non-ReviewerOutput shapes survive."""
        adapter = AnthropicAdapter()
        synth_json = '{"verdict": "NO-SHIP", "findings": [], "dismissed": []}'
        text = adapter.extract_final_text(
            self._result(_make_envelope_stdout(synth_json)),
            empty_output_exception_class=_NotAReviewerError,
        )
        assert text == synth_json

    def test_parse_output_is_extract_plus_parse(self):
        """The identity that lets parse_output be a thin wrapper. If this
        breaks, the two paths have drifted and the reviewer verdict no longer
        comes from the same text the judge would see."""
        adapter = AnthropicAdapter()
        envelope = _make_envelope_stdout(
            '{"verdict": "SHIP", "findings": [], "summary": "ok", '
            '"priority_order": [], "coverage_gaps": [], "dismissed_concerns": []}'
        )
        from syncade.findings import parse_reviewer_output

        direct = adapter.parse_output(self._result(envelope))
        via_seam = parse_reviewer_output(
            adapter.extract_final_text(
                self._result(envelope),
                empty_output_exception_class=ReviewerOutputError,
            )
        )
        assert direct == via_seam

    def test_is_error_envelope_raises_invocation_error_not_callers_class(self):
        """A subprocess-side failure must stay ReviewerInvocationError (exit 40,
        retry-eligible) — NOT the caller's output-error class (exit 70). Getting
        this backwards would make an auth failure look like a bad model reply and
        silently skip the retry path."""
        adapter = AnthropicAdapter()
        envelope = _make_envelope_stdout("Invalid API key", is_error=True, api_error_status=401)
        with pytest.raises(ReviewerInvocationError) as exc:
            adapter.extract_final_text(
                self._result(envelope, returncode=1),
                empty_output_exception_class=_NotAReviewerError,
            )
        assert exc.value.api_error_status == 401
        assert "Invalid API key" in str(exc.value)

    def test_nonzero_rc_without_envelope_raises_invocation_error(self):
        adapter = AnthropicAdapter()
        with pytest.raises(ReviewerInvocationError):
            adapter.extract_final_text(
                self._result("", returncode=1, stderr="unknown flag"),
                empty_output_exception_class=_NotAReviewerError,
            )

    def test_ran_fine_but_said_nothing_raises_callers_class(self):
        """rc=0, no parseable envelope: the caller's exception type, so the
        failure lands in the caller's taxonomy rather than the adapter's."""
        adapter = AnthropicAdapter()
        with pytest.raises(_NotAReviewerError):
            adapter.extract_final_text(
                self._result("not a json envelope at all"),
                empty_output_exception_class=_NotAReviewerError,
            )

    def test_envelope_with_null_result_raises_callers_class(self):
        adapter = AnthropicAdapter()
        envelope = json.dumps({"type": "result", "is_error": False, "result": None})
        with pytest.raises(_NotAReviewerError):
            adapter.extract_final_text(
                self._result(envelope),
                empty_output_exception_class=_NotAReviewerError,
            )

    def test_same_adapter_honors_a_different_callers_taxonomy(self):
        """The adapter is agnostic about what it's being parsed into: the same
        stdout raises whichever class the caller passed."""
        adapter = AnthropicAdapter()
        bad = self._result("not a json envelope at all")
        with pytest.raises(ReviewerOutputError):
            adapter.extract_final_text(bad, empty_output_exception_class=ReviewerOutputError)
        with pytest.raises(_NotAReviewerError):
            adapter.extract_final_text(bad, empty_output_exception_class=_NotAReviewerError)
