"""Decision-needed checkpoint persistence.

When a NO-SHIP round's producer escalates a finding as an operator
decision (see :mod:`syncade.producer_escalation`) AND the loop honors
that escalation, the loop terminator writes
``<run_dir>/decision-needed.md`` — the operator-facing checkpoint
carrying the producer's case (the finding, the decision to make, the
concrete options, the reproduction-backed rationale) and the
record-a-decision-then-resume instructions. Since the escalation
is honored (and this file written) ONLY when its ``finding_indices``
cover every active blocker in the round; an escalation that leaves a
blocker uncovered is treated as a producer stall (exit 30) and this
file is not written.

The companion is ``decision.txt``: the operator writes their decision
into ``<run_dir>/decision.txt`` and runs ``syncade --resume <run-id>``;
the resumed round's producer receives that text. The filename constant
and the reader live here so the writer (which tells the operator where to
write) and the reader (resume) share one source of truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from syncade.producer_escalation import ProducerEscalation

from ._atomic import atomic_write_text

if TYPE_CHECKING:
    from syncade.test_runner import TestRunResult

DECISION_NEEDED_FILENAME = "decision-needed.md"
"""The operator-facing escalation checkpoint, at the run root."""

OPERATOR_DECISION_FILENAME = "decision.txt"
"""Where the operator records the decision the resume reads back. Plain
text (the producer receives it verbatim). Single source of truth shared
by :func:`persist_decision_needed` (which instructs the operator) and
:func:`read_operator_decision` (which the resume path calls)."""


def persist_decision_needed(
    run_dir: Path,
    *,
    round_idx: int,
    escalation: ProducerEscalation,
    run_id: str,
    branch_advanced: bool = False,
    check_results: list[TestRunResult] | None = None,
) -> Path:
    """Write ``<run_dir>/decision-needed.md`` for an honored escalation.

    The loop terminator calls this only when the round's producer
    escalated AND the escalation's ``finding_indices`` covered every
    active blocker; a non-honored escalation maps to a producer
    stall (exit 30) and never reaches this writer.

    Args:
        run_dir: Top-level run directory (``<repo>/.syncade/runs/<id>/``).
        round_idx: The 0-indexed round whose producer escalated.
        escalation: The structured :class:`ProducerEscalation` the
            producer emitted.
        run_id: The run-id, for the heading + the resume command.
        branch_advanced: Whether an EARLIER imported candidate already
            fast-forwarded the branch, through the trusted importer and the
            compare-and-swap advance.
        check_results: The mechanical-check results from this round, if any.
            Failing blocking checks are listed in the document so the operator
            knows that resuming addresses the producer question but the checks
            will still rerun and must also pass.

    Returns:
        Path of the written ``decision-needed.md``.

    Raises:
        FileNotFoundError: If ``run_dir`` does not exist.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    _branch_note = (
        "**Your branch was already advanced** by an earlier round in this "
        "run — producer commits from that round are on it. Nothing was "
        "advanced for THIS round."
        if branch_advanced
        else "No branch was advanced."
    )
    lines: list[str] = [
        f"# Decision needed — Syncade run {run_id}",
        "",
        f"The producer escalated a finding in round {round_idx} that it "
        "determined is an **operator decision** — a spec/design conflict it "
        "cannot resolve in code without a human ruling, not a defect it can "
        f"fix. The loop checkpointed and terminated (exit 10); {_branch_note} "
        "The mechanical verdict is unchanged (still NO-SHIP).",
        "",
        "## The finding",
        "",
        *_md_text_block_lines(escalation.finding),
        "",
        "## The decision you must make",
        "",
        *_md_text_block_lines(escalation.decision),
        "",
        "## Options",
        "",
    ]
    for idx, option in enumerate(escalation.options, start=1):
        lines.extend([f"### Option {idx}", "", *_md_text_block_lines(option), ""])
    lines += [
        "## The producer's rationale (reproduction-backed)",
        "",
        *_md_text_block_lines(escalation.rationale),
        "",
    ]

    failing_blocking_checks = [
        c for c in (check_results or []) if c.severity == "blocking" and c.outcome != "passed"
    ]
    if failing_blocking_checks:
        lines += [
            "## Co-failing blocking checks",
            "",
            "These blocking mechanical checks also failed this round. Resuming "
            "addresses the producer decision above, but the checks rerun on "
            "resume and must also pass before the round can SHIP:",
            "",
        ]
        for c in failing_blocking_checks:
            status = "FAIL" if c.outcome == "failed" else "ERROR"
            lines.append(f"- **{c.name}**: {status} (exit {c.exit_code})")
            if c.name:
                lines.append(
                    f"  Output: `{c.name}.check.stdout` / `{c.name}.check.stderr` "
                    f"in `round-{round_idx}/`"
                )
        lines.append("")

    lines += [
        "## How to continue",
        "",
        f"1. Decide, then write your decision (the option you chose plus any "
        f"specifics the producer needs) into `{OPERATOR_DECISION_FILENAME}` in "
        "this run directory.",
        f"2. Resume: `syncade --resume {run_id}`. The escalated round re-runs "
        "with your decision fed to the producer. (Resume refuses if "
        f"`{OPERATOR_DECISION_FILENAME}` is missing or empty.)",
        "",
    ]

    path = run_dir / DECISION_NEEDED_FILENAME
    atomic_write_text(path, "\n".join(lines))
    return path


