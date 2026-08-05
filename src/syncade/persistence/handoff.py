"""Loop-level handoff.md persistence.

Writes ``<run_dir>/handoff.md`` — the structured operator handoff
artifact when the loop terminates with work remaining. Auto-
classifies remaining active blockers into a small set of disposition
categories so the operator reading one file sees what's left, what
the producer tried, and how to disposition each item.

The classification is HEURISTIC. The rendered handoff says so
explicitly; the operator's judgment owns the final call.

the heuristic classifier + its phrase tables + category
labels/descriptions live in :mod:`.handoff_classify`; this module renders
handoff.md.
"""

from __future__ import annotations

from pathlib import Path

from syncade.synthesis import ConsolidatedFinding

from ._atomic import atomic_write_text
from ._markdown import _file_or_repo_wide
from .handoff_classify import (
    _HANDOFF_CATEGORY_DESCRIPTIONS,
    _HANDOFF_CATEGORY_LABELS,
    _classify_handoff_finding,
)

_HANDOFF_TERMINATION_REASON_LABELS: dict[str, str] = {
    "ship": "SHIP",
    "findings_present": "findings present",
    "max_rounds_reached": "max rounds reached",
    "producer_stalled": "producer stalled",
    "producer_subprocess_error": "producer subprocess error",
    "reviewer_failure": "reviewer failure",
    "synth_failure": "synthesizer failure",
    "test_subprocess_error": "test subprocess error",
    "worktree_error": "worktree provisioning error",
    "diff_malformed": "diff filter refusal (unidentifiable headers)",
    "parse_failure": "output parse failure",
    "config_error": "config error",
}


