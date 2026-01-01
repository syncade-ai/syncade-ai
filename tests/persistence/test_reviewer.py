"""Tests for :mod:`syncade.persistence`.

Constructs :class:`ReviewerRunResult` / :class:`DispatchResult` /
:class:`Snapshot` / :class:`SubprocessResult` directly — no real
subprocess calls or git operations. The orchestrator is the only
production caller; these tests target the persistence module in
isolation so a future regression in file-layout, JSON shape, or
manifest schema fails here rather than at the integration boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.dispatcher import ReviewerRunResult
from syncade.findings import ReviewerOutput, ReviewerOutputError
from syncade.persistence import persist_reviewer_result
from tests.persistence._helpers import (
    _make_round_dir,
    _no_ship_with_finding,
    _ship,
    _subprocess_result,
)


class TestPersistReviewerResultSuccess:
    def test_writes_stdout_stderr_and_parsed_json(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        run = ReviewerRunResult(
            reviewer_name="claude-reviewer",
            provider="anthropic",
            output=_ship(),
            error=None,
            duration_seconds=4.2,
            raw_subprocess_result=_subprocess_result(),
        )
        persist_reviewer_result(round_dir, run, run.raw_subprocess_result)

        assert (round_dir / "claude-reviewer.stdout").read_text() == "captured stdout\n"
        assert (round_dir / "claude-reviewer.stderr").read_text() == "captured stderr\n"
        # parsed.json round-trips through pydantic — content should match
        parsed_path = round_dir / "claude-reviewer.parsed.json"
        assert parsed_path.is_file()
        parsed = ReviewerOutput.model_validate_json(parsed_path.read_text())
        assert parsed.verdict == "SHIP"
        # No error file on the success path
        assert not (round_dir / "claude-reviewer.error.txt").exists()

    def test_parsed_json_is_pretty_printed(self, tmp_path: Path):
        """model_dump_json(indent=2) is required for stable diffs across
        runs — single-line JSON would churn every run. Test by counting
        newlines: a SHIP with one finding should be 10+ lines."""
        round_dir = _make_round_dir(tmp_path)
        run = ReviewerRunResult(
            reviewer_name="rv1",
            provider="anthropic",
            output=_no_ship_with_finding(),
            error=None,
            duration_seconds=2.0,
            raw_subprocess_result=_subprocess_result(),
        )
        persist_reviewer_result(round_dir, run, run.raw_subprocess_result)
        text = (round_dir / "rv1.parsed.json").read_text()
        assert text.count("\n") >= 8  # indented JSON has many newlines


# ---------------------------------------------------------------------------
# persist_reviewer_result — failure path
# ---------------------------------------------------------------------------


class TestPersistReviewerResultFailure:
    def test_writes_error_txt_with_class_and_message(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        # Construct then raise+catch so the exception carries a traceback,
        # matching the dispatcher's real path.
        try:
            raise ReviewerInvocationError(
                "claude auth failed",
                returncode=1,
                stdout="...",
                stderr="Please run /login",
            )
        except ReviewerInvocationError as exc:
            err = exc
        run = ReviewerRunResult(
            reviewer_name="rv1",
            provider="anthropic",
            output=None,
            error=err,
            duration_seconds=0.5,
            raw_subprocess_result=_subprocess_result(rc=1),
        )
        persist_reviewer_result(round_dir, run, run.raw_subprocess_result)

        error_text = (round_dir / "rv1.error.txt").read_text()
        # Class name AND message both present (the brief calls this out
        # specifically — exit-code routing tooling buckets by class name).
        assert "ReviewerInvocationError" in error_text
        assert "claude auth failed" in error_text
        # Traceback is included when available
        assert "Traceback" in error_text
        # stdout/stderr also written
        assert (round_dir / "rv1.stdout").is_file()
        assert (round_dir / "rv1.stderr").is_file()
        # No parsed.json on the failure path
        assert not (round_dir / "rv1.parsed.json").exists()

    def test_writes_error_txt_for_parse_failure(self, tmp_path: Path):
        """ReviewerOutputError surfaces the same way structurally,
        with a different class name — that's what downstream tooling
        keys on."""
        round_dir = _make_round_dir(tmp_path)
        run = ReviewerRunResult(
            reviewer_name="rv1",
            provider="openai",
            output=None,
            error=ReviewerOutputError("garbage in stdout"),
            duration_seconds=3.0,
            raw_subprocess_result=_subprocess_result(),
        )
        persist_reviewer_result(round_dir, run, run.raw_subprocess_result)
        error_text = (round_dir / "rv1.error.txt").read_text()
        assert "ReviewerOutputError" in error_text
        assert "garbage in stdout" in error_text


# ---------------------------------------------------------------------------
# persist_reviewer_result — no subprocess ran
# ---------------------------------------------------------------------------


class TestPersistWithoutSubprocessResult:
    def test_stdout_stderr_files_empty_when_no_subprocess_ran(self, tmp_path: Path):
        """Auth fail-fast and unknown-provider failures happen BEFORE
        any subprocess launches, so raw_subprocess_result is None.
        Persistence still writes stdout/stderr files (empty) so the
        downstream tooling can rely on file presence without
        special-casing."""
        round_dir = _make_round_dir(tmp_path)
        run = ReviewerRunResult(
            reviewer_name="rv1",
            provider="not-a-provider",
            output=None,
            error=ValueError("unknown reviewer provider 'not-a-provider'"),
            duration_seconds=0.0,
            raw_subprocess_result=None,
        )
        persist_reviewer_result(round_dir, run, None)

        # stdout/stderr present but empty
        assert (round_dir / "rv1.stdout").read_text() == ""
        assert (round_dir / "rv1.stderr").read_text() == ""
        # error.txt names the ValueError
        error_text = (round_dir / "rv1.error.txt").read_text()
        assert "ValueError" in error_text
        assert "not-a-provider" in error_text
        assert not (round_dir / "rv1.parsed.json").exists()


# ---------------------------------------------------------------------------
# persist_reviewer_result — input validation
# ---------------------------------------------------------------------------


class TestPersistReviewerResultValidation:
    def test_round_dir_must_exist(self, tmp_path: Path):
        bogus = tmp_path / "does-not-exist"
        run = ReviewerRunResult(
            reviewer_name="rv1",
            provider="anthropic",
            output=_ship(),
            error=None,
            duration_seconds=1.0,
            raw_subprocess_result=_subprocess_result(),
        )
        with pytest.raises(FileNotFoundError):
            persist_reviewer_result(bogus, run, run.raw_subprocess_result)

    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            ".",
            "..",
            "../escape",
            "subdir/file",
            "a\\b",
            "/absolute",
        ],
    )
    def test_path_traversal_in_reviewer_name_is_refused(self, tmp_path: Path, bad_name: str):
        round_dir = _make_round_dir(tmp_path)
        run = ReviewerRunResult(
            reviewer_name=bad_name,
            provider="anthropic",
            output=_ship(),
            error=None,
            duration_seconds=1.0,
            raw_subprocess_result=_subprocess_result(),
        )
        with pytest.raises(ValueError) as exc_info:
            persist_reviewer_result(round_dir, run, run.raw_subprocess_result)
        assert "basename" in str(exc_info.value).lower()
