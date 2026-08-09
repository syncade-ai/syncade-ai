"""Tests for :mod:`syncade.logging` Logger.summary block + pointers.

The Logger has no global state and writes via plain ``print`` — so
every test constructs its own Logger and captures output through
pytest's ``capsys``. No subprocesses, no filesystem.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.dispatcher import ReviewerRunResult
from syncade.logging import Logger
from tests.logging._helpers import (
    _failed_result,
    _run_result,
    _ship_result,
    _synth_parse_failure_result,
    _synth_success_result,
)

# ---------------------------------------------------------------------------
# Summary block (normal mode)
# ---------------------------------------------------------------------------


class TestSummaryExitSeventyPointers:
    """PR-5.6: when a reviewer failed with ReviewerOutputError, the
    final summary block must point the user at the per-reviewer
    .stdout (raw response) and .error.txt (parse exception). The
    Acme 2026-05-15 run exited 70 with a stale "FAILED
    (ReviewerOutputError)" line and no breadcrumbs — the user
    reasonably concluded "claude didn't fire" because parsed.json
    was missing, when in fact the raw NO-SHIP verdict was sitting in
    .stdout the whole time."""

    def _output_error_result(self, name: str = "claude-reviewer") -> ReviewerRunResult:
        from syncade.findings import ReviewerOutputError

        return ReviewerRunResult(
            reviewer_name=name,
            provider="anthropic",
            output=None,
            error=ReviewerOutputError("could not parse"),
            duration_seconds=320.0,
        )

    def test_summary_appends_pointer_for_each_output_error(self, capsys):
        result = _run_result(
            _ship_result("codex-reviewer", "openai"),
            self._output_error_result("claude-reviewer"),
            exit_code=70,
        )
        Logger("normal").summary(result)
        out = capsys.readouterr().out
        # The pointer line names the failing reviewer and both files
        assert "claude-reviewer.stdout" in out
        assert "claude-reviewer.error.txt" in out
        # The line gives the user actionable language about WHERE
        # the verdict might be readable
        assert "raw" in out.lower()
        # Ship reviewer did NOT trigger a pointer (it had no error)
        assert "codex-reviewer.stdout" not in out

    def test_summary_no_pointer_when_no_output_error(self, capsys):
        """A pure SHIP run shouldn't grow exit-70 pointers."""
        result = _run_result(_ship_result(), exit_code=0)
        Logger("normal").summary(result)
        out = capsys.readouterr().out
        assert ".error.txt" not in out
        # The .stdout substring CAN appear in the artifact directory
        # mention; assert the narrative line specifically isn't there.
        assert "raw response" not in out.lower()

    def test_quiet_mode_stays_concise_but_surfaces_exit_70_pointers(self, capsys):
        """PR-5.6 review fix (P0.3): quiet mode keeps the standard
        one-line "run complete" summary AND emits one concise
        terminal pointer per ReviewerOutputError reviewer. Acceptance
        requires .stdout / .error.txt pointers in BOTH terminal and
        summary.md — the previous implementation only put them in
        summary.md, so a quiet user surfacing exit 70 saw nothing
        actionable on the terminal."""
        result = _run_result(
            self._output_error_result("claude-reviewer"),
            exit_code=70,
        )
        Logger("quiet").summary(result)
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        # 2 lines: the summary line + one pointer line for the
        # single ReviewerOutputError reviewer.
        assert len(lines) == 2
        # Standard summary line stays
        assert "summary.md" in lines[0]
        assert "exit 70" in lines[0]
        # Per-reviewer pointer line names both files
        assert "claude-reviewer.stdout" in lines[1]
        assert "claude-reviewer.error.txt" in lines[1]

    def test_quiet_mode_emits_no_pointer_for_non_output_error_runs(self, capsys):
        """Quiet mode for a clean SHIP run is still exactly one line —
        the pointer block fires only for ReviewerOutputError."""
        result = _run_result(_ship_result(), exit_code=0)
        Logger("quiet").summary(result)
        out = capsys.readouterr().out
        assert len(out.strip().splitlines()) == 1
        assert "summary.md" in out

    def test_quiet_mode_one_pointer_line_per_failing_reviewer(self, capsys):
        """Two ReviewerOutputError reviewers produce two pointer lines
        + the standard summary line."""
        result = _run_result(
            self._output_error_result("claude-reviewer"),
            self._output_error_result("codex-reviewer"),
            exit_code=70,
        )
        Logger("quiet").summary(result)
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert len(lines) == 3
        assert "claude-reviewer.stdout" in out
        assert "codex-reviewer.stdout" in out


