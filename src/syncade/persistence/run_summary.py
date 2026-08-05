"""Per-round summary.md persistence.

Writes ``<round_dir>/summary.md`` — the human-readable per-round
dashboard. The manifest (``manifest.json``) is for tooling; this file
is for the user. Every round produces one, regardless of exit code.

the per-exit-code "Next steps" content blocks + the two resolvers
live in :mod:`.run_summary_next_steps`; this module keeps the summary renderer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from syncade import billing
from syncade.dispatcher import DispatchResult
from syncade.producer import ProducerResult
from syncade.snapshot import Snapshot
from syncade.synthesis import has_active_blocker
from syncade.synthesizer import SYNTHESIZER_NAME, SynthesizerResult
from syncade.test_runner import TestRunResult

from ._atomic import atomic_write_text
from ._markdown import (
    _format_string_list_block,
    _format_summary_block,
    _md_command_lines,
    _reviewer_file_links,
)
from .checks import check_aware_next_steps as check_aware_next_steps
from .checks import render_checks_section
from .producer import PRODUCER_NAME
from .run_summary_next_steps import _resolve_next_steps, _resolve_next_steps_with_producer
from .test_run import TEST_RUN_NAME

# Exit-code labels for the human-readable run summary. Mirrors the
# constant names in :mod:`syncade.exit_codes`; kept local so persistence
# doesn't reach into that module's internals just for a display string.
_EXIT_CODE_LABELS: dict[int, str] = {
    0: "SUCCESS",
    10: "CLARIFICATION_NEEDED",
    20: "MAX_ROUNDS_REACHED",
    30: "FINDINGS_PRESENT",
    40: "REVIEWER_FAILURE",
    50: "CONFIG_ERROR",
    60: "WORKTREE_ERROR",
    70: "REVIEWER_OUTPUT_UNPARSEABLE",
}


_SKIP_REASON_MESSAGES: dict[str, str] = {
    "test_command_unset": (
        "skipped (`[loop] test_command` is not configured in "
        "`.syncade/config.toml`; the test re-run leg is opt-in)"
    ),
    "reviewer_failed": (
        "skipped (a reviewer failed; the test re-run leg runs only "
        "when every prior phase succeeded)"
    ),
    "synth_failed": (
        "skipped (the synthesizer failed; the test re-run leg runs "
        "only when every prior phase succeeded)"
    ),
    "synth_blocker": (
        "skipped (the synthesizer surfaced an active blocker; the "
        "test re-run leg is skipped on synth-blocker paths to avoid "
        "wasted compute when the verdict is already NO-SHIP)"
    ),
    "test_worktree_error": (
        "skipped (the test re-run leg's worktree could not be "
        "provisioned; reviewer + synthesizer artifacts above are "
        "still valid — the failure happened AFTER they completed)"
    ),
}
"""Human-readable messages for each test-skip reason.

