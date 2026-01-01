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

from syncade.producer_escalation import ProducerEscalation

from ._atomic import atomic_write_text

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

    Returns:
        Path of the written ``decision-needed.md``.

    Raises:
        FileNotFoundError: If ``run_dir`` does not exist.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    lines: list[str] = [
        f"# Decision needed — Syncade run {run_id}",
        "",
        f"The producer escalated a finding in round {round_idx} that it "
        "determined is an **operator decision** — a spec/design conflict it "
        "cannot resolve in code without a human ruling, not a defect it can "
        "fix. The loop checkpointed and terminated (exit 10); no branch was "
        "advanced and the mechanical verdict is unchanged (still NO-SHIP).",
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
