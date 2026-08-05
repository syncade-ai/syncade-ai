"""findings.md persistence.

Renders the operator-facing consolidated review report. Two entry points:

- :func:`persist_findings_md` writes ``<round_dir>/findings.md`` once
  per round, only when the synthesizer succeeded.
- :func:`persist_current_findings_md` copies the latest round's
  ``findings.md`` to ``<run_dir>/findings.md`` so the skill / future
  tooling can address the active report without knowing the round
  number.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from syncade.dispatcher import DispatchResult
from syncade.synthesizer import SynthesizerResult
from syncade.test_runner import TestRunResult

from ._atomic import atomic_write_text
from ._clusters import render_cluster_section
from ._findings_verdict import _compute_findings_md_verdict
from ._markdown import (
    _consensus_lines,
    _file_or_repo_wide,
    _format_summary_block,
    _md_command_lines,
)
from .checks import render_checks_section
from .test_run import TEST_RUN_NAME


def persist_findings_md(
    round_dir: Path,
    synth_result: SynthesizerResult,
    started_at: datetime,
    test_result: TestRunResult | None = None,
    dispatch_result: DispatchResult | None = None,
    test_skip_reason: str | None = None,
    snapshot_sha: str | None = None,
    check_results: list[TestRunResult] | None = None,
) -> Path:
    """Write ``<round_dir>/findings.md`` — the operator-facing
    consolidated review report.

    Called only when the synthesizer succeeded
    (``synth_result.output is not None``). When the synthesizer
    failed, there are no consolidated findings to render and the
    operator's path forward is to inspect ``synthesizer.stdout`` /
    ``synthesizer.error.txt`` — both linked from ``summary.md``'s
    Next-steps block.

    Layout::

        # Findings — Syncade run <run-id>

        **Verdict:** SHIP|NO-SHIP (mechanical, from
        consolidated_findings)
        **Started:** YYYY-MM-DD HH:MM:SS UTC

        ## Test Suite (only when the test leg ran)

        outcome / exit_code / command / duration + pointer to
        test-run.stdout

        ## Synthesis summary

        <synth_result.output.synthesis_summary>

        ## Findings

        ### [<severity>] <description first line>

        **File:** `path` (or "repo-wide")
        **Status:** Active (or "Dismissed by synthesizer")
        **Flagged by:** <name1> (<sev1>), <name2> (<sev2>)
        **Synthesizer severity:** <severity>
        **Severity change rationale:** ... (if present)

        <description body, if multi-line>

        **Original per-reviewer descriptions:**
        - claude-reviewer: "..."
        - codex-reviewer: "..."

        **Dismissal rationale:** ... (if dismissed)

        ## Per-reviewer summaries

        ### <reviewer name> (<provider>)

        <ReviewerOutput.summary, rendered with _format_summary_block>

    Args:
        round_dir: The round directory to write into. Must already
            exist.
        synth_result: The :class:`SynthesizerResult`. Must have
            ``output is not None``; otherwise this function refuses
            with ``ValueError`` (defensive — the orchestrator
            shouldn't call us on the failure path).
        The Verdict label is derived from
            :func:`syncade.synthesis.has_active_blocker` against the synth's
            consolidated findings. The mechanical exit code is persisted in
            ``manifest.json`` and ``summary.md``; findings.md only needs its
            own local verdict label.
        started_at: The run-start instant captured by the
            orchestrator. Same value the manifest and summary use.
        test_result: The :class:`TestRunResult` from the opt-in
            test re-run leg, or ``None`` when the leg was
            skipped. When present, a ``## Test Suite`` section is
            prepended after the header so it's the first content
            the operator sees — test failures are typically more
            actionable than synth findings on a clean-synth run.
        dispatch_result: The :class:`DispatchResult` from the
            reviewer dispatch. When supplied, a
            ``## Per-reviewer summaries`` section is appended at
            the END of the document, rendering each successful
            reviewer's ``ReviewerOutput.summary`` field. This makes
            findings.md self-sufficient in both ship-clean and findings-present
            cases. ``None`` (the default) keeps the summaries section absent.
        snapshot_sha: The snapshot SHA of THIS round (what
            the reviewers had as HEAD when they produced the
            findings rendered below). When supplied, a
            ``**Generated against SHA:**`` header line is written
            immediately after the verdict line. ``None`` (the
            default) omits the line.

    Returns:
        The path of the written ``findings.md``.

    Raises:
        ValueError: If ``synth_result.output is None`` — defensive
            guard against being called on a failure path.
        FileNotFoundError: If ``round_dir`` does not exist.
    """
    if synth_result.output is None:
        raise ValueError(
            "persist_findings_md called with synth_result.output=None — "
            "there are no consolidated findings to render. The "
            "orchestrator must only call this on the synth-success path."
        )
    if not round_dir.is_dir():
        raise FileNotFoundError(f"round_dir does not exist: {round_dir}")

    run_id = round_dir.parent.name
    started = started_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    output = synth_result.output
    # The verdict line must reflect the overall mechanical result, not just
    # the synth's view of consolidated_findings. A clean synth with failed
    # tests still renders NO-SHIP, matching the orchestrator's exit code.
    #
    # The matrix below mirrors _compute_exit_code's test-leg AND
    # blocking-check branches exactly so a future refinement to "what
    # counts as blocking" lands in one place by changing both:
    # - a mechanical gate (test leg OR a blocking check) subprocess_error
    #   → "ABORT" (the harness couldn't run — exit 40); outranks even a
    #   synth blocker, since checks run on synth-blocker rounds too
    # - synth blocker (no gate errored) → NO-SHIP (synth said no)
    # - synth clean + a mechanical gate failed → NO-SHIP (the gate said no)
    # - synth clean + all gates passed → SHIP
    # - synth clean + test skipped (test_command unset, etc.) →
    #   SHIP
    # only BLOCKING checks reach the verdict; advisory results
    # are filtered out here so they are structurally unable to gate the
    # headline, exactly as they cannot reach _compute_exit_code.
    blocking_check_results = [c for c in (check_results or []) if c.severity == "blocking"]
    verdict_label, verdict_qualifier = _compute_findings_md_verdict(
        output, test_result, test_skip_reason, blocking_check_results, dispatch_result
    )
    lines: list[str] = [
        f"# Findings — Syncade run {run_id}",
        "",
        f"**Verdict:** {verdict_label} ({verdict_qualifier})  ",
    ]
    # SHA annotation. The orchestrator passes the snapshot SHA of this round;
    # callers that leave ``snapshot_sha`` as ``None`` omit the header line.
    if snapshot_sha:
        lines.append(f"**Generated against SHA:** `{snapshot_sha[:12]}` (full: `{snapshot_sha}`)  ")
    lines.extend(
        [
            f"**Started:** {started}",
            "",
        ]
    )

    # --- Test Suite section -----------------------------
    # Rendered when the test leg ran, BEFORE the synthesis summary
    # so it's the first thing the operator sees. Test failures on
    # a clean-synth run are typically the most actionable signal
    # (the synth said nothing; the tests said something).
    if test_result is not None:
        lines.extend(_format_findings_test_suite_block(test_result))

    # Mechanical-checks section (advisory failures tagged non-blocking).
    # render_checks_section returns [] for an empty/None list.
    lines.extend(render_checks_section(check_results or []))

    lines.extend(
        [
            "## Synthesis summary",
            "",
            output.synthesis_summary,
            "",
        ]
    )

    # Root-cause clusters, rendered above the individual findings so the
    # producer sees "these N are one issue" before reading them individually.
    lines.extend(render_cluster_section(output))

    lines.extend(
        [
            "## Findings",
            "",
        ]
    )

    if not output.consolidated_findings:
        lines.append("No consolidated findings — both reviewers verified the spec cleanly.")
        lines.append("")
    else:
        for finding in output.consolidated_findings:
            # Section header: severity tag + description first line, so
            # the operator scanning the document sees severity before
            # narrative.
            description_first_line = finding.description.strip().splitlines()[0]
            lines.append(f"### [{finding.severity}] {description_first_line}")
            lines.append("")
            lines.append(f"**File:** {_file_or_repo_wide(finding.file)}  ")
            status = "Dismissed by synthesizer" if finding.dismissed else "Active"
            lines.append(f"**Status:** {status}  ")
            flagged_by = ", ".join(
                f"{p.reviewer_name} ({p.original_severity})" for p in finding.provenance
            )
            lines.append(f"**Flagged by:** {flagged_by}  ")
            # advisory per-finding reviewer consensus — render-derived,
            # never reaches _compute_exit_code; omitted when dispatch_result is None.
            lines.extend(_consensus_lines(finding, dispatch_result))
            lines.append(f"**Synthesizer severity:** {finding.severity}")
            if finding.severity_change_rationale:
                lines.append("")
                lines.append(f"**Severity change rationale:** {finding.severity_change_rationale}")

            # If the description spans multiple lines, render the
            # remainder as a body block.
            description_rest = "\n".join(finding.description.strip().splitlines()[1:]).strip()
            if description_rest:
                lines.append("")
                lines.append(description_rest)

            # Original per-reviewer descriptions — always rendered even
            # when single-reviewer, so the operator sees the verbatim
            # source.
            lines.append("")
            lines.append("**Original per-reviewer descriptions:**")
            lines.append("")
            for p in finding.provenance:
                # Quote the description so newlines inside don't break
                # the bullet structure visually.
                quoted = p.original_description.strip().replace("\n", " ")
                lines.append(f"- {p.reviewer_name}: {quoted!r}")

            if finding.dismissed and finding.dismissal_rationale:
                lines.append("")
                lines.append(f"**Dismissal rationale:** {finding.dismissal_rationale}")
            lines.append("")

    # --- Per-reviewer summaries section -----------------
    # Appended AFTER the Findings section so the action items
    # (consolidated findings) stay above the fold. The per-
    # reviewer summaries are context — what each reviewer saw in
    # prose — useful for understanding the synthesizer's
    # consolidation choices but not the operator's first action
    # target.
    #
    # Per-reviewer summaries make findings.md self-sufficient even when there
    # are no consolidated findings.
    #
    # Only successful reviewers contribute (a failed reviewer
    # never produced a structured ReviewerOutput.summary to
    # render). If no reviewer succeeded, the section is omitted —
    # but in that case persist_findings_md wouldn't have been
    # called at all (the synthesizer is skipped on any reviewer
    # failure → no synth output → no findings.md).
    if dispatch_result is not None:
        successful = [r for r in dispatch_result.results if r.output is not None]
        if successful:
            lines.append("## Per-reviewer summaries")
            lines.append("")
            for r in successful:
                lines.append(f"### {r.reviewer_name} ({r.provider})")
                lines.append("")
                # Reuse the same _format_summary_block helper
                # summary.md uses. One source of truth
                # for the rendering rule.
                lines.extend(_format_summary_block(r.output.summary))
                lines.append("")

    findings_path = round_dir / "findings.md"
    atomic_write_text(findings_path, "\n".join(lines))
    return findings_path


def _format_findings_test_suite_block(test_result: TestRunResult) -> list[str]:
    """Render the ``## Test Suite`` section for findings.md as a
    list of lines.

    rendered at the TOP of findings.md (between the header
    and the Synthesis summary) so it's the first thing the
    operator sees when the test leg fired. Detailed test output
    stays in ``test-run.stdout``; findings.md just summarizes the
    outcome and points at the artifact.

    Three states: passed / failed / subprocess_error. Skipped
    legs do NOT call this — findings.md only fires on the synth-
    success path, and on that path the test leg either ran or
    was skipped via config opt-out (which findings.md doesn't
    mention; the operator already knows their config).
    """
    block: list[str] = ["## Test Suite", ""]
    # Link all three test-run artifacts on every outcome (passed / failed /
    # subprocess_error), matching what summary.md does. Consistent linking
    # keeps findings.md self-sufficient for any outcome.
    output_links = (
        f"**Output:** [test-run.stdout]({TEST_RUN_NAME}.stdout) | "
        f"[test-run.stderr]({TEST_RUN_NAME}.stderr) | "
        f"[test-run.exit-code.txt]({TEST_RUN_NAME}.exit-code.txt)"
    )
    if test_result.outcome == "subprocess_error":
        err_cls = type(test_result.error).__name__ if test_result.error is not None else "Unknown"
        block.append(f"**Outcome:** subprocess_error ({err_cls})  ")
        block.extend(_md_command_lines(test_result.command, inline_suffix="  "))
        block.append(f"**Duration:** {test_result.duration_seconds:.1f}s  ")
        block.append(output_links)
    else:
        # passed or failed — both render the same block shape.
        block.append(f"**Outcome:** {test_result.outcome} (exit {test_result.exit_code})  ")
        block.extend(_md_command_lines(test_result.command, inline_suffix="  "))
        block.append(f"**Duration:** {test_result.duration_seconds:.1f}s  ")
        block.append(output_links)
    block.append("")
    return block


def persist_current_findings_md(
    run_dir: Path,
    latest_round_findings_md: Path | None,
) -> Path | None:
    """Write/refresh ``<run_dir>/findings.md`` as a
    copy of the latest round's per-round ``findings.md``.

    The PRD calls out this artifact explicitly under "Run-dir
    layout":

        <run-id>/findings.md (run-root "current findings"
        symlink-or-copy that always points at the latest round's
        findings.md, so the skill / future tooling can address
        the active report without knowing the round number)

    Implemented as a file copy (not a symlink) for two reasons:

    1. Cross-platform: Windows symlinks require elevated
       privileges and behave differently than POSIX. A copy works
       everywhere.
    2. Operator inspecting ``<run_dir>/findings.md`` mid-loop
       sees the latest round's content directly — no
       broken-symlink moment if a round in progress hasn't
       written its findings.md yet.

    Returns the written path on success; ``None`` if the latest
    round had no findings.md (synth failed or was skipped). Best-
    effort: any I/O failure surfaces as None and a swallowed
    error — the per-round artifacts are already on disk; the
    convenience copy is non-essential.
    """
    if latest_round_findings_md is None or not latest_round_findings_md.is_file():
        return None
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")
    target = run_dir / "findings.md"
    try:
        shutil.copy2(latest_round_findings_md, target)
    except OSError:
        return None
    return target
