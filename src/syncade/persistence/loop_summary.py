"""Loop-level loop-summary.md persistence.

Writes ``<run_dir>/loop-summary.md`` — the multi-round summary
artifact. Top-level alongside the per-round directories. Rolls up
every round + final verdict + the SHA series the loop's producer
commits produced.

the per-termination-reason next-steps + empty-series text tables and
the four small render helpers live in :mod:`.loop_summary_text`; this module
keeps the headline labels + ``persist_loop_summary``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ._atomic import atomic_write_text
from .checks import _LOOP_NEXT_STEPS_CHECK_FAILURE, _final_round_blocking_check_failure
from .loop_summary_text import (
    _LOOP_NEXT_STEPS,
    _budget_section,
    _empty_commit_series_note,
    _producer_commit_subject,
    _round_duration_seconds,
    _round_verdict_label,
    _run_usages,
)

_TERMINATION_REASON_LABELS: dict[str, str] = {
    "ship": "SHIP",
    "no_changes_to_review": "nothing to review",
    "producer_emptied_diff": "nothing to review (producer emptied all changes)",
    "findings_present": "findings present",
    "max_rounds_reached": "max rounds reached",
    "budget_exceeded": "budget exceeded",
    "producer_stalled": "producer stalled",
    "producer_subprocess_error": "producer subprocess error",
    "reviewer_failure": "reviewer failure",
    "synth_failure": "synthesizer failure",
    "test_subprocess_error": "test subprocess error",
    "check_subprocess_error": "blocking-check subprocess error",
    "decision_needed": "decision needed (producer escalation)",
    "blockers_all_deactivated": "decision needed (reviewers' blockers all deactivated)",
    "worktree_error": "worktree provisioning error",
    "diff_malformed": "diff filter refusal (unidentifiable headers)",
    "parse_failure": "output parse failure",
    "config_error": "config error",
}
"""Human-readable label for each :data:`syncade.orchestrator.TerminationReason`.
Used in loop-summary.md's headline so the operator sees a plain-
English description rather than the categorical machine-readable
slug."""


def persist_loop_summary(
    run_dir: Path,
    *,
    final_exit_code: int,
    final_round: int,
    termination_reason: str,
    rounds: list,  # list[RoundResult]; typed as list to avoid circular import
    max_rounds: int,
    repo_root: Path | None = None,
    started_at: datetime,
    completed_at: datetime,
    budget_tokens: int | None = None,
    budget_usd: float | None = None,
    budget_usages: list | None = None,
    budget_ceiling: str | None = None,
) -> Path:
    """Write ``<run_dir>/loop-summary.md`` — the multi-round
    summary.

    Top-level artifact alongside the per-round directories. Rolls
    up every round + final verdict + the SHA series the loop's
    producer commits produced. The operator reading
    ``loop-summary.md`` sees the whole loop in one document
    without traversing N per-round `summary.md` files.

    For ``max_rounds=1`` runs (single-pass back-compat), the
    summary still fires but renders a single round section + the
    trivial commit series ("no commits — round 0 shipped" or
    similar).

    Args:
        run_dir: Top-level run directory (``<repo>/.syncade/runs/<id>/``).
        final_exit_code: The loop's final exit code (see
            :mod:`syncade.exit_codes`).
        final_round: 0-indexed round that terminated the loop.
        termination_reason: Categorical label (see
            :data:`syncade.orchestrator.TerminationReason`).
        rounds: List of :class:`syncade.orchestrator.RoundResult`,
            one per round executed. ``len(rounds) == final_round + 1``.
        max_rounds: Configured ``[loop] max_rounds`` ceiling.
        started_at: Run start instant (captured once at the top of
            ``run_review``).
        completed_at: Run-end instant. The wall-clock duration is
            ``completed_at - started_at``.

    Returns:
        Path of the written ``loop-summary.md``.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    run_id = run_dir.name
    started = started_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    duration = completed_at - started_at
    total_seconds = int(duration.total_seconds())
    hh, rem = divmod(total_seconds, 3600)
    mm, ss = divmod(rem, 60)
    duration_str = f"{hh:02d}:{mm:02d}:{ss:02d}"

    # Final verdict label — mirrors the findings.md verdict
    # convention: SHIP for exit 0, NO-SHIP for exit 30 (real
    # findings), ABORT for environmental failures (40 / 60).
    # no_changes_to_review / producer_emptied_diff exit 0 but are NOT SHIPs — the
    # final round dispatched no reviewers (no approval was rendered this round).
    if final_exit_code == 0 and termination_reason in (
        "no_changes_to_review",
        "producer_emptied_diff",
    ):
        verdict_label = "NOTHING TO REVIEW"
    elif final_exit_code == 0:
        verdict_label = "SHIP"
    elif final_exit_code == 30:
        verdict_label = "NO-SHIP"
    elif final_exit_code == 20:
        verdict_label = "NO-SHIP"
    elif final_exit_code == 25:
        # Budget abort. Still NO-SHIP: the run did not converge, it was stopped
        # at a token/dollar budget ceiling with findings potentially remaining.
        verdict_label = "NO-SHIP"
    elif final_exit_code == 10:
        # producer escalation. Still NO-SHIP (escalation does not
        # override the mechanical verdict); the termination reason carries
        # the "decision needed" signal.
        verdict_label = "NO-SHIP"
    elif final_exit_code in (40, 60, 70):
        verdict_label = "ABORT"
    elif final_exit_code == 50:
        verdict_label = "ABORT"
    else:
        verdict_label = "UNKNOWN"

    reason_label = _TERMINATION_REASON_LABELS.get(termination_reason, termination_reason)
    if termination_reason == "ship":
        reason_label = f"ship (round {final_round})"
    elif termination_reason == "budget_exceeded" and budget_ceiling is not None:
        # Name WHICH ceiling in the headline too — a token-only abort must not read as a cost
        # stop (Finding: the generic "cost ceiling hit" contradicted the Budget section).
        which = "token" if budget_ceiling == "budget_tokens" else "cost"
        reason_label = f"budget exceeded ({which} ceiling)"

    lines: list[str] = [
        f"# Syncade run {run_id} — loop summary",
        "",
        f"**Final verdict:** {verdict_label}  ",
        f"**Termination reason:** {reason_label}  ",
        f"**Final exit code:** {final_exit_code}  ",
    ]
    # SHA annotation. The loop summary covers multiple rounds
    # so a single "Generated against SHA" line would be ambiguous —
    # the commit series below already lists every per-round snapshot.
    # The headline gets the round-0 starting SHA (the operator's pre-
    # loop tree state), which is the question a re-reader is most
    # likely to have. The empty-``rounds`` branch is defensive; in
    # practice the orchestrator only writes loop-summary.md after at
    # least one round ran.
    if rounds:
        first_round_sha = rounds[0].snapshot.commit_sha
        if first_round_sha:
            lines.append(
                f"**Round 0 starting SHA:** `{first_round_sha[:12]}` (full: `{first_round_sha}`)  "
            )
    lines.extend(
        [
            f"**Rounds executed:** {len(rounds)} of {max_rounds}  ",
            f"**Started:** {started}  ",
            f"**Total wall-clock:** {duration_str}",
            "",
        ]
    )

    # --- Per-round sections ---------------------------------------
    for r in rounds:
        round_verdict = _round_verdict_label(r)
        round_duration_s = _round_duration_seconds(r)
        lines.append(f"## Round {r.round_idx} — {round_verdict} ({round_duration_s:.1f}s)")
        lines.append("")
        # Reviewers — count consolidated findings or report failure
        if getattr(r, "no_changes_to_review", False):
            lines.append("- Reviewers: not dispatched (diff was empty before review)")
        elif r.fail_closed_headers:
            lines.append("- Reviewers: not dispatched (diff refused — unidentifiable headers)")
        elif r.dispatch_result is not None and r.dispatch_result.all_succeeded:
            n_reviewers = len(r.dispatch_result.successes)
            lines.append(f"- Reviewers: {n_reviewers} succeeded")
        elif r.dispatch_result is not None:
            n_failed = len(r.dispatch_result.failures)
            lines.append(f"- Reviewers: {n_failed} failed")
        else:
            lines.append("- Reviewers: did not run")
        # Synth
        if r.synth_result is None:
            lines.append("- Synthesizer: skipped")
        elif r.synth_result.output is not None:
            active = sum(1 for f in r.synth_result.output.consolidated_findings if not f.dismissed)
            blockers = sum(
                1
                for f in r.synth_result.output.consolidated_findings
                if not f.dismissed and f.severity == "blocker"
            )
            lines.append(f"- Synthesizer: {active} active finding(s), {blockers} blocker(s)")
        else:
            err = type(r.synth_result.error).__name__
            lines.append(f"- Synthesizer: failed ({err})")
        # Test re-run
        if r.test_result is None:
            lines.append(f"- Test re-run: skipped ({r.test_skip_reason or 'unknown'})")
        elif r.test_result.outcome == "subprocess_error":
            err = type(r.test_result.error).__name__ if r.test_result.error else "Unknown"
            lines.append(f"- Test re-run: subprocess_error ({err})")
        else:
            lines.append(f"- Test re-run: {r.test_result.outcome} (exit {r.test_result.exit_code})")
        # Producer
        if r.producer_result is None:
            lines.append("- Producer: did not run (loop terminated)")
        elif r.producer_result.outcome == "committed":
            short_sha = r.producer_result.ending_sha[:12]
            lines.append(f"- Producer: committed `{short_sha}`")
        elif r.producer_result.outcome == "stalled":
            lines.append("- Producer: stalled (no commit)")
        elif r.producer_result.outcome == "escalated":
            # an escalated producer always terminates the loop, so this
            # round is the terminating round and ``termination_reason`` carries
            # the coverage guard's disposition. "decision_needed" → the
            # escalation covered every active blocker and was honored (exit 10);
            # any other reason → it left a blocker uncovered and was rejected,
            # so the loop treated the round as a stall (exit 30). Render the
            # rejected case as such rather than as an honored decision checkpoint.
            if termination_reason == "decision_needed":
                lines.append("- Producer: escalated (operator decision needed)")
            else:
                lines.append(
                    "- Producer: escalated but not honored — left active "
                    "blocker(s) uncovered (treated as stall)"
                )
        else:
            err = type(r.producer_result.error).__name__ if r.producer_result.error else "Unknown"
            lines.append(f"- Producer: subprocess_error ({err})")
        # Per-round artifacts
        lines.append(f"- Per-round artifacts: [round-{r.round_idx}/](round-{r.round_idx}/)")
        lines.append("")

    # --- Commit series produced by this loop ----------------------
    lines.append("## Commit series produced by this loop")
    lines.append("")
    # one-line lead-in. The headline Round 0 SHA invites the
    # operator to look here for the rest of the SHAs; the lead-in
    # makes that link explicit instead of dumping the operator into a
    # bullet list with no framing.
    lines.append(
        "Each entry is one git commit on the operator's branch, in "
        "chronological order. The first entry is the pre-loop tree "
        "state; subsequent entries are producer commits."
    )
    lines.append("")
    if not rounds:
        lines.append("- (no rounds executed)")
    else:
        # First round's starting SHA = operator's pre-loop SHA
        first_round = rounds[0]
        starting_sha = first_round.snapshot.commit_sha
        lines.append(f"- `{starting_sha[:12]}` — round 0 starting SHA (operator's commit)")
        any_commits = False
        for r in rounds:
            if r.producer_result is None or r.producer_result.outcome != "committed":
                continue
            any_commits = True
            ending = r.producer_result.ending_sha[:12]
            # include the producer commit's
            # subject line so the operator can scan the series
            # without dropping to ``git log``. Looked up via
            # ``git log -1 --pretty=format:'%s' <ending_sha>`` —
            # wrapped in try/except so any git failure (missing
            # repo_root, weird ref state) degrades to the
            # subject-less form rather than crashing the summary
            # write.
            subject = _producer_commit_subject(repo_root, r.producer_result.ending_sha)
            if subject:
                lines.append(f'- `{ending}` — round {r.round_idx} producer ("{subject}")')
            else:
                lines.append(f"- `{ending}` — round {r.round_idx} producer")
        if not any_commits:
            # phrasing branches on termination
            # reason so the empty-series wording matches what
            # actually happened.
            lines.append(_empty_commit_series_note(termination_reason))
    lines.append("")

    # --- Budget (only on a budget abort) --------------------------
    if termination_reason == "budget_exceeded":
        # Prefer the loop's ENFORCEMENT tally (fresh on resume — excludes rehydrated original
        # rounds, so the reported number is the one that tripped). Fall back to summing all
        # rounds only when a caller (a direct-construction test) didn't thread it — equivalent
        # for a non-resumed run, where every round IS this-process spend.
        usages = budget_usages if budget_usages is not None else _run_usages(rounds)
        lines.extend(_budget_section(usages, budget_tokens, budget_usd, budget_ceiling))

    # --- Next steps -----------------------------------------------
    lines.append("## Next steps")
    lines.append("")
    # validation fix: a synth-clean BLOCKING-check NO-SHIP on the final round
    # gets check-pointing guidance — the termination_reason-keyed text (e.g.
    # max_rounds_reached's "read active blockers") is misleading when the
    # NO-SHIP came from the mechanical lane, not the synthesizer.
    if _final_round_blocking_check_failure(rounds):
        next_steps = _LOOP_NEXT_STEPS_CHECK_FAILURE
    else:
        next_steps = _LOOP_NEXT_STEPS.get(
            termination_reason,
            "- See `manifest.json` and the per-round directories for details.",
        )
    lines.append(next_steps)
    lines.append("")

    summary_path = run_dir / "loop-summary.md"
    atomic_write_text(summary_path, "\n".join(lines))
    return summary_path