def _handoff_producer_commit_subject(repo_root: Path | None, ending_sha: str) -> str:
    """Look up the producer commit subject without importing loop_summary.

    Handoff and loop_summary are peers in the top persistence layer, so
    handoff keeps this small best-effort helper local to preserve the
    documented acyclic layer direction.
    """
    if repo_root is None or not ending_sha:
        return ""
    try:
        from syncade.process import run_subprocess

        result = run_subprocess(
            ["git", "log", "-1", "--pretty=format:%s", ending_sha],
            cwd=repo_root,
            timeout=5.0,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def persist_handoff(
    run_dir: Path,
    *,
    final_exit_code: int,
    final_round: int,  # noqa: ARG001 — accepted for API symmetry with persist_loop_summary/manifest
    termination_reason: str,
    rounds: list,  # list[RoundResult]; typed as list to avoid circular import
    max_rounds: int,
    pr_doc_path: Path | None = None,
    repo_root: Path | None = None,
) -> Path | None:
    """Write ``<run_dir>/handoff.md`` — structured operator handoff
    when the loop terminates with work remaining.

    Generated only when ``final_exit_code in (20, 30)`` AND the final
    round's synthesizer surfaced at least one active (non-dismissed)
    blocker. Returns ``None`` on any other path so the caller (the
    orchestrator) can call this unconditionally and rely on the
    function's own gate.

    The handoff is APPEND, not REPLACE: ``loop-summary.md`` is still
    written by :func:`persist_loop_summary` with its current shape
    and role (high-level rollup). The handoff focuses narrowly on
    "what's still blocking, here's what the producer tried, here's
    how to disposition the remaining work" so the operator reading
    one file gets the whole picture without grepping multiple
    artifacts.

    Auto-classification is HEURISTIC. The rendered handoff explicitly
    documents this so the operator doesn't over-trust the
    categorization. See :func:`_classify_handoff_finding` for the
    classification rules and priority order.

    Args:
        run_dir: Top-level run directory
            (``<repo>/.syncade/runs/<id>/``).
        final_exit_code: The loop's final exit code. The handoff
            fires only for ``20`` (max_rounds_reached) and ``30``
            (findings present / producer stalled).
        final_round: 0-indexed round that terminated the loop.
        termination_reason: Categorical termination label (see
            :data:`syncade.orchestrator.TerminationReason`).
        rounds: List of :class:`syncade.orchestrator.RoundResult`,
            one per round executed.
        max_rounds: Configured ``[loop] max_rounds`` ceiling.
        pr_doc_path: Path to the PR brief that drove the run. Used
            by the auto-classifier to recognize "this finding's file
            IS the PR brief" → category ``"P"``. ``None`` skips the
            path-based check (phrase-based classification still
            runs).
        repo_root: Repo root, used for ``git log`` lookups when
            rendering producer commit subjects. Optional — falls
            back to the SHA-only form when missing.

    Returns:
        Path of the written ``handoff.md`` on the write path;
        ``None`` when the exit code or active-blocker conditions
        aren't met (no handoff to write).

    Raises:
        FileNotFoundError: If ``run_dir`` does not exist (caller
            bug — the orchestrator creates it during run setup).
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    # Gate 1: only fire on exit 20 / 30.
    if final_exit_code not in (20, 30):
        return None

    # Gate 2: must have active blockers in the FINAL round's synth.
    # The spec contract is: final_exit_code in (20, 30) AND
    # active_blocker_count > 0. Zero-blocker exit-30 paths (e.g.
    # producer_stalled after a clean synth) do not produce a handoff
    # — there are no remaining blockers for the operator to action.
    if not rounds:
        return None
    last_round = rounds[-1]
    active_blockers: list[ConsolidatedFinding] = []
    if last_round.synth_result is not None and last_round.synth_result.output is not None:
        for f in last_round.synth_result.output.consolidated_findings:
            if not f.dismissed and f.severity == "blocker":
                active_blockers.append(f)

    if not active_blockers:
        return None

    run_id = run_dir.name

    # ---- Header ------------------------------------------------------
    verdict_label = "NO-SHIP"
    reason_label = _HANDOFF_TERMINATION_REASON_LABELS.get(termination_reason, termination_reason)

    # SHA annotation. The handoff describes the FINAL round's
    # outstanding blockers, so the SHA the operator (or future agent)
    # needs is the FINAL round's snapshot SHA — what reviewers had as
    # HEAD when they produced the findings under inspection. The
    # empty-``rounds`` branch is defensive; the gate above returns
    # ``None`` before we get here when no rounds ran.
    last_round_sha = rounds[-1].snapshot.commit_sha if rounds else ""
    if last_round_sha:
        sha_line = (
            f"**Generated against SHA:** `{last_round_sha[:12]}` (full: `{last_round_sha}`)  "
        )
    else:
        sha_line = "**Generated against SHA:** (unknown — no rounds executed)  "

    lines: list[str] = [
        f"# Syncade run {run_id} — handoff",
        "",
        f"**Final verdict:** {verdict_label}  ",
        f"**Final exit code:** {final_exit_code}  ",
        f"**Termination reason:** {reason_label}  ",
        sha_line,
        f"**Rounds executed:** {len(rounds)} of {max_rounds}  ",
        f"**Active blockers remaining:** {len(active_blockers)}",
        "",
        "> This handoff is generated automatically when the loop "
        "terminates with work remaining. The disposition categories "
        "below are HEURISTIC — the operator's judgment owns the "
        "final call. See the per-blocker provenance for the raw "
        "reviewer outputs.",
        "",
    ]

    # ---- What's left -------------------------------------------------
    # The zero-blockers path returns None at the gate above, so by here ``active_blockers`` is
    # always non-empty. Classify and render directly.
    lines.append("## What's left")
    lines.append("")
    classifications: list[tuple[ConsolidatedFinding, str]] = [
        (f, _classify_handoff_finding(f, pr_doc_path=pr_doc_path)) for f in active_blockers
    ]
    for i, (finding, category) in enumerate(classifications, start=1):
        first_line = finding.description.strip().splitlines()[0]
        lines.append(f"### Blocker {i} — {first_line}")
        lines.append("")
        # File + provenance
        file_md = _file_or_repo_wide(finding.file)
        lines.append(f"- **File:** {file_md}")
        provenance_md = ", ".join(
            f"{p.reviewer_name} ({p.original_severity})" for p in finding.provenance
        )
        lines.append(f"- **Provenance:** {provenance_md}")
        # Description body. Flatten newlines to spaces so a multi-line
        # synthesizer description doesn't break the bullet list — mirrors
        # findings_md.py's per-reviewer-description handling.
        description = finding.description.strip().replace("\n", " ")
        lines.append(f"- **Description:** {description}")
        # Classification
        cat_label = _HANDOFF_CATEGORY_LABELS.get(category, category)
        lines.append(f"- **Suggested disposition category:** {category} — {cat_label}")
        lines.append(f"- **Operator action:** {_HANDOFF_CATEGORY_DESCRIPTIONS.get(category, '')}")
        lines.append("")

    # ---- What the producer attempted --------------------------------
    lines.append("## What the producer attempted")
    lines.append("")
    producer_rounds = [r for r in rounds if r.producer_result is not None]
    if not producer_rounds:
        lines.append("_(no producer rounds ran)_")
        lines.append("")
    else:
        # Build round_idx → active blocker count for "Findings
        # addressed" / "Remaining forwarded" heuristic rollup.
        round_blocker_count: dict[int, int] = {}
        for r in rounds:
            count = 0
            if r.synth_result is not None and r.synth_result.output is not None:
                for f in r.synth_result.output.consolidated_findings:
                    if not f.dismissed and f.severity == "blocker":
                        count += 1
            round_blocker_count[r.round_idx] = count

        for r in producer_rounds:
            pr = r.producer_result
            if pr.outcome == "committed":
                short_sha = pr.ending_sha[:12]
                subject = _handoff_producer_commit_subject(repo_root, pr.ending_sha)
                if subject:
                    lines.append(
                        f'- **Round {r.round_idx} producer commit:** `{short_sha}` ("{subject}")'
                    )
                else:
                    lines.append(f"- **Round {r.round_idx} producer commit:** `{short_sha}`")
                # Heuristic rollup: compare this round's synth blocker
                # count to the next round's to approximate how many the
                # producer addressed. "Heuristic" is surfaced explicitly
                # so the operator doesn't over-trust the count.
                k_before = round_blocker_count.get(r.round_idx, 0)
                next_idx = r.round_idx + 1
                if next_idx in round_blocker_count:
                    k_after = round_blocker_count[next_idx]
                    addressed = max(0, k_before - k_after)
                    lines.append(
                        f"- **Findings addressed:** (heuristic) ~{addressed} of {k_before}"
                    )
                    lines.append(
                        f"- **Remaining findings forwarded to round {next_idx}:** {k_after}"
                    )
                else:
                    lines.append(
                        "- **Findings addressed:** (heuristic) unknown — no subsequent round synth"
                    )
                    lines.append(
                        f"- **Remaining findings forwarded to round {next_idx}:** N/A (final round)"
                    )
            elif pr.outcome == "stalled":
                lines.append(
                    f"- **Round {r.round_idx} producer:** stalled "
                    f"(no commit; HEAD stayed at `{pr.starting_sha[:12]}`)"
                )
            elif pr.outcome == "escalated":
                # the handoff fires only on exit 20 / 30; an HONORED
                # escalation exits 10 and never reaches here. So an escalated
                # producer in the handoff was NOT honored — its escalation left
                # active blocker(s) uncovered and the loop treated the round as
                # a stall. Classify it as such.
                lines.append(
                    f"- **Round {r.round_idx} producer:** escalated but not honored "
                    f"(left active blocker(s) uncovered → treated as stall; "
                    f"HEAD stayed at `{pr.starting_sha[:12]}`)"
                )
            else:
                err = type(pr.error).__name__ if pr.error else "Unknown"
                lines.append(
                    f"- **Round {r.round_idx} producer:** subprocess_error "
                    f"({err}; HEAD stayed at `{pr.starting_sha[:12]}`)"
                )
        lines.append("")

    # ---- Suggested next-step categories -----------------------------
    lines.append("## Suggested next-step categories")
    lines.append("")
    lines.append(
        "The operator-procedural pattern says some findings "
        "self-resolve in the next commit, some are environment-bound, "
        "some are real-code-defects. Below is the auto-classification "
        "for THIS run's remaining blockers. The classification is "
        "HEURISTIC — phrase-matching against synthesizer + reviewer "
        "descriptions plus a file-path check for modified-file findings. "
        "Treat it as a starting point, not a decision."
    )
    lines.append("")
    # ``active_blockers`` is non-empty by the gate (see above), so
    # ``classifications`` is always populated here. Group by category.
    category_buckets: dict[str, list[tuple[int, ConsolidatedFinding]]] = {
        c: [] for c in ("M", "F", "P", "A", "D")
    }
    for i, (finding, category) in enumerate(classifications, start=1):
        category_buckets.setdefault(category, []).append((i, finding))
    for cat in ("M", "F", "P", "A", "D"):
        bucket = category_buckets.get(cat, [])
        label = _HANDOFF_CATEGORY_LABELS[cat]
        description = _HANDOFF_CATEGORY_DESCRIPTIONS[cat]
        lines.append(f"### {cat} — {label}")
        lines.append("")
        lines.append(description)
        lines.append("")
        if not bucket:
            lines.append("- _(none)_")
        else:
            for idx, finding in bucket:
                first_line = finding.description.strip().splitlines()[0]
                lines.append(f"- Blocker {idx}: {first_line}")
        lines.append("")

    # ---- Next steps -------------------------------------------------
    lines.append("## Next steps")
    lines.append("")
    if termination_reason == "max_rounds_reached":
        lines.append(
            "- The loop ran the configured ``max_rounds`` without "
            "converging. Address the M-category findings in a new "
            "commit, then re-run with ``--max-rounds 1`` to verify "
            "just the fixes. P-category findings self-resolve when "
            "the operator commits the completion record. F-category "
            "findings reflect brief/implementation drift — amend the "
            "brief or add the convention to CLAUDE.md. A-category "
            "findings require the operator to run the gate locally "
            "and attest in the completion record."
        )
    elif termination_reason == "findings_present":
        lines.append(
            "- The single-pass run found remaining active blockers. "
            "Address the findings manually, then re-run syncade to "
            "verify the updated tree."
        )
    elif termination_reason == "producer_stalled":
        lines.append(
            "- The producer ran but didn't commit. Inspect the "
            "final round's ``producer.stdout`` to see what it "
            "attempted; common causes are under-specified findings "
            "or the producer concluding the existing code is "
            "already correct. Address the finding manually or "
            "refine the producer prompt, then re-run."
        )
    elif termination_reason == "producer_subprocess_error":
        lines.append(
            "- The producer subprocess failed before it could "
            "attempt a fix. Read the final round's "
            "``producer.stderr`` and ``producer.error.txt`` for the "
            "exception trace. Common causes: auth (run ``claude "
            "login`` / ``codex login``), network errors, missing "
            "CLI binary."
        )
    else:
        lines.append(
            f"- Loop terminated with reason ``{termination_reason}``. "
            "Read the per-round artifacts under this run directory "
            "for the failure shape."
        )
    lines.append("")

    handoff_path = run_dir / "handoff.md"
    atomic_write_text(handoff_path, "\n".join(lines))
    return handoff_path
