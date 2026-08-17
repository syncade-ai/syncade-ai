"""Small stdout/stderr progress logger for syncade runs."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from syncade.billing import from_usages as _billing_from_usages
from syncade.findings import ReviewerOutputError
from syncade.retry import is_usage_limit_error
from syncade.synthesis import SynthesizerOutputError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from syncade.orchestrator import RunResult

LogLevel = Literal["quiet", "normal"]


def _timestamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def _is_budget_stop(run_result: RunResult) -> bool:
    return getattr(run_result, "termination_reason", None) == "budget_exceeded"


def _budget_stop_line(run_result: RunResult) -> str:
    """The budget-abort notice (PR-v2-11), shown even under ``--quiet``.

    Rewritten in PR-h-field-06 to answer the four questions the operator actually has, the
    same shape :func:`_usage_limit_stop_line` uses: what was spent against what ceiling, that
    the completed work survived, and the exact command to continue. It previously pointed at
    a file for the tally, which is one indirection too many for a terminal condition — and it
    matters more now that a ceiling exists BY DEFAULT, so the first time most operators meet
    this message they will not have chosen the number it names.

    Tokens are translated to their API-equivalent dollar figure, flagged as a VALUATION: the
    numbers come from the enforcement tally carried on the result, so this line, the
    loop-summary Budget section and ``--metrics`` cannot disagree.
    """
    tokens = sum(u.total_tokens for u in run_result.budget_usages)
    # Use billing.from_usages() — the SAME rule as loop-summary.md and --metrics — so that
    # the lower-bound warning fires whenever cost_incomplete_tokens > 0 (which includes any
    # Usage with cost_source="unknown", even when cost_usd is non-None).  Checking
    # `cost_usd is not None` alone misses that case and contradicts the artifact.
    b = _billing_from_usages(run_result.budget_usages)
    run_id = run_result.artifacts.run_dir.name

    if run_result.budget_ceiling == "budget_usd":
        limit = run_result.budget_usd
        crossed = f"cost ceiling (budget_usd = ${limit:,.2f})" if limit else "cost ceiling"
    else:
        tokens_limit = run_result.budget_tokens
        crossed = (
            f"token ceiling (budget_tokens = {tokens_limit:,})" if tokens_limit else "token ceiling"
        )

    if b.total_unpriced_tokens:
        valuation = (
            f"${b.api_equiv:,.2f} (LOWER BOUND — {b.total_unpriced_tokens:,} tokens unpriced)"
        )
    elif b.api_equiv > 0:
        valuation = f"${b.api_equiv:,.2f}"
    else:
        valuation = "unpriced"
    if run_result.budget_ceiling == "budget_usd":
        remedy = "Raise the ceiling (or set `[loop] budget_usd = 0` for none)"
    else:
        remedy = "Raise the ceiling (or set `[loop] budget_tokens = 0` for none)"
    return (
        f"[syncade] stopped early: this run crossed your {crossed}.\n"
        f"          Spent: {tokens:,} tokens (API-equivalent {valuation} — a valuation, not "
        f"billed money).\n"
        f"          Completed rounds and their findings are preserved; nothing was interrupted "
        f"mid-call.\n"
        f"          {remedy}, then: "
        f"syncade --resume {run_id}"
    )


def _approaching_budget_line(ceiling: str, usages: list, loop: object) -> str:
    """The 80%-of-ceiling heads-up. Advisory: it never changes a verdict or an exit code.

    Says what the remaining headroom IS rather than only that a limit is near, because the
    decision it supports is "let this run finish or stop it myself", and that needs a number.
    """
    if ceiling == "budget_usd":
        spent = sum(u.cost_usd for u in usages if u.cost_usd is not None)
        limit = loop.budget_usd
        return (
            f"[syncade] approaching your cost ceiling: ${spent:,.2f} of ${limit:,.2f} "
            f"(API-equivalent, a valuation not billed money). The run stops at the ceiling and "
            f"is resumable."
        )
    spent = sum(u.total_tokens for u in usages)
    limit = loop.budget_tokens
    return (
        f"[syncade] approaching your token ceiling: {spent:,} of {limit:,} tokens "
        f"({limit - spent:,} left). The run stops at the ceiling and is resumable; raise "
        f"`[loop] budget_tokens` or set it to 0 for none."
    )


def _quota_candidates(
    run_result: RunResult,
) -> Iterator[tuple[BaseException | None, str | None]]:
    """Every actor that could have been the one refused, with its provider.

    A quota is ACCOUNT-wide, so whichever actor reaches the provider first discovers it — and
    after the routing fix that is as often the judge or the producer as a reviewer. Scanning
    only the reviewers left both of those printing a bare "the provider", which is the one
    thing this line exists to avoid saying.
    """
    for r in run_result.dispatch_result.results:
        yield r.error, r.provider
    synth = run_result.synth_result
    if synth is not None:
        yield synth.error, synth.provider
    for rnd in reversed(run_result.rounds):
        producer = rnd.producer_result
        if producer is not None:
            yield producer.error, producer.provider


def _usage_limit_stop_line(run_result: RunResult) -> str:
    """The provider-quota abort notice (PR-h-field-02), shown even under ``--quiet``.

    Three field runs hit this and the operator saw NOTHING — no verdict, no error, no exit code.
    A terminal condition the operator can act on is worth four lines: what happened, that their
    completed work survived, the remedy, and the exact command to continue.

    Names the PROVIDER because a mixed panel makes "which one ran out" the first question, and
    names the run id because the resume command is useless without it.
    """
    provider = next(
        (p for err, p in _quota_candidates(run_result) if p and is_usage_limit_error(err)),
        "the provider",
    )
    run_id = run_result.artifacts.run_dir.name
    return (
        f"[syncade] stopped early: {provider}'s usage limit reached — not your configured "
        f"budget, "
        f"and not a fault in the code under review.\n"
        f"          Completed rounds and their findings are preserved; nothing was interrupted "
        f"mid-call.\n"
        f"          Remedy: consume a reset (run `codex`, then /usage) or wait for the window — "
        f"retrying now fails the same way.\n"
        f"          Resume with: syncade --resume {run_id}"
    )


def _is_usage_limit_stop(run_result: RunResult) -> bool:
    return getattr(run_result, "termination_reason", None) == "provider_usage_limit"


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

    def safety(self, message: str) -> None:
        """A disclosure about the DISPOSITION OF COMMITTED WORK — never suppressed (PR-h-13).

        `--quiet` is for progress, not for safety. The class this exists for is narrow and
        answerable in one sentence: *where did the producer's commits go, and is my tree
        consistent with that?* A branch that did or did not advance, a commit stranded on a
        detached HEAD, a non-descendant ending SHA, a failed `update-ref` — and above all the
        working tree left holding a fully STAGED REVERT of the producer's work, where the
        operator's next `git commit` silently undoes the run.

        That last one is not hypothetical: it happened twice in one session on 2026-08-10, with
        the warning visible and an experienced operator driving. Under `--quiet` there would
        have been nothing at all. The auth block and the default-branch disclosure already
        bypass quiet for the same reason; this gives that rule a name instead of a hand-rolled
        `print` at each site.

        Deliberately NOT this class: dirty-tree and untracked-file notes, resume-drift notes,
        auto-prune failures. They describe the run, not the fate of work already committed.
        """
        self._emit_err(f"warning: {message}")

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
        if budget_note is None and _is_usage_limit_stop(run_result):
            budget_note = _usage_limit_stop_line(run_result)

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