class TestSummaryBlock:
    def test_summary_block_has_run_id_exit_code_and_reviewers(self, capsys):
        result = _run_result(
            _ship_result("claude-reviewer", "anthropic"),
            _failed_result("codex-reviewer", "openai"),
            exit_code=40,
        )
        Logger("normal").summary(result)
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        # 5-10 line block per the brief
        assert 5 <= len(lines) <= 10
        assert "2026-05-14T10-00-00" in out  # run id
        assert "40" in out  # exit code
        assert "claude-reviewer" in out
        assert "SHIP" in out
        assert "codex-reviewer" in out
        assert "RuntimeError" in out  # failed reviewer's error class
        assert "/tmp/repo/.syncade/runs/2026-05-14T10-00-00" in out  # artifact path
        assert "summary.md" in out  # path of the human-readable summary file


# ---------------------------------------------------------------------------
# PR-7 fix #4: findings.md pointer + SynthesizerOutputError pointer block
# ---------------------------------------------------------------------------


class TestSummaryFindingsMdPointer:
    """PR-7 fix #4: when the synthesizer succeeded, Logger.summary's
    normal-mode block surfaces a ``findings:`` line pointing at the
    operator-facing consolidated review report directly — so the user
    doesn't have to open summary.md, read it, then click through to
    findings.md. Quiet mode keeps the same one-line shape and does
    NOT add a findings line (the brief PR-5.6 contract pins quiet at
    one line for clean runs)."""

    def test_normal_mode_includes_findings_md_line_on_synth_success(self, capsys):
        findings_md = Path("/tmp/repo/.syncade/runs/2026-05-14T10-00-00/round-0/findings.md")
        result = _run_result(
            _ship_result("rv1", "anthropic"),
            _ship_result("rv2", "openai"),
            exit_code=0,
            synth_result=_synth_success_result(),
            findings_md_path=findings_md,
        )
        Logger("normal").summary(result)
        out = capsys.readouterr().out
        assert "findings:" in out
        assert str(findings_md) in out

    def test_normal_mode_omits_findings_md_line_when_synth_skipped(self, capsys):
        """Synth skipped (any reviewer failed) → findings_md_path is
        None → no findings: line. The user's path forward is the
        .error.txt files; findings.md doesn't exist."""
        result = _run_result(
            _ship_result("rv1", "anthropic"),
            _failed_result("rv2", "openai"),
            exit_code=40,
            synth_result=None,
            findings_md_path=None,
        )
        Logger("normal").summary(result)
        out = capsys.readouterr().out
        assert "findings:" not in out

    def test_normal_mode_omits_findings_md_line_when_synth_failed(self, capsys):
        """Synth ran but failed → findings_md_path is None → no
        findings: line. Same reasoning as the skipped case."""
        result = _run_result(
            _ship_result("rv1", "anthropic"),
            _ship_result("rv2", "openai"),
            exit_code=70,
            synth_result=_synth_parse_failure_result(),
            findings_md_path=None,
        )
        Logger("normal").summary(result)
        out = capsys.readouterr().out
        assert "findings:" not in out

    def test_quiet_mode_stays_single_line_when_synth_succeeded(self, capsys):
        """Quiet mode keeps the PR-5.6 contract: exactly one line for
        clean runs. Adding a findings.md pointer would break that;
        the synth-success findings.md pointer is normal-mode only.
        Operators using --quiet who want findings.md follow the
        summary.md link the existing line already points at."""
        findings_md = Path("/tmp/repo/.syncade/runs/2026-05-14T10-00-00/round-0/findings.md")
        result = _run_result(
            _ship_result("rv1", "anthropic"),
            _ship_result("rv2", "openai"),
            exit_code=0,
            synth_result=_synth_success_result(),
            findings_md_path=findings_md,
        )
        Logger("quiet").summary(result)
        out = capsys.readouterr().out
        # Strictly one line per PR-5.6 contract.
        assert len(out.strip().splitlines()) == 1


