"""Escalation coverage guard.

The loop terminator honors a producer escalation (checkpoint → exit 10 +
``decision_needed`` + ``decision-needed.md``) **only when the escalation
covers every active blocker in the round**. Otherwise the escalation left
work undone and is treated as an ordinary stall (exit 30).

**The bug this closes (codex reproduced it in the validation):** the
terminator honored *any* escalation unconditionally. Because ``escalated``
is mutually exclusive with committing (``escalated`` ⟺ no-commit), a
producer could escalate finding A, commit nothing for active blocker B, and
still short-circuit the loop to exit 10 — leaving B silently unresolved.

**The fix is mechanical set-containment, not a count and not prompt-trust.**
Escalation references findings by index into
``SynthesizerOutput.consolidated_findings``. The orchestrator computes the
active-blocker index set from the
synth's OWN output and honors the escalation iff:

1. every referenced ``finding_indices`` entry is a real active (non-dismissed)
   blocker — catches out-of-range, dismissed, and minor/nit references; and
2. every active blocker is referenced.

Together those two are set equality, but they're checked separately so the
mapping to the brief's two requirements (and the cluster cross-check shape)
stays legible. A count-guard ("honor only when the escalated finding is the
sole blocker") was rejected: one operator decision can legitimately unblock
several blockers (A, B, C all downstream of one design question), and a
sole-blocker guard would false-downgrade that to a stall.

Pure module — no I/O, no loop logic. The terminator wiring lives in
:func:`syncade.orchestrator.loop.run_review`.
"""

from __future__ import annotations

from syncade.producer_escalation import ProducerEscalation
from syncade.synthesis import SynthesizerOutput


def _active_blocker_indices(synth_output: SynthesizerOutput) -> set[int]:
    """The indices into ``consolidated_findings`` that are active
    (non-dismissed) blockers — the same condition
    :func:`syncade.synthesis.has_active_blocker` reduces over."""
    return {
        i
        for i, f in enumerate(synth_output.consolidated_findings)
        if f.severity == "blocker" and not f.dismissed
    }


def escalation_covers_active_blockers(
    escalation: ProducerEscalation,
    synth_output: SynthesizerOutput | None,
) -> bool:
    """Return ``True`` iff the loop should honor this escalation as an
    operator-decision checkpoint.

    The escalation is honored only when its ``finding_indices`` exactly cover
    the round's active-blocker set: every referenced index is a real active
    blocker (in range + non-dismissed + ``severity == "blocker"``) and every
    active blocker is referenced. Any mismatch — a left-uncovered blocker, an
    out-of-range index, or a reference to a dismissed / minor / nit finding —
    returns ``False`` (the terminator then treats the round as a stall, exit
    30). Never raises: a bad reference is a non-honored escalation, not a crash.

    ``synth_output is None`` (no consolidated findings in hand) → ``False``.
    No active blockers at all (e.g. a test-failure-driven NO-SHIP round) →
    ``False``: there is no blocker an operator decision could resolve.
    """
    if synth_output is None:
        return False
    active = _active_blocker_indices(synth_output)
    if not active:
        return False
    covered = set(escalation.finding_indices)
    # (1) every referenced index must be a real active blocker.
    if not covered <= active:
        return False
    # (2) every active blocker must be covered.
    return active <= covered
