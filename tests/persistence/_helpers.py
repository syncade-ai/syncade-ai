"""Plain helper functions + shared constants for the persistence test
subdir.

These are *called* (not injected), so each split test file imports the
ones it needs:
``from tests.persistence._helpers import _make_round_dir, _FIXED_STARTED_AT``.
The leading underscore keeps pytest from collecting this as a test module.
``test_persistence`` had no custom fixtures (only built-in ``tmp_path``),
so the persistence subdir needs no conftest.

Moved verbatim from the former ``tests/test_persistence.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from syncade.findings import Finding, ReviewerOutput
from syncade.process import SubprocessResult, SubprocessTimeoutError
from syncade.snapshot import Snapshot
from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput
from syncade.test_runner import TestRunResult as _TestRunResult

# A fixed run-start instant for deterministic timestamp assertions.
# persist_round_manifest / persist_run_summary both take started_at as a
# parameter (run_review captures it once per run), so tests pin it
# rather than racing datetime.now(). Chosen to match _make_round_dir's
# default run-id so the manifest's run_id and timestamp line up.
_FIXED_STARTED_AT = datetime(2026, 5, 12, 15, 30, 4, tzinfo=UTC)


def _subprocess_result(
    stdout: str = "captured stdout\n", stderr: str = "captured stderr\n", rc: int = 0
) -> SubprocessResult:
    return SubprocessResult(
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=1.0,
    )


def _ship() -> ReviewerOutput:
    return ReviewerOutput(
        verdict="SHIP",
        findings=[],
        summary="persistence test SHIP",
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _no_ship_with_finding() -> ReviewerOutput:
    return ReviewerOutput(
        verdict="NO-SHIP",
        findings=[
            Finding(
                severity="blocker",
                file="src/x.py",
                spec_clause="G1",
                finding="missing thing",
            )
        ],
        summary="persistence test NO-SHIP with one blocker",
        priority_order=[0],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _snapshot(
    *,
    base_ref: str | None = None,
    diff_text: str = "",
    branch: str | None = "main",
    dirty: bool = False,
) -> Snapshot:
    # Keep the boolean kwarg on this helper for call-site stability and map it
    # to the tracked dirty state when True.
    return Snapshot(
        repo_root=Path("/tmp/fake-repo"),
        commit_sha="a" * 40,
        branch=branch,
        base_ref=base_ref,
        diff_text=diff_text,
        dirty_state="tracked" if dirty else "clean",
    )


def _make_round_dir(tmp_path: Path, run_id: str = "2026-05-12T15-30-04") -> Path:
    """Make a round_dir at the conventional layout so the manifest's
    `run_id` field can be derived from the parent name."""
    round_dir = tmp_path / ".syncade" / "runs" / run_id / "round-0"
    round_dir.mkdir(parents=True)
    return round_dir


def _find_output_line(lines: list[str], reviewer_name: str) -> str:
    """Return the ``- **Output:** ...`` line that follows the
    ``### <reviewer_name> (...)`` heading in a summary.md."""
    in_section = False
    for line in lines:
        if line.startswith(f"### {reviewer_name} "):
            in_section = True
        elif line.startswith("### "):
            in_section = False
        elif in_section and line.startswith("- **Output:**"):
            return line
    raise AssertionError(f"no Output line found for {reviewer_name!r}")


def _synth_output_empty() -> SynthesizerOutput:
    return SynthesizerOutput(
        consolidated_findings=[],
        synthesis_summary="both reviewers verified the spec; nothing to consolidate",
    )


def _synth_output_with_findings() -> SynthesizerOutput:
    """Two consolidated findings: one active blocker (both reviewers,
    same call), one dismissed minor. Exercises every render path in
    findings.md."""
    return SynthesizerOutput(
        consolidated_findings=[
            ConsolidatedFinding(
                description="user.email column missing NOT NULL constraint",
                file="src/db/schema.sql",
                severity="blocker",
                provenance=[
                    FindingProvenance(
                        reviewer_name="claude-reviewer",
                        original_severity="blocker",
                        original_index=0,
                        original_description="email column nullable; spec says required",
                    ),
                    FindingProvenance(
                        reviewer_name="codex-reviewer",
                        original_severity="blocker",
                        original_index=2,
                        original_description="schema.sql line 14 — email not NULL-protected",
                    ),
                ],
                dismissed=False,
            ),
            ConsolidatedFinding(
                description="repo-wide: README still references gpt-4",
                file=None,  # repo-wide
                severity="nit",
                provenance=[
                    FindingProvenance(
                        reviewer_name="claude-reviewer",
                        original_severity="minor",
                        original_index=3,
                        original_description="README.md line 42 mentions gpt-4",
                    )
                ],
                dismissed=True,
                dismissal_rationale="spec explicitly defers doc updates to phase 04",
                severity_change_rationale="downgraded from minor to nit: doc-only, not behavioral",
            ),
        ],
        synthesis_summary=(
            "Two findings consolidated from claude (4) + codex (3); merged the "
            "unanimous blocker on user.email nullability, passed the README nit "
            "through with dismissal rationale."
        ),
    )


def _synth_result(
    output=None,
    error=None,
    *,
    duration_seconds: float = 18.7,
    raw_stdout: str = "synth stdout\n",
    raw_stderr: str = "synth stderr\n",
    raw_rc: int = 0,
):
    """Build a SynthesizerResult for persistence tests."""
    from syncade.synthesizer import SynthesizerResult

    if output is None and error is None:
        output = _synth_output_empty()
    raw = SubprocessResult(
        returncode=raw_rc,
        stdout=raw_stdout,
        stderr=raw_stderr,
        duration_seconds=duration_seconds,
    )
    return SynthesizerResult(
        output=output,
        error=error,
        duration_seconds=duration_seconds,
        raw_subprocess_result=raw,
    )


def _passed_test_result(command: str = "pytest -q") -> _TestRunResult:
    return _TestRunResult(
        exit_code=0,
        outcome="passed",
        duration_seconds=8.3,
        stdout="==== 12 passed in 8.3s ====\n",
        stderr="",
        command=command,
    )


def _failed_test_result(command: str = "pytest -q") -> _TestRunResult:
    return _TestRunResult(
        exit_code=1,
        outcome="failed",
        duration_seconds=4.5,
        stdout="FAILED tests/test_thing.py::test_x\n",
        stderr="",
        command=command,
    )


def _errored_test_result(command: str = "sleep 30") -> _TestRunResult:
    """Synthesize a subprocess-error TestRunResult with a real
    SubprocessTimeoutError preserved."""
    err = SubprocessTimeoutError("sleep 30 timed out", stdout="partial\n", stderr="", timeout=0.5)
    return _TestRunResult(
        exit_code=-1,
        outcome="subprocess_error",
        duration_seconds=0.5,
        stdout="partial\n",
        stderr="",
        error=err,
        command=command,
    )


def _ship_with_summary(summary: str = "I verified the spec end-to-end.") -> ReviewerOutput:
    """ReviewerOutput with a custom ``summary`` field — used to
    assert findings.md surfaces the verbatim summary text."""
    return ReviewerOutput(
        verdict="SHIP",
        findings=[],
        summary=summary,
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )
