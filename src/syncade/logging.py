"""Small stdout/stderr progress logger for syncade runs."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from syncade.findings import ReviewerOutputError
from syncade.synthesis import SynthesizerOutputError

if TYPE_CHECKING:
    from syncade.orchestrator import RunResult

LogLevel = Literal["quiet", "normal"]


def _timestamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def _is_budget_stop(run_result: RunResult) -> bool:
    return getattr(run_result, "termination_reason", None) == "budget_exceeded"


def _budget_stop_line(run_result: RunResult) -> str:
    """One-line budget-abort notice (PR-v2-11), shown even under ``--quiet``. Points to the
    loop-summary Budget section rather than reprinting the tally, and says API-EQUIVALENT so
    the terminal never implies a subscription run spent real money."""
    loop_summary = run_result.artifacts.run_dir / "loop-summary.md"
    return (
        f"[syncade] stopped early: budget exceeded — see the Budget section of "
        f"{loop_summary} for the API-equivalent tally (a valuation, not billed money; "
        f"a lower bound if any cost was unpriced)."
    )


class Logger:
    """Timestamped stdout/stderr logger with quiet-mode suppression."""

    def __init__(self, level: LogLevel = "normal") -> None:
        self.level = level

    def _emit(self, line: str) -> None:
        if self.level != "quiet":
            print(f"{_timestamp()} {line}", flush=True)

    def _emit_err(self, line: str) -> None:
        print(f"{_timestamp()} {line}", file=sys.stderr, flush=True)

    def _emit_warning(self, line: str) -> None:
        if self.level != "quiet":
            print(f"{_timestamp()} {line}", file=sys.stderr, flush=True)

    def warning(self, message: str) -> None:
        self._emit_warning(f"warning: {message}")

    def event(self, message: str, *, error: bool = False) -> None:
        if error:
            self._emit_err(message)
        else:
            self._emit(message)

    def summary(self, run_result: RunResult) -> None:
        """Print the final run summary, including parse-error artifact pointers."""
        artifacts = run_result.artifacts
        round_dir = artifacts.round_dir
        output_error_reviewers = [
            r
            for r in run_result.dispatch_result.results
            if isinstance(r.error, ReviewerOutputError)
        ]
        synth_result = run_result.synth_result
        synth_output_error = synth_result is not None and isinstance(
            synth_result.error, SynthesizerOutputError
        )

        budget_note = _budget_stop_line(run_result) if _is_budget_stop(run_result) else None

        if self.level == "quiet":
            print(
                f"[syncade] run complete — exit {run_result.exit_code}, "
                f"summary at {artifacts.summary_path}",
                flush=True,
            )
            if budget_note is not None:
                print(budget_note, flush=True)
            for r in output_error_reviewers:
                print(
                    f"[syncade] {r.reviewer_name} raw response: "
                    f"{round_dir / f'{r.reviewer_name}.stdout'}; parse exception: "
                    f"{round_dir / f'{r.reviewer_name}.error.txt'}",
                    flush=True,
                )
            if synth_output_error:
                print(
                    f"[syncade] synthesizer raw response: "
                    f"{round_dir / 'synthesizer.stdout'}; parse exception: "
                    f"{round_dir / 'synthesizer.error.txt'}",
                    flush=True,
                )
            return

        lines = [
            f"{_timestamp()} run complete",
            f"  run id:    {artifacts.run_dir.name}",
            f"  exit code: {run_result.exit_code}",
        ]
        for r in run_result.dispatch_result.results:
            if r.output is not None:
                lines.append(f"  - {r.reviewer_name} ({r.provider}): {r.output.verdict}")
            else:
                err_cls = type(r.error).__name__ if r.error else "Unknown"
                lines.append(f"  - {r.reviewer_name} ({r.provider}): FAILED ({err_cls})")
        lines.append(f"  artifacts: {artifacts.run_dir}")
        lines.append(f"  summary:   {artifacts.summary_path}")
        if artifacts.findings_md_path is not None:
            lines.append(f"  findings:  {artifacts.findings_md_path}")
        if budget_note is not None:
            lines.append(budget_note)

        for r in output_error_reviewers:
            lines.append(
                f"  For {r.reviewer_name}: raw response preserved at "
                f"{round_dir / f'{r.reviewer_name}.stdout'}; parse exception at "
                f"{round_dir / f'{r.reviewer_name}.error.txt'}."
            )
            lines.append(
                "    The reviewer ran successfully but its output didn't parse — the verdict "
                "and findings may still be readable in the raw .stdout file."
            )

        if synth_output_error:
            lines.append(
                f"  For synthesizer: raw response preserved at "
                f"{round_dir / 'synthesizer.stdout'}; parse exception at "
                f"{round_dir / 'synthesizer.error.txt'}."
            )
            lines.append(
                "    The synthesizer ran successfully but its output didn't parse — common "
                "shapes are invented findings, unanimous-blocker deactivation attempts, or "
                "missing required fields."
            )

        print("\n".join(lines), flush=True)
