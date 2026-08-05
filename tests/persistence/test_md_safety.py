"""Tests for the markdown-safety helpers in :mod:`syncade.persistence`.

Moved verbatim from the former ``tests/test_persistence.py``: the
inline-code / command-line rendering helpers that keep an operator's
``test_command`` (possibly containing backticks or newlines) from
breaking the code span in summary.md + findings.md.
"""

from __future__ import annotations

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence import (
    persist_findings_md,
    persist_run_summary,
)
from syncade.persistence.decision_needed import persist_decision_needed
from syncade.producer_escalation import ProducerEscalation
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _passed_test_result,
    _ship,
    _ship_with_summary,
    _snapshot,
    _subprocess_result,
    _synth_output_empty,
    _synth_result,
    _TestRunResult,
)


class TestMdInlineCodeBacktickSafe:
    """T2.7: the markdown inline-code helper handles content with
    backticks correctly. Used to render test_result.command in
    summary.md + findings.md."""

    def test_plain_text_uses_single_backtick(self):
        from syncade.persistence import _md_inline_code

        assert _md_inline_code("pytest -q") == "`pytest -q`"

    def test_content_with_one_backtick_uses_double_fence(self):
        """An operator's test_command containing backticks for
        command substitution (legacy shell syntax)."""
        from syncade.persistence import _md_inline_code

        result = _md_inline_code("pytest --basetemp=`mktemp -d` .")
        # Fence is 2 backticks (one longer than the longest internal
        # run of 1 backtick on each side of "mktemp -d").
        assert result == "``pytest --basetemp=`mktemp -d` .``"
        # Sanity: rendering does NOT prematurely close the code span.
        # If single-backtick wrapping were used, parsers would see
        # "pytest --basetemp=" as code, "mktemp -d" as plain text.
        assert "``" in result and not result.startswith("`pytest")

    def test_content_with_double_backticks_uses_triple_fence(self):
        from syncade.persistence import _md_inline_code

        result = _md_inline_code("echo ``two``")
        assert result.startswith("```") and result.endswith("```")
        assert "echo ``two``" in result

    def test_content_starting_with_backtick_pads(self):
        """CommonMark requires padding spaces when the content
        starts or ends with a backtick, else the outer fence and
        inner backtick concatenate ambiguously."""
        from syncade.persistence import _md_inline_code

        result = _md_inline_code("`leading-backtick")
        # Fence-1, space, content, space, fence-1 — but the content
        # has a backtick so fence must be >=2.
        assert result == "`` `leading-backtick ``"

    def test_content_ending_with_backtick_pads(self):
        from syncade.persistence import _md_inline_code

        result = _md_inline_code("trailing-backtick`")
        assert result == "`` trailing-backtick` ``"

    def test_empty_string_renders_as_empty_span(self):
        from syncade.persistence import _md_inline_code

        # Don't emit an unclosed backtick; minimal valid form is ``.
        assert _md_inline_code("") == "``"


class TestCommandWithBackticksRendersSafely:
    """T2.7: end-to-end check — a test_command containing
    backticks renders into summary.md + findings.md without
    breaking the code span."""

    def _build_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship_with_summary("ok"),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_summary_md_renders_backtick_command_safely(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        tr = _passed_test_result(command="pytest --basetemp=`mktemp -d`")
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._build_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=tr,
        ).read_text()
        section = text[text.find("## Test Suite") : text.find("## Next steps")]
        # The full command text appears intact (not split by the
        # inner backtick closing the span).
        assert "pytest --basetemp=`mktemp -d`" in section
        # The wrapping uses >=2 backticks (the helper).
        # Wrapping uses 2 backticks (one longer than internal
        # backtick run). Content ends in a backtick so the helper
        # also pads with a leading/trailing space per CommonMark.
        assert "`` pytest --basetemp=`mktemp -d` ``" in section

    def test_findings_md_renders_backtick_command_safely(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        tr = _passed_test_result(command="pytest --basetemp=`mktemp -d`")
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, tr, self._build_dispatch()
        ).read_text()
        ts = text[text.find("## Test Suite") : text.find("## Synthesis summary")]
        assert "pytest --basetemp=`mktemp -d`" in ts
        assert "`` pytest --basetemp=`mktemp -d` ``" in ts