class TestSummarySynthParseFailurePointers:
    """PR-7 fix #4: SynthesizerOutputError mirrors
    ReviewerOutputError's pointer-line pattern from PR-5.6 — in BOTH
    quiet and normal mode, when the synthesizer's output didn't parse,
    Logger.summary emits a concise pointer line naming
    synthesizer.stdout (raw response) and synthesizer.error.txt
    (parse exception). The synthesizer is single-shot per round so
    there's at most one such line per run.

    Asymmetry vs reviewer pointer: in normal mode, the synth pointer
    block adds a one-line explanation calling out the
    SynthesizerOutputError-specific failure shapes (invented
    findings, unanimous-blocker dismissal attempts, missing required
    field) — those are useful diagnostic anchors that the reviewer
    parse-failure case doesn't have analogues for.
    """

    def test_quiet_mode_appends_synth_pointer_line(self, capsys):
        result = _run_result(
            _ship_result("rv1", "anthropic"),
            _ship_result("rv2", "openai"),
            exit_code=70,
            synth_result=_synth_parse_failure_result(),
            findings_md_path=None,
        )
        Logger("quiet").summary(result)
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        # 1 summary line + 1 synth pointer line = 2 lines total.
        assert len(lines) == 2
        # Path pointer mentions both synthesizer artifacts.
        assert "synthesizer.stdout" in out
        assert "synthesizer.error.txt" in out

    def test_normal_mode_appends_synth_pointer_block(self, capsys):
        result = _run_result(
            _ship_result("rv1", "anthropic"),
            _ship_result("rv2", "openai"),
            exit_code=70,
            synth_result=_synth_parse_failure_result(),
            findings_md_path=None,
        )
        Logger("normal").summary(result)
        out = capsys.readouterr().out
        # Path pointer for the synthesizer
        assert "synthesizer.stdout" in out
        assert "synthesizer.error.txt" in out
        # One-line explanation mentions the synth-specific failure
        # shapes so the user has diagnostic anchors.
        lower = out.lower()
        assert "invented finding" in lower or "unanimous-blocker" in lower

    def test_quiet_no_synth_pointer_on_success(self, capsys):
        result = _run_result(
            _ship_result("rv1", "anthropic"),
            _ship_result("rv2", "openai"),
            exit_code=0,
            synth_result=_synth_success_result(),
            findings_md_path=Path(
                "/tmp/repo/.syncade/runs/2026-05-14T10-00-00/round-0/findings.md"
            ),
        )
        Logger("quiet").summary(result)
        out = capsys.readouterr().out
        assert "synthesizer.stdout" not in out
        assert "synthesizer.error.txt" not in out

    def test_normal_no_synth_pointer_on_success(self, capsys):
        result = _run_result(
            _ship_result("rv1", "anthropic"),
            _ship_result("rv2", "openai"),
            exit_code=0,
            synth_result=_synth_success_result(),
            findings_md_path=Path(
                "/tmp/repo/.syncade/runs/2026-05-14T10-00-00/round-0/findings.md"
            ),
        )
        Logger("normal").summary(result)
        out = capsys.readouterr().out
        assert "For synthesizer:" not in out

    def test_quiet_synth_pointer_AND_reviewer_pointer_coexist(self, capsys):
        """Mixed-failure case the unit-test fixture can synthesize but
        the orchestrator can't reach in production (synth requires
        all-reviewers-success, so a reviewer parse failure would skip
        synth). Still pin the rendering shape: if a caller constructs
        a RunResult with BOTH a reviewer ReviewerOutputError AND a
        synth SynthesizerOutputError, both pointer lines render in
        quiet mode."""
        from syncade.findings import ReviewerOutputError

        review_err = ReviewerRunResult(
            reviewer_name="claude-reviewer",
            provider="anthropic",
            output=None,
            error=ReviewerOutputError("could not parse"),
            duration_seconds=12.0,
        )
        result = _run_result(
            review_err,
            _ship_result("rv2", "openai"),
            exit_code=70,
            synth_result=_synth_parse_failure_result(),
            findings_md_path=None,
        )
        Logger("quiet").summary(result)
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        # 1 summary + 1 reviewer pointer + 1 synth pointer = 3 lines.
        assert len(lines) == 3
        assert "claude-reviewer" in out
        assert "synthesizer.stdout" in out


# ---------------------------------------------------------------------------
# Pre-dispatch no-op runs do not advertise artifact paths
# ---------------------------------------------------------------------------


class TestArtifactPathsAreAlwaysAdvertised:
    """The inverse of a suppression this file used to pin, and the reason it is gone.

    Three separate print sites were suppressed across three dogfood rounds so a refusal would
    not advertise paths the CLI then deleted. That chase only existed because undo removed
    `.syncade/`; it no longer does, so every advertised path survives and suppression is not
    only unnecessary but WRONG — it keyed on `termination_reason`/dispatch state (a property
    of the RUN) while survival depends on whether the directory started empty (a property the
    logger cannot see). Measured: in a pre-existing repo, `no_changes_to_review` keeps its run
    directory, and the suppression hid the pointer to it in the common case.
    """

    @pytest.mark.parametrize(
        ("reason", "code"), [("no_changes_to_review", 0), ("diff_malformed", 60)]
    )
    def test_normal_mode_advertises_artifact_and_summary_paths(self, reason, code, capsys):
        logger = Logger("normal")
        logger.summary(_run_result(termination_reason=reason, exit_code=code))
        out = capsys.readouterr().out
        assert "artifacts:" in out and "summary:" in out, (
            f"{reason} hid the run record; those files are on disk and the operator is not "
            f"told where they are"
        )

    @pytest.mark.parametrize(
        ("reason", "code"), [("no_changes_to_review", 0), ("diff_malformed", 60)]
    )
    def test_quiet_mode_advertises_the_summary_path(self, reason, code, capsys):
        logger = Logger("quiet")
        logger.summary(_run_result(termination_reason=reason, exit_code=code))
        assert "summary at" in capsys.readouterr().out, f"{reason} hid the summary path"