Used by :func:`persist_run_summary`. Logger uses its own short strings; both
surfaces are driven by the same enum so they agree about which reason fired.
"""


def persist_run_summary(
    round_dir: Path,
    snapshot: Snapshot,
    dispatch_result: DispatchResult,
    exit_code: int,
    started_at: datetime,
    synth_result: SynthesizerResult | None = None,
    test_result: TestRunResult | None = None,
    test_skip_reason: str | None = None,
    *,
    producer_result: ProducerResult | None = None,
    producer_provider: str | None = None,
    producer_model: str | None = None,
    resumed_under_drift: bool = False,
    check_results: list[TestRunResult] | None = None,
    escalation_honored: bool = False,
    branch_already_advanced: bool = False,
    no_changes_to_review: bool = False,
    fail_closed_headers: list[str] | None = None,
) -> Path:
    """Write ``<round_dir>/summary.md`` — a human-readable run summary.

    The manifest (``manifest.json``) is for tooling; this file is for
    the user. Every run produces one, regardless of exit code.

    Layout::

        # Syncade run <run-id>

        **Started:** 2026-05-13 11:46:55 UTC
        **Exit code:** 0 (SUCCESS)
        **Repo:** <commit-sha> on <branch>

        ## Reviewers

        ### claude-reviewer (anthropic)
        - **Outcome:** success
        - **Duration:** 588.6s
        - **Verdict:** SHIP
        - **Findings:** 0
        - **Output:** [.parsed.json](claude-reviewer.parsed.json) | ...

        **Summary:** I verified the new MoneyMovement widget against
        mockup-v2 line-by-line, ran the full frontend test suite
        (279 tests pass), confirmed the SectorRotation deletion is
        complete...

        **Coverage gaps:** None.

        **Dismissed concerns:**

        - Considered: the SectorRotationData interface still in
          types/index.ts. The spec exempts types files explicitly.

        ### codex-reviewer (openai)
        - **Outcome:** failure
        - **Duration:** 600.1s
        - **Error:** SubprocessTimeoutError
        - **Output:** [.stdout](codex-reviewer.stdout) | ...

        ## Next steps

        - <exit-code-specific guidance>

    Per-reviewer ``Output`` links point only at files that exist for
    that reviewer's outcome (success → ``.parsed.json`` / ``.stdout`` /
    ``.stderr``; failure → ``.stdout`` / ``.stderr`` / ``.error.txt``),
    so the rendered markdown never carries a dangling link.

    success entries also render the structured-narrative blocks
    the reviewer populated on :class:`ReviewerOutput`: ``Summary``,
    ``Coverage gaps`` (rendered as ``None.`` when empty), and
    ``Dismissed concerns`` (same empty treatment). Failure entries omit these
    blocks because the reviewer never produced a structured output to render.

    Args:
        round_dir: The round directory to write into. Must already
            exist. The run-id in the heading is derived from
            ``round_dir.parent.name`` — the same convention :func:`persist_round_manifest` uses.
        snapshot: The run's repo snapshot — supplies the commit SHA and
            branch for the **Repo** line.
        dispatch_result: The dispatch result whose per-reviewer entries
            drive the **Reviewers** section.
        exit_code: The run's exit code — drives the **Exit code** line
            and the **Next steps** guidance.
        started_at: The run-start instant, captured once by the
            orchestrator and shared with :func:`persist_round_manifest`
            so both files agree on when the run began — the
            **Started:** line shows this, not the file's write time.

    Returns:
        The path of the written ``summary.md``.

    Raises:
        FileNotFoundError: If ``round_dir`` does not exist (caller bug —
            the orchestrator is responsible for creating it).
    """
    if not round_dir.is_dir():
        raise FileNotFoundError(f"round_dir does not exist: {round_dir}")

    run_id = round_dir.parent.name
    started = started_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    if no_changes_to_review:
        exit_label = "NO_CHANGES_TO_REVIEW"
    elif fail_closed_headers is not None:
        exit_label = "DIFF_MALFORMED"
    else:
        exit_label = _EXIT_CODE_LABELS.get(exit_code, "UNKNOWN")
    branch = snapshot.branch or "(detached HEAD)"

    lines = [
        f"# Syncade run {run_id}",
        "",
        f"**Started:** {started}  ",
        f"**Exit code:** {exit_code} ({exit_label})  ",
        f"**Repo:** {snapshot.commit_sha} on {branch}",
        "",
    ]

    if resumed_under_drift:
        lines += [
            "**Resumed under tree drift:** `--force-drift` was passed; "
            "this round snapshotted from current HEAD, not from the "
            "run's original expected SHA. Cross-round context from "
            "prior rounds references findings against the original tree state.",
            "",
        ]

    lines += [
        "## Reviewers",
        "",
    ]

    for r in dispatch_result.results:
        lines.append(f"### {r.reviewer_name} ({r.provider})")
        if r.output is not None:
            lines.append("- **Outcome:** success")
            lines.append(f"- **Duration:** {r.duration_seconds:.1f}s")
            lines.append(f"- **Verdict:** {r.output.verdict}")
            lines.append(f"- **Findings:** {len(r.output.findings)}")
            lines.append(f"- **Output:** {_reviewer_file_links(r)}")
            # structured-narrative blocks. Always rendered for
            # success entries — every successful reviewer is required
            # by the schema to populate them, so the summary mirrors
            # what's in `.parsed.json` in human-readable form.
            lines.append("")
            lines.extend(_format_summary_block(r.output.summary))
            lines.append("")
            lines.extend(_format_string_list_block("Coverage gaps", r.output.coverage_gaps))
            lines.append("")
            lines.extend(
                _format_string_list_block("Dismissed concerns", r.output.dismissed_concerns)
            )
        else:
            err_cls = type(r.error).__name__ if r.error is not None else "Unknown"
            lines.append("- **Outcome:** failure")
            lines.append(f"- **Duration:** {r.duration_seconds:.1f}s")
            lines.append(f"- **Error:** {err_cls}")
            lines.append(f"- **Output:** {_reviewer_file_links(r)}")
            # No Summary / Coverage gaps / Dismissed concerns blocks
            # for failure entries — the reviewer never produced a
            # structured ReviewerOutput for us to render. The error
            # class + .error.txt + .stdout files (linked above) carry
            # the actionable detail.
        lines.append("")

    # --- Synthesizer subsection -----------------------------
    lines.append("## Synthesizer")
    lines.append("")
    if synth_result is None:
        if no_changes_to_review:
            lines.append(
                "- **Outcome:** not applicable (no reviewers were dispatched — nothing to review)"
            )
        elif fail_closed_headers is not None:
            lines.append(
                "- **Outcome:** not applicable (no reviewers were dispatched — diff refused)"
            )
        else:
            lines.append(
                "- **Outcome:** skipped (a reviewer failed; cold "
                "synthesis runs only when every reviewer succeeded)"
            )
    elif synth_result.output is not None:
        output = synth_result.output
        consolidated = output.consolidated_findings
        dismissed = sum(1 for f in consolidated if f.dismissed)
        active_by_sev = {"blocker": 0, "minor": 0, "nit": 0}
        for f in consolidated:
            if not f.dismissed:
                active_by_sev[f.severity] += 1
        lines.append("- **Outcome:** success")
        lines.append(f"- **Duration:** {synth_result.duration_seconds:.1f}s")
        lines.append(
            f"- **Consolidated findings:** {len(consolidated)} "
            f"({dismissed} dismissed, {active_by_sev['blocker']} active "
            f"blocker(s), {active_by_sev['minor']} active minor, "
            f"{active_by_sev['nit']} active nit)"
        )
        lines.append(
            f"- **Output:** [findings.md](findings.md) | "
            f"[.parsed.json]({SYNTHESIZER_NAME}.parsed.json) | "
            f"[.stdout]({SYNTHESIZER_NAME}.stdout) | "
            f"[.stderr]({SYNTHESIZER_NAME}.stderr)"
        )
        lines.append("")
        lines.extend(_format_summary_block(output.synthesis_summary))
    else:
        err_cls = type(synth_result.error).__name__ if synth_result.error is not None else "Unknown"
        lines.append("- **Outcome:** failure")
        lines.append(f"- **Duration:** {synth_result.duration_seconds:.1f}s")
        lines.append(f"- **Error:** {err_cls}")
        # only link `.error.txt` when persistence will
        # actually write it — i.e. when ``synth_result.error is not
        # None``. ``SynthesizerResult`` now enforces exactly one of
        # output/error at construction; this conditional stays as
        # defense in depth and mirrors the manifest side.
        output_links = [
            f"[.stdout]({SYNTHESIZER_NAME}.stdout)",
            f"[.stderr]({SYNTHESIZER_NAME}.stderr)",
        ]
        if synth_result.error is not None:
            output_links.append(f"[.error.txt]({SYNTHESIZER_NAME}.error.txt)")
        lines.append("- **Output:** " + " | ".join(output_links))
    lines.append("")

    # --- Test Suite subsection ---------------------------
    # Between the Synthesizer subsection and Next steps. Always
    # rendered — even when the leg was skipped, the operator wants
    # to see WHY it was skipped (config opt-out vs. prior-phase
    # failure).
    lines.append("## Test Suite")
    lines.append("")
    if test_result is None:
        # Render the explicit skip reason passed by ``run_review``. This always
        # agrees with the Logger's live-log skip message because both are driven
        # by the same TestSkipReason value. Falls back to inference for callers
        # that don't pass the argument.
        if no_changes_to_review:
            lines.append(
                "- **Outcome:** not applicable (no reviewers were dispatched — nothing to review)"
            )
        elif fail_closed_headers is not None:
            lines.append(
                "- **Outcome:** not applicable (no reviewers were dispatched — diff refused)"
            )
        elif test_skip_reason is not None and test_skip_reason in _SKIP_REASON_MESSAGES:
            lines.append(f"- **Outcome:** {_SKIP_REASON_MESSAGES[test_skip_reason]}")
        else:
            # Fallback inference path for callers that don't pass test_skip_reason.
            if not dispatch_result.all_succeeded:
                lines.append(
                    "- **Outcome:** skipped (a reviewer failed; the test "
                    "re-run leg runs only when every prior phase succeeded)"
                )
            elif synth_result is not None and synth_result.error is not None:
                lines.append(
                    "- **Outcome:** skipped (the synthesizer failed; "
                    "the test re-run leg runs only when every prior "
                    "phase succeeded)"
                )
            elif (
                synth_result is not None
                and synth_result.output is not None
                and has_active_blocker(synth_result.output)
            ):
                lines.append(
                    "- **Outcome:** skipped (the synthesizer surfaced an "
                    "active blocker; the test re-run leg is skipped on "
                    "synth-blocker paths to avoid wasted compute when "
                    "the verdict is already NO-SHIP)"
                )
            else:
                lines.append(
                    "- **Outcome:** skipped (`[loop] test_command` is not "
                    "configured in `.syncade/config.toml`; the test "
                    "re-run leg is opt-in)"
                )
    elif test_result.outcome == "subprocess_error":
        err_cls = type(test_result.error).__name__ if test_result.error is not None else "Unknown"
        lines.append("- **Outcome:** subprocess_error")
        lines.append(f"- **Duration:** {test_result.duration_seconds:.1f}s")
        lines.extend(_md_command_lines(test_result.command, prefix="- "))
        lines.append(f"- **Error:** {err_cls}")
        lines.append(
            f"- **Output:** [.stdout]({TEST_RUN_NAME}.stdout) | "
            f"[.stderr]({TEST_RUN_NAME}.stderr) | "
            f"[exit-code.txt]({TEST_RUN_NAME}.exit-code.txt)"
        )
    else:
        # outcome == "passed" or "failed". Both render the same
        # block with outcome + exit_code + command + duration +
        # artifact links.
        lines.append(f"- **Outcome:** {test_result.outcome}")
        lines.append(f"- **Exit code:** {test_result.exit_code}")
        lines.extend(_md_command_lines(test_result.command, prefix="- "))
        lines.append(f"- **Duration:** {test_result.duration_seconds:.1f}s")
        lines.append(
            f"- **Output:** [.stdout]({TEST_RUN_NAME}.stdout) | "
            f"[.stderr]({TEST_RUN_NAME}.stderr) | "
            f"[exit-code.txt]({TEST_RUN_NAME}.exit-code.txt)"
        )
    lines.append("")

    # mechanical-checks subsection ([] for no checks → byte-identical).
    lines.extend(render_checks_section(check_results or []))

    # --- Producer subsection ----------------
    # Rendered only when a producer ran on this round (NO-SHIP
    # round in a multi-round loop). On the SHIP round + on
    # single-pass (max_rounds=1) runs, the producer never runs
    # and this section is omitted.
    if producer_result is not None:
        lines.append("## Producer")
        lines.append("")
        if producer_result.outcome == "committed":
            ending = producer_result.ending_sha[:12]
            lines.append("- **Outcome:** committed")
            lines.append(f"- **Provider / model:** {producer_provider} / {producer_model}")
            lines.append(f"- **Duration:** {producer_result.duration_seconds:.1f}s")
            lines.append(f"- **Commit SHA:** `{producer_result.ending_sha}` (short: `{ending}`)")
            lines.append(
                f"- **Output:** [{PRODUCER_NAME}.stdout]({PRODUCER_NAME}.stdout) | "
                f"[{PRODUCER_NAME}.stderr]({PRODUCER_NAME}.stderr) | "
                f"[{PRODUCER_NAME}.commit.txt]({PRODUCER_NAME}.commit.txt)"
            )
        elif producer_result.outcome == "stalled":
            lines.append("- **Outcome:** stalled (no commit)")
            lines.append(f"- **Provider / model:** {producer_provider} / {producer_model}")
            lines.append(f"- **Duration:** {producer_result.duration_seconds:.1f}s")
            lines.append(
                f"- **Output:** [{PRODUCER_NAME}.stdout]({PRODUCER_NAME}.stdout) | "
                f"[{PRODUCER_NAME}.stderr]({PRODUCER_NAME}.stderr) | "
                f"[{PRODUCER_NAME}.commit.txt]({PRODUCER_NAME}.commit.txt)"
            )
        elif producer_result.outcome == "escalated" and escalation_honored:
            # the producer escalated a finding as an operator decision
            # (no commit) AND the coverage guard HONORED it — its
            # finding_indices cover every active blocker, so the loop
            # checkpointed (exit 10) and wrote decision-needed.md at the run
            # root. The per-round summary names the decision and links it.
            lines.append("- **Outcome:** escalated (operator decision needed)")
            lines.append(f"- **Provider / model:** {producer_provider} / {producer_model}")
            lines.append(f"- **Duration:** {producer_result.duration_seconds:.1f}s")
            if producer_result.escalation is not None:
                lines.append(f"- **Decision needed:** {producer_result.escalation.decision}")
            lines.append(
                f"- **Output:** [{PRODUCER_NAME}.stdout]({PRODUCER_NAME}.stdout) | "
                f"[{PRODUCER_NAME}.stderr]({PRODUCER_NAME}.stderr) | "
                "[decision-needed.md](../decision-needed.md)"
            )
        elif producer_result.outcome == "escalated":
            # the producer escalated, but the coverage guard REJECTED it
            # — the escalation left at least one active blocker uncovered (or
            # referenced a non-blocker / out-of-range index), so the loop treated
            # the round as a stall (exit 30, NO branch advance, NO
            # decision-needed.md). Render the TRUE disposition: do NOT link a
            # decision-needed.md that was never written, and do NOT tell the
            # operator a decision checkpoint is pending.
            lines.append(
                "- **Outcome:** escalated but not honored (left active "
                "blocker(s) uncovered — treated as a stall)"
            )
            lines.append(f"- **Provider / model:** {producer_provider} / {producer_model}")
            lines.append(f"- **Duration:** {producer_result.duration_seconds:.1f}s")
            if producer_result.escalation is not None:
                lines.append(f"- **Attempted escalation:** {producer_result.escalation.decision}")
            lines.append(
                f"- **Output:** [{PRODUCER_NAME}.stdout]({PRODUCER_NAME}.stdout) | "
                f"[{PRODUCER_NAME}.stderr]({PRODUCER_NAME}.stderr)"
            )
        else:  # subprocess_error
            err_cls = type(producer_result.error).__name__ if producer_result.error else "Unknown"
            lines.append("- **Outcome:** subprocess_error")
            lines.append(f"- **Provider / model:** {producer_provider} / {producer_model}")
            lines.append(f"- **Duration:** {producer_result.duration_seconds:.1f}s")
            lines.append(f"- **Error:** {err_cls}")
            if producer_result.ending_sha != producer_result.starting_sha:
                lines.append(
                    "- **Indeterminate producer commit:** HEAD moved from "
                    f"{producer_result.starting_sha[:12]} to "
                    f"{producer_result.ending_sha[:12]} before the subprocess failed; "
                    "the branch was not advanced."
                )
            lines.append(
                f"- **Output:** [{PRODUCER_NAME}.stdout]({PRODUCER_NAME}.stdout) | "
                f"[{PRODUCER_NAME}.stderr]({PRODUCER_NAME}.stderr) | "
                f"[{PRODUCER_NAME}.error.txt]({PRODUCER_NAME}.error.txt)"
            )
        lines.append("")

    lines.extend(_cost_section(dispatch_result, synth_result, producer_result))

    lines.append("## Next steps")
    lines.append("")
    # Exits 30, 40, and 70 split by phase; _resolve_next_steps routes to the
    # right variant. When a producer ran on this round, next-step guidance
    # should acknowledge that producer attempt.
    if producer_result is not None:
        lines.append(
            _resolve_next_steps_with_producer(
                exit_code, producer_result, escalation_honored, branch_already_advanced
            )
        )
    else:
        lines.append(
            _resolve_next_steps(
                exit_code,
                synth_result,
                test_result,
                test_skip_reason,
                check_results,
                no_changes_to_review=no_changes_to_review,
                fail_closed_headers=fail_closed_headers,
            )
        )
    lines.append("")

    summary_path = round_dir / "summary.md"
    atomic_write_text(summary_path, "\n".join(lines))
    return summary_path


def _cost_section(dispatch_result, synth_result, producer_result) -> list[str]:
    """A single per-run '## Token usage & cost' section (PR-v2-04): per-actor tokens
    + cost with a run total, in one place rather than scattered per-actor lines. Rendered
    only when at least one actor reported usage.

    **The dollars here are an API-EQUIVALENT VALUATION, not spend** (PR-v2-24). Fixing
    ``--metrics`` and leaving this artifact printing "Total: $0.1426" for a run that cost
    the user nothing would have re-introduced the exact bug one surface over -- which is
    what the panel caught. Both surfaces now read the same ``auth_mode``.
    """
    rows: list[tuple[str, object]] = []
    for r in dispatch_result.results:
        if r.usage is not None:
            rows.append((f"{r.reviewer_name} ({r.provider})", r.usage))
    if synth_result is not None and synth_result.usage is not None:
        rows.append((SYNTHESIZER_NAME, synth_result.usage))
    if producer_result is not None and producer_result.usage is not None:
        rows.append(("producer", producer_result.usage))
    if not rows:
        return []
    out = ["## Token usage & cost", ""]
    total_tok = 0
    any_estimated = False
    unpriced = 0
    for label, u in rows:
        cost = f"${u.cost_usd:.4f}" if u.cost_usd is not None else "unknown"
        reasoning = f", {u.reasoning_output_tokens} reasoning" if u.reasoning_output_tokens else ""
        out.append(
            f"- **{label}:** {u.total_tokens} tok{reasoning} · {cost} "
            f"({u.cost_source}, auth={u.auth_mode})"
        )
        total_tok += u.total_tokens
        if u.cost_usd is None:
            unpriced += 1
        any_estimated = any_estimated or u.cost_source == "estimated"

    notes = []
    if any_estimated:
        notes.append("includes estimates")
    if unpriced:
        # Unpriced usage is NOT free — it has tokens but no price (dogfood #4).
        notes.append(f"{unpriced} unpriced, not free")
    suffix = f" ({'; '.join(notes)})" if notes else ""
    out.append(f"- **tokens:** {total_tok}{suffix}")

    # THE SAME classifier and THE SAME words as `--metrics`. This file and metrics_mode
    # kept diverging -- three separate rounds of the panel caught summary.md still telling
    # the lie --metrics had just stopped telling -- because each computed billing itself.
    # Now neither does.
    out.extend(
        billing.render(billing.from_usages([u for _label, u in rows]), indent="", bullet=True)
    )
    out.append("")
    return out