class TestMultilineTestCommandRendering:
    """R2.T3.11: TOML triple-quoted commands can span newlines.
    Inline-code rendering breaks on newlines; multi-line content
    must use a fenced code block."""

    def _passed_multiline(self) -> _TestRunResult:
        return _TestRunResult(
            exit_code=0,
            outcome="passed",
            duration_seconds=1.0,
            stdout="",
            stderr="",
            command="cd subdir\npytest -q\n",
        )

    def _build_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_md_command_lines_single_line(self):
        from syncade.persistence import _md_command_lines

        result = _md_command_lines("pytest -q")
        # Single line; no prefix, no inline_suffix → exact form
        assert result == ["**Command:** `pytest -q`"]

    def test_md_command_lines_single_line_with_prefix_and_suffix(self):
        from syncade.persistence import _md_command_lines

        result = _md_command_lines("pytest -q", prefix="- ", inline_suffix="  ")
        assert result == ["- **Command:** `pytest -q`  "]

    def test_md_command_lines_multiline_uses_fenced_block(self):
        from syncade.persistence import _md_command_lines

        result = _md_command_lines("cd subdir\npytest -q\n", prefix="- ")
        # Returns multiple lines: label, blank, ```sh, command, ```
        assert result[0] == "- **Command:**"
        assert result[1] == ""
        assert result[2] == "```sh"
        # Trailing newline stripped before the content
        assert result[3] == "cd subdir\npytest -q"
        assert result[4] == "```"

    def test_md_command_lines_multiline_with_inner_backticks_bumps_fence(self):
        from syncade.persistence import _md_command_lines

        # Command with a triple-backtick inside (e.g. shell-style
        # multi-line string) requires a 4-backtick fence.
        result = _md_command_lines("echo '```sh\nfoo\n```'\n")
        # Find the fences
        fence_lines = [line for line in result if line.startswith("```") or line.startswith("````")]
        # At least one fence longer than 3 backticks
        assert any(len(line) >= 4 and line.startswith("````") for line in fence_lines), (
            f"fence should be >= 4 backticks when content has triple-backtick; got: {result}"
        )

    def test_summary_md_renders_multiline_command_as_fenced_block(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._build_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=self._passed_multiline(),
        ).read_text()
        section = text[text.find("## Test Suite") : text.find("## Next steps")]
        # The command's content (both lines) must appear verbatim
        assert "cd subdir" in section
        assert "pytest -q" in section
        # Inside a fenced code block (not inline)
        assert "```sh" in section
        # And the inline form must NOT have been used (which
        # would have produced literal `cd subdir\npytest -q`
        # in a single backtick span).
        assert "`cd subdir\npytest -q`" not in section

    def test_findings_md_renders_multiline_command_as_fenced_block(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            self._passed_multiline(),
            self._build_dispatch(),
        ).read_text()
        ts = text[text.find("## Test Suite") : text.find("## Synthesis summary")]
        assert "cd subdir" in ts
        assert "pytest -q" in ts
        assert "```sh" in ts


class TestDecisionNeededMarkdownNeutralization:
    def test_decision_needed_renders_operator_text_inside_fences(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        escalation = ProducerEscalation(
            finding_indices=[0],
            finding="## injected finding\n- fake action",
            decision="```md\n# close the fence\n```",
            options=["- pretend this is a real instruction", "```\nraw fence\n```"],
            rationale="# fake rationale\n1. not a real step",
        )

        text = persist_decision_needed(
            run_dir,
            round_idx=0,
            escalation=escalation,
            run_id="run-1",
        ).read_text()

        assert "## The finding\n\n````text\n## injected finding\n- fake action\n````" in text
        assert (
            "## The decision you must make\n\n````text\n```md\n# close the fence\n```\n````" in text
        )
        assert "### Option 1\n\n````text\n- pretend this is a real instruction\n````" in text
        assert "### Option 2\n\n````text\n```\nraw fence\n```\n````" in text
        assert (
            "## The producer's rationale (reproduction-backed)\n\n````text\n# fake rationale"
            in text
        )

    def test_decision_needed_includes_failing_blocking_check(self, tmp_path):
        """When check_results has a failing blocking check, decision-needed.md
        must include a 'Co-failing blocking checks' section so the operator
        knows the check must also pass after resume, not just the producer decision."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        escalation = ProducerEscalation(
            finding_indices=[0],
            finding="the finding",
            decision="the decision",
            options=["option A"],
            rationale="the rationale",
        )
        check = _TestRunResult(
            exit_code=1,
            outcome="failed",
            duration_seconds=0.5,
            stdout="lint errors\n",
            stderr="",
            name="lint",
            severity="blocking",
        )

        text = persist_decision_needed(
            run_dir,
            round_idx=0,
            escalation=escalation,
            run_id="run-1",
            check_results=[check],
        ).read_text()

        assert "## Co-failing blocking checks" in text
        assert "**lint**" in text
        assert "FAIL" in text
        assert "rerun" in text

    def test_decision_needed_no_check_section_when_no_failing_checks(self, tmp_path):
        """When there are no failing blocking checks, 'Co-failing blocking checks'
        must not appear in decision-needed.md."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        escalation = ProducerEscalation(
            finding_indices=[0],
            finding="the finding",
            decision="the decision",
            options=["option A"],
            rationale="the rationale",
        )

        text = persist_decision_needed(
            run_dir,
            round_idx=0,
            escalation=escalation,
            run_id="run-1",
            check_results=[],
        ).read_text()

        assert "Co-failing blocking checks" not in text