def _md_text_block_lines(text: str) -> list[str]:
    max_run = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            if current > max_run:
                max_run = current
        else:
            current = 0
    fence = "`" * max(4, max_run + 1)
    return [f"{fence}text", text.rstrip("\n"), fence]


def read_operator_decision(run_dir: Path) -> str | None:
    """Return the operator's recorded decision from
    ``<run_dir>/decision.txt``, or ``None`` when the file is absent or
    blank (whitespace-only counts as absent — an empty file is not a
    decision). Stripped of surrounding whitespace.
    """
    decision_path = run_dir / OPERATOR_DECISION_FILENAME
    if not decision_path.is_file():
        return None
    text = decision_path.read_text(encoding="utf-8").strip()
    return text or None


def persist_deactivated_blockers_decision_needed(
    run_dir: Path,
    *,
    round_idx: int,
    run_id: str,
    deactivated: list[tuple[str, str, str]],
    branch_advanced: bool = False,
) -> Path:
    """Write ``decision-needed.md`` for a PR-h-01 increment-D escalation.

    The other escalation in this module is the producer's: it argues a case and
    asks the operator to choose. This one has no advocate. Two or more distinct
    reviewers independently raised blockers, the synthesizer ruled every one of
    them out, and nothing checked whether those rulings were right — so the
    round is neither a defensible SHIP nor a mechanical NO-SHIP.

    The document's whole job is to put the reviewers' own words in front of the
    operator next to what the synthesizer did with them, because that
    comparison is the decision. Quotes are verbatim: provenance text is
    cross-checked against the source reviewer's finding
    (``syncade.synthesizer.validation``), so what is printed here is what the
    reviewer actually wrote.

    Args:
        run_dir: Top-level run directory (``<repo>/.syncade/runs/<id>/``).
        round_idx: The 0-indexed round that escalated.
        run_id: The run-id, for the heading.
        deactivated: ``(reviewer_name, verbatim_finding_text, disposition)``
            per deactivated source blocker, where ``disposition`` describes how
            the synthesizer removed it (e.g. ``dismissed: <rationale>``).
        branch_advanced: Whether an EARLIER imported candidate already
            fast-forwarded the branch. Passed in rather than assumed because
            telling an operator their branch is untouched when it is not is
            exactly the kind of wrong that gets acted on.

    Returns:
        Path of the written ``decision-needed.md``.

    Raises:
        FileNotFoundError: If ``run_dir`` does not exist.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    reviewers = sorted({name for name, _, _ in deactivated})
    lines: list[str] = [
        f"# Decision needed — Syncade run {run_id}",
        "",
        f"In round {round_idx}, **{len(reviewers)} independent reviewers "
        f"({', '.join(reviewers)}) each raised at least one blocker, and the "
        "synthesizer deactivated every one of them** — by dismissal, by "
        "downgrade, or by splitting one concern into separate single-reviewer "
        "findings it could then rule out individually.",
        "",
        (
            "That may be entirely correct. But independent corroboration is the "
            "strongest signal this tool produces, and a machine should not "
            "discard all of it silently. The loop terminated at exit 10 rather "
            "than reporting a SHIP it cannot justify."
        ),
        "",
        (
            "**Your branch was already advanced** by an earlier round in this "
            "run — producer commits from that round are on it. Nothing was "
            "advanced for THIS round."
            if branch_advanced
            else "No branch was advanced."
        ),
        "",
        "## What each reviewer actually said",
        "",
        "Quoted verbatim from the reviewers' own output — not the synthesizer's restatement of it.",
        "",
    ]
    for idx, (reviewer, text, disposition) in enumerate(deactivated, start=1):
        lines.extend(
            [
                f"### {idx}. {reviewer} — blocker",
                "",
                *_md_text_block_lines(text),
                "",
                f"**Synthesizer's disposition:** {disposition}",
                "",
            ]
        )
    lines += [
        "## How to continue",
        "",
        "Read the quotes above and decide whether the synthesizer was right.",
        "",
        "- **It was right** — the concerns really are false positives. This "
        "round is a SHIP; nothing is blocking you.",
        "- **It was wrong about any of them** — that concern is real and "
        "unfixed. Address it and re-run.",
        "",
        "The full consolidated view, including each dismissal rationale in "
        f"context, is in `round-{round_idx}/findings.md`.",
        "",
    ]

    path = run_dir / DECISION_NEEDED_FILENAME
    atomic_write_text(path, "\n".join(lines))
    return path
