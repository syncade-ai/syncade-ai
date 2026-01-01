"""Mechanical-check leg persistence.

Writes ``<round_dir>/<check_name>.check.{stdout,stderr,exit-code.txt}`` for each
configured check that ran, mirroring :func:`persist_test_run_result`'s
file-layout convention. The orchestrator calls this once per check on rounds
where checks ran (reviewers succeeded AND the synth produced output). The
manifest / findings surfacing of these results lives in the round-manifest +
findings_md + run_summary writers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from syncade.synthesis import has_active_blocker
from syncade.test_runner import TestRunResult

from ._atomic import atomic_write_text
from ._validation import _validate_reviewer_filename_basename

if TYPE_CHECKING:
    from syncade.synthesizer import SynthesizerResult


@dataclass(frozen=True)
class CheckArtifactPaths:
    """Where one mechanical check's artifacts land on disk.

    All paths are absolute and rooted at ``<round_dir>``. ``name`` is the
    check's name (a validated plain basename) so a consumer can map the files
    back to the configured check without re-deriving the layout convention.
    """

    name: str
    stdout: Path
    stderr: Path
    exit_code: Path


def persist_check_result(
    round_dir: Path,
    check_result: TestRunResult,
) -> CheckArtifactPaths:
    """Write one check's outputs to
    ``<round_dir>/<name>.check.{stdout,stderr,exit-code.txt}``.

    Mirrors :func:`persist_test_run_result`; the ``.check.`` infix groups the
    files and keeps them distinct from the per-reviewer and test-run artifacts.
    The check name is a validated plain basename (rejected at config-load
    otherwise); this re-validates defensively before using it as a filename so
    a check name can never escape ``round_dir``.

    Args:
        round_dir: The round directory to write into (must already exist).
        check_result: One :class:`~syncade.test_runner.TestRunResult`
            populated with mechanical-check ``name`` and ``severity``.

    Returns:
        :class:`CheckArtifactPaths` naming all three written files.

    Raises:
        FileNotFoundError: If ``round_dir`` does not exist.
    """
    _require_check_metadata(check_result)
    _validate_reviewer_filename_basename(check_result.name)
    if not round_dir.is_dir():
        raise FileNotFoundError(f"round_dir does not exist: {round_dir}")

    base = check_result.name
    stdout_path = round_dir / f"{base}.check.stdout"
    stderr_path = round_dir / f"{base}.check.stderr"
    exit_code_path = round_dir / f"{base}.check.exit-code.txt"

    atomic_write_text(stdout_path, check_result.stdout)
    atomic_write_text(stderr_path, check_result.stderr)
    atomic_write_text(exit_code_path, f"{check_result.exit_code}\n")

    return CheckArtifactPaths(
        name=base,
        stdout=stdout_path,
        stderr=stderr_path,
        exit_code=exit_code_path,
    )


_STATUS_LABEL = {"passed": "PASS", "failed": "FAIL", "subprocess_error": "ERROR"}


def render_checks_section(check_results: list[TestRunResult]) -> list[str]:
    """Render the ``## Mechanical checks`` section (findings.md / summary.md) as
    a list of lines, or ``[]`` when there are no checks.

    Advisory failures are tagged ``(advisory — non-blocking)`` and counted in a
    trailing note; blocking failures read plainly (they drove the verdict). A
    failing/erroring check links its captured output files.
    """
    if not check_results:
        return []
    lines = ["## Mechanical checks", ""]
    advisory_failures = 0
    for c in check_results:
        status = _STATUS_LABEL.get(c.outcome, c.outcome.upper())
        failed = c.outcome != "passed"
        if failed and c.severity == "advisory":
            tag = " (advisory — non-blocking)"
            advisory_failures += 1
        elif failed:
            tag = " (blocking)"
        else:
            tag = ""
        lines.append(f"- **{c.name}** ({c.severity}): {status} — exit {c.exit_code}{tag}  ")
        if failed:
            lines.append(
                f"  Output: [{c.name}.check.stdout]({c.name}.check.stdout) | "
                f"[{c.name}.check.stderr]({c.name}.check.stderr)"
            )
    if advisory_failures:
        noun = "check" if advisory_failures == 1 else "checks"
        lines.append("")
        lines.append(f"Advisory: {advisory_failures} mechanical {noun} failed (non-blocking).")
    lines.append("")
    return lines


def _require_check_metadata(check_result: TestRunResult) -> None:
    if check_result.name is None or check_result.severity is None:
        raise ValueError("check result requires name and severity")


def _check_manifest_entry(check_result: TestRunResult) -> dict[str, object]:
    """Build one ``checks[]`` entry for the round manifest. ``blocking`` is
    surfaced as an explicit boolean so a consumer can filter gating vs advisory
    checks without re-deriving it from ``severity``."""
    _require_check_metadata(check_result)
    return {
        "name": check_result.name,
        "severity": check_result.severity,
        "blocking": check_result.severity == "blocking",
        "outcome": check_result.outcome,
        "exit_code": check_result.exit_code,
        "command": check_result.command,
        "duration_seconds": check_result.duration_seconds,
        "stdout_path": f"{check_result.name}.check.stdout",
        "stderr_path": f"{check_result.name}.check.stderr",
        "exit_code_path": f"{check_result.name}.check.exit-code.txt",
        "error_type": (
            type(check_result.error).__name__ if check_result.error is not None else None
        ),
    }


# check-aware Next-steps for the per-round summary.md.
# When a BLOCKING mechanical check (not a reviewer / synth / test phase)
# drove the round's exit code, the generic per-exit-code guidance points
# the operator at the wrong artifact ("read the active synth blockers" /
# "a reviewer subprocess failed"). These variants point at the check.
_NEXT_STEPS_30_CHECK_FAILED = (
    "- A blocking mechanical check FAILED (the cold synthesizer was\n"
    "  clean — no consolidated blockers). The `## Mechanical checks`\n"
    "  section of `findings.md` names which check failed; open that\n"
    "  check's `<name>.check.stdout` / `<name>.check.stderr` for its\n"
    "  output. This is a NO-SHIP from the mechanical lane, not a\n"
    "  reviewer / synth finding — `findings.md`'s consolidated review\n"
    "  has zero active blockers."
)
_NEXT_STEPS_40_CHECK_SUBPROCESS = (
    "- A blocking mechanical check's subprocess could not run to\n"
    "  completion (every reviewer succeeded; the synthesizer\n"
    "  succeeded). `findings.md` renders Verdict: ABORT — the check\n"
    "  signal is indeterminate until the environment problem is\n"
    "  fixed. Read the failing check's `<name>.check.stderr` and the\n"
    "  manifest's `checks[].error_type`; common shapes are a missing\n"
    "  binary (the check `command` references a tool not on PATH in\n"
    "  the check worktree) or a timeout."
)


def check_aware_next_steps(
    exit_code: int,
    synth_result: SynthesizerResult | None,
    test_result: TestRunResult | None,
    check_results: list[TestRunResult] | None,
) -> str | None:
    """Return a check-driven Next-steps override for the per-round
    summary.md, or ``None`` when the round's failure was NOT driven by a
    blocking mechanical check (the caller keeps its normal routing).

    Fires only for the two check-driven exits — a failing blocking check
    on a synth-clean round (exit 30) and a blocking-check subprocess
    error (exit 40) — and only when no reviewer / synth / test phase is
    the real cause. Every zero-check round passes ``check_results``
    empty and gets ``None`` here, so the generic guidance stays
    byte-identical everywhere else.
    """
    checks = check_results or []
    blocking_failed = any(c.severity == "blocking" and c.outcome == "failed" for c in checks)
    blocking_errored = any(
        c.severity == "blocking" and c.outcome == "subprocess_error" for c in checks
    )
    if exit_code == 40 and blocking_errored:
        synth_failed = synth_result is not None and synth_result.error is not None
        test_errored = test_result is not None and test_result.outcome == "subprocess_error"
        if not synth_failed and not test_errored:
            return _NEXT_STEPS_40_CHECK_SUBPROCESS
    if exit_code == 30 and blocking_failed:
        test_failed = test_result is not None and test_result.outcome == "failed"
        synth_clean = (
            synth_result is not None
            and synth_result.output is not None
            and not has_active_blocker(synth_result.output)
        )
        if synth_clean and not test_failed:
            return _NEXT_STEPS_30_CHECK_FAILED
    return None


# validation fix (codex blocker): the LOOP-summary equivalent of the
# per-round check-aware next-steps. When the FINAL round's NO-SHIP was a
# synth-clean blocking-check failure, loop-summary.md points at the check
# instead of the termination_reason-keyed text (e.g. max_rounds_reached's
# "read the remaining active blockers"), which misleads — there are none.
_LOOP_NEXT_STEPS_CHECK_FAILURE = (
    "- The final round was a NO-SHIP driven by a failing BLOCKING mechanical "
    "check, not by the synthesizer's consolidated findings (which were clean). "
    "Read the `## Mechanical checks` section of that round's `findings.md` to "
    "see which check failed, then open its `<name>.check.stdout` / "
    "`<name>.check.stderr` in the round directory. There are NO active synth "
    "blockers here — the mechanical lane is the NO-SHIP signal."
)


def _final_round_blocking_check_failure(rounds: list) -> bool:
    """True when the LAST round's NO-SHIP was a synth-clean BLOCKING
    mechanical-check failure (duck-typed over ``RoundResult``). Drives the
    loop-summary next-steps override above."""
    if not rounds:
        return False
    r = rounds[-1]
    synth = r.synth_result
    synth_clean = (
        synth is not None and synth.output is not None and not has_active_blocker(synth.output)
    )
    if not synth_clean:
        return False
    if r.test_result is not None and r.test_result.outcome == "failed":
        return False
    return any(c.severity == "blocking" and c.outcome == "failed" for c in r.check_results)
