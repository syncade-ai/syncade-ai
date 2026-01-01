"""Test-leg persistence: ``persist_test_run_result`` + the test-run
section of ``persist_round_manifest`` / ``persist_run_summary`` +
``test_skip_reason`` recording.

Moved verbatim from the former ``tests/test_persistence.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence import (
    TEST_RUN_NAME,
    persist_round_manifest,
    persist_run_summary,
    persist_test_run_result,
)
from syncade.persistence import (
    TestRunArtifactPaths as _TestRunArtifactPaths,
)
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _errored_test_result,
    _failed_test_result,
    _make_round_dir,
    _passed_test_result,
    _ship,
    _snapshot,
    _subprocess_result,
    _synth_output_empty,
    _synth_output_with_findings,
    _synth_result,
)


class TestPersistTestRunResult:
    """``persist_test_run_result`` writes three files: stdout, stderr,
    exit-code.txt. All hardcoded basenames (the operator's
    ``test_command`` never reaches the filesystem layer)."""

    def test_passed_writes_three_files_with_zero_exit(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        paths = persist_test_run_result(round_dir, _passed_test_result())
        assert isinstance(paths, _TestRunArtifactPaths)
        assert paths.stdout.read_text() == "==== 12 passed in 8.3s ====\n"
        assert paths.stderr.read_text() == ""
        assert paths.exit_code.read_text() == "0\n"
        # The file basenames are the hardcoded constants (no
        # operator command in the name).
        assert paths.stdout.name == f"{TEST_RUN_NAME}.stdout"
        assert paths.stderr.name == f"{TEST_RUN_NAME}.stderr"
        assert paths.exit_code.name == f"{TEST_RUN_NAME}.exit-code.txt"

    def test_failed_writes_positive_exit_code(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        paths = persist_test_run_result(round_dir, _failed_test_result())
        assert paths.exit_code.read_text() == "1\n"
        assert "FAILED" in paths.stdout.read_text()

    def test_subprocess_error_writes_negative_one_exit_code(self, tmp_path: Path):
        """Sentinel ``-1`` indicates the subprocess was killed
        (timeout) or never produced an exit code (binary missing)."""
        round_dir = _make_round_dir(tmp_path)
        paths = persist_test_run_result(round_dir, _errored_test_result())
        assert paths.exit_code.read_text() == "-1\n"
        # Partial output preserved
        assert paths.stdout.read_text() == "partial\n"

    def test_round_dir_must_exist(self, tmp_path: Path):
        bogus = tmp_path / "missing"
        with pytest.raises(FileNotFoundError, match="round_dir does not exist"):
            persist_test_run_result(bogus, _passed_test_result())

    def test_operator_test_command_does_not_reach_filename(self, tmp_path: Path):
        """Path-traversal defense: even if the operator's
        ``test_command`` is ``../../etc/passwd``, the on-disk file
        names are the hardcoded ``test-run.*`` basenames. The command
        echoes through to ``manifest.json`` only (text echo), never
        as a path component."""
        round_dir = _make_round_dir(tmp_path)
        evil_command = "../../etc/passwd"
        paths = persist_test_run_result(round_dir, _passed_test_result(command=evil_command))
        # The command is preserved on disk as text content, NOT as
        # a path. The three files land at the hardcoded basenames
        # under round_dir.
        assert paths.stdout.parent == round_dir
        assert paths.stderr.parent == round_dir
        assert paths.exit_code.parent == round_dir


class TestPersistRoundManifestTestRunSection:
    """``persist_round_manifest`` gains a ``test_run`` key (null when
    the leg was skipped; populated dict when it ran)."""

    def _ship_dispatch(self) -> DispatchResult:
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

    def test_test_run_null_when_skipped(self, tmp_path: Path):
        """The unconfigured-leg path: test_run is null in manifest."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        manifest_path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=None,
        )
        manifest = json.loads(manifest_path.read_text())
        assert manifest["test_run"] is None

    def test_test_run_passed_schema(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        test_result = _passed_test_result(command="pytest -q --tb=no")
        manifest_path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=test_result,
        )
        manifest = json.loads(manifest_path.read_text())
        section = manifest["test_run"]
        assert section is not None
        assert section["outcome"] == "passed"
        assert section["exit_code"] == 0
        assert section["command"] == "pytest -q --tb=no"
        assert section["duration_seconds"] == 8.3
        assert section["stdout_path"] == "test-run.stdout"
        assert section["stderr_path"] == "test-run.stderr"
        assert section["exit_code_path"] == "test-run.exit-code.txt"
        assert section["error_type"] is None

    def test_test_run_failed_schema(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        manifest_path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=_failed_test_result(),
        )
        manifest = json.loads(manifest_path.read_text())
        section = manifest["test_run"]
        assert section["outcome"] == "failed"
        assert section["exit_code"] == 1
        assert section["error_type"] is None  # tests-failed is NOT a subprocess error

    def test_test_run_subprocess_error_schema_names_exception_class(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        manifest_path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=_errored_test_result(),
        )
        manifest = json.loads(manifest_path.read_text())
        section = manifest["test_run"]
        assert section["outcome"] == "subprocess_error"
        assert section["exit_code"] == -1
        assert section["error_type"] == "SubprocessTimeoutError"


class TestPersistRunSummaryTestSuiteSection:
    """``persist_run_summary`` gains a ``## Test Suite`` subsection
    between the Synthesizer subsection and Next steps. Renders all
    four states: skipped (config), skipped (prior phase), passed,
    failed, subprocess_error."""

    def _ship_dispatch(self) -> DispatchResult:
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

    def _failed_dispatch(self) -> DispatchResult:
        """One failed reviewer (ReviewerInvocationError)."""
        try:
            raise ReviewerInvocationError("auth failed", returncode=1, stdout="", stderr="")
        except ReviewerInvocationError as exc:
            err = exc
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=None,
                    error=err,
                    duration_seconds=0.5,
                    raw_subprocess_result=_subprocess_result(rc=1),
                )
            ],
            total_duration_seconds=0.5,
        )

    def test_section_present_and_skipped_when_test_command_unset(self, tmp_path: Path):
        """Skipped (config opt-out): the most common path. Synth
        succeeded, no test_result, dispatch all_succeeded → message
        names the config opt-in."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=None,
        ).read_text()
        assert "## Test Suite" in text
        # Section between Synthesizer and Next steps
        synth_idx = text.find("## Synthesizer")
        test_idx = text.find("## Test Suite")
        next_idx = text.find("## Next steps")
        assert synth_idx < test_idx < next_idx
        # Message names the config opt-in path
        section = text[test_idx:next_idx]
        assert "skipped" in section.lower()
        assert "test_command" in section
        assert ".syncade/config.toml" in section

    def test_section_renders_skipped_reason_for_reviewer_failure(self, tmp_path: Path):
        """Skipped because a reviewer failed → the message names the
        prior-phase-failure reason, not the config opt-in."""
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._failed_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
        ).read_text()
        section = text[text.find("## Test Suite") : text.find("## Next steps")]
        assert "reviewer failed" in section.lower()

    def test_section_renders_skipped_reason_for_synth_blocker(self, tmp_path: Path):
        """Skipped because synth surfaced an active blocker — exit
        30 from synth alone; the test leg would be wasted compute."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=None,
        ).read_text()
        section = text[text.find("## Test Suite") : text.find("## Next steps")]
        assert "active blocker" in section.lower()

    def test_section_renders_passed(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=_passed_test_result(command="pytest -q"),
        ).read_text()
        section = text[text.find("## Test Suite") : text.find("## Next steps")]
        assert "**Outcome:** passed" in section
        assert "**Exit code:** 0" in section
        assert "**Command:** `pytest -q`" in section
        assert "test-run.stdout" in section
        assert "test-run.exit-code.txt" in section

    def test_section_renders_failed(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=_failed_test_result(),
        ).read_text()
        section = text[text.find("## Test Suite") : text.find("## Next steps")]
        assert "**Outcome:** failed" in section
        assert "**Exit code:** 1" in section

    def test_section_renders_subprocess_error_with_exception_class(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=_errored_test_result(),
        ).read_text()
        section = text[text.find("## Test Suite") : text.find("## Next steps")]
        assert "**Outcome:** subprocess_error" in section
        assert "**Error:** SubprocessTimeoutError" in section


class TestManifestPersistsSkipReason:
    """R2.T2.6: manifest.json includes ``test_skip_reason`` so
    tooling can read WHY the test leg didn't fire without
    inferring from dispatch + synth state. Always present;
    ``null`` when the leg actually ran."""

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

    def test_test_command_unset_recorded(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        manifest_path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._build_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=None,
            test_skip_reason="test_command_unset",
        )
        manifest = json.loads(manifest_path.read_text())
        assert manifest["test_run"] is None
        assert manifest["test_skip_reason"] == "test_command_unset"

    def test_reviewer_failed_recorded(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        manifest_path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._build_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
            test_result=None,
            test_skip_reason="reviewer_failed",
        )
        manifest = json.loads(manifest_path.read_text())
        assert manifest["test_skip_reason"] == "reviewer_failed"

    def test_worktree_error_recorded(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        manifest_path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._build_dispatch(),
            exit_code=60,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=None,
            test_skip_reason="test_worktree_error",
        )
        manifest = json.loads(manifest_path.read_text())
        assert manifest["test_skip_reason"] == "test_worktree_error"

    def test_test_ran_then_skip_reason_null(self, tmp_path):
        """When the leg actually ran, test_skip_reason is null —
        the test_run dict carries the outcome instead."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        manifest_path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._build_dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
            test_result=_passed_test_result(),
            test_skip_reason=None,
        )
        manifest = json.loads(manifest_path.read_text())
        assert manifest["test_run"] is not None
        assert manifest["test_skip_reason"] is None
